# memory.py — The Memory Engine

`memory.py` is the core of the project. It decides **what to remember**,
**detects contradictions**, **versions old facts**, **expires dated facts**,
and **retrieves the most relevant memories** for each reply. All data lives
in PostgreSQL (Neon + pgvector). Every function gets its connection from
`db.get_conn()`.

---

## 1. Pipeline on every message

```
user message
  1. log_message()      -> saved in the messages table (raw chat log)
  2. retrieve()         -> top-5 relevant, active memories (weighted score)
  3. assemble_context() -> prompt = persona + memories + recent chat + message
  4. generate_reply()   -> (llm.py) Groq writes the reply
  5. is_fact()?         -> only statements become memories, never questions
  6. find_conflicts()   -> does it contradict a stored fact?
        yes -> user ticks the outdated facts -> resolve_conflict()
        no  -> store_memory()
```

**Guarantees**
- Exact duplicates are never stored twice; repeating a fact **reinforces**
  it instead.
- **All** contradicting facts are shown, not just the closest one.
- Replaced facts are **kept for history** but excluded from retrieval.

---

## 2. Tables used

| Table | Purpose |
|---|---|
| `agents` | `agent_id`, `name`, `description` (the bot personas) |
| `messages` | Raw chat log: `message_id`, `agent_id`, `role` ('user'/'agent'), `content`, `priority`, `conversation_id` |
| `memories` | Facts: `memory_id`, `agent_id` (NULL if shared), `scope` ('private'/'shared'), `content`, `priority` 1–5, `embedding vector(384)`, `access_count`, `last_accessed`, `created_at`, `valid_until` (TIMESTAMPTZ), `superseded`, `superseded_by` (self-FK), `superseded_at`, `source_message_id` (FK to messages) |
| `memory_audit` | Change log written **only by a trigger**, never by Python |

---

## 3. Tuning knobs (top of the file)

```python
CONFLICT_THRESHOLD = 0.50   # similarity above this = probably the SAME fact
PREFIX_WORDS       = 3      # same first 3 words ("i live in") = same attribute...
PREFIX_FLOOR       = 0.30   # ...so flag it even at this lower similarity
MIN_SIMILARITY     = 0.25   # below this = unrelated, never retrieved

W_SIM, W_PRI, W_RECENCY, W_URGENCY = 1.0, 0.5, 0.3, 0.7   # retrieval weights
```

---

## 4. Embeddings

```python
_model = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text):
    return _model.encode(text).tolist()        # text -> 384 numbers

def _to_pgvector(vec):
    return "[" + ",".join(str(x) for x in vec) + "]"   # '[0.1,0.2,...]'
```

- An **embedding** is a list of 384 numbers that captures a sentence's
  **meaning**. Similar meanings give vectors that point in similar directions.
- MiniLM runs **locally**, so it is free and works offline after a one-time
  ~90 MB download. It is loaded once when the module is imported.
- `_to_pgvector` formats the list as text that pgvector accepts
  (`%s::vector`).
- In SQL, `embedding <=> query` is pgvector's **cosine distance**, so
  **similarity = 1 − distance** (1 = same meaning, 0 = unrelated).

---

## 5. Messages (raw chat log)

| Function | SQL | Notes |
|---|---|---|
| `log_message(agent_id, role, content, conversation_id)` | `INSERT ... RETURNING message_id` | Also bumps `conversations.updated_at` in the **same transaction**. The returned ID becomes the memory's `source_message_id`. |
| `load_messages(agent_id, limit)` | `SELECT ... ORDER BY message_id` | An agent's chat history, oldest first. |

---

## 6. CRUD on memories (shown on the Manage Data page)

| Op | Function | Notes |
|---|---|---|
| **Read** | `list_memories(agent_id, include_superseded)` | `WHERE scope = 'shared' OR agent_id = %s`: own private memories plus shared ones. |
| **Update** | `update_memory(id, content, priority, valid_until, clear_expiry)` | Builds the SET list dynamically. If the text changes it is **re-embedded**. Extending the expiry **restores** a memory that `fn_purge_expired` had retired. |
| **Delete** | `delete_memory(id)` | First sets `superseded_by = NULL` on rows that point to it, so the **self-referencing FK** doesn't block the delete. The audit trigger still records it. |
| Audit | `list_audit(agent_id)` | Reads `memory_audit`, newest first. |
| Agents | `list_agents`, `create_agent`, `update_agent`, `delete_agent` | `delete_agent` clears FK references, then removes the agent's memories, messages, and the agent itself, in one transaction. |

**Safe dynamic SQL:** `update_memory` uses an f-string only to join **fixed
column names** (`content = %s`, ...). Every value is a `%s` parameter, so
there is no SQL injection.

---

## 7. What counts as a memory: `is_fact(text)`

Returns `False` for:
- text ending in `?`;
- text starting with a question word (`what`, `when`, `how`, `do`, `is`,
  `tell me`, ...);
- fewer than 3 words (`hello`, `ok thanks`).

**Why:** questions are retrieval cues, not facts. Storing them pollutes the
memory store and creates false conflicts between similarly worded questions.

---

## 8. Priority: `detect_priority(text)`

A transparent, rule-based score from 1 to 5:

| Rule | Priority |
|---|---|
| Contains `important`, `remember`, `deadline`, `always`, `never`, `allergic`, `exam`, `my name is`, `i prefer` or `i live` | **5** |
| Contains `?` | **2** |
| Anything else | **1** |

A CHECK constraint in the DB (`priority BETWEEN 1 AND 5`) enforces the range.

---

## 9. Conflict detection: `find_conflicts(agent_id, content)`

Returns **every** active memory that is about the same thing but says
something different, as `[(memory_id, content, similarity), ...]`, most
similar first.

A stored memory is a conflict if it is active (not superseded, not expired),
is not the exact same text, and **either**:

1. **similarity > 0.50**, or
2. **similarity > 0.30 and the first 3 words match**.

```sql
(regexp_split_to_array(lower(btrim(content)), '[[:space:]]+'))[1:3]
  = (regexp_split_to_array(lower(btrim(new_text)), '[[:space:]]+'))[1:3]
```

**Why rule 2?** "I live in Trivandrum" vs "I live in Mumbai now" scores only
**0.36**, because the embedding is dominated by the place names. The same
opening ("i live in") means the same attribute with a new value.

**Why return all of them?** If two old versions exist, both get retired
together, so contradictions can't silently pile up.

---

## 10. Expiry (valid time): `extract_expiry(text)`

Finds a date in the text and returns it as the memory's `valid_until`.

1. `dateparser.search_dates` finds candidate dates (preferring future ones).
2. The `_REAL_DATE` regex **keeps only real calendar dates**:
   - day + month name (`20 November 2026`, `Nov 20`)
   - numeric (`20/11/2026`, `2026-11-20`)
   - weekday names, `tomorrow`
3. It takes the **latest** date and sets the time to **23:59:59**, so the
   fact stays valid through that whole day.
4. `.astimezone()` attaches the local time zone, so the **TIMESTAMPTZ**
   column stores the correct instant.

**Why the regex?** dateparser also reads ordinary words as dates (`now`,
`may`, `am 21`), which gave "I live in Pune now" an expiry of tonight.

**Why TIMESTAMPTZ?** The column was originally `TIMESTAMP` (no zone) and
Neon runs in UTC, so an IST end-of-day expired **5.5 h late**.

---

## 11. Store: `_store()` and `store_memory()`

```
_store(cur, content, agent_id, scope, source_message_id):
    same text already active?  -> UPDATE access_count + 1   (reinforce, no duplicate)
    otherwise                  -> INSERT with embedding, priority, valid_until, source id
```

- `_store` takes the **caller's cursor**, so it can be part of a larger
  transaction (used by `resolve_conflict`).
- Shared memories are saved with `agent_id = NULL`.
- `source_message_id` records **provenance**: which message created this
  fact.
- `store_memory` is the simple wrapper: get a connection, `_store`, commit.

---

## 12. Versioning: `resolve_conflict(...)`

Runs after the user ticks which old facts are no longer true:

```python
new_id = _store(...)                               # 1. insert the new fact
CALL sp_supersede(retire_ids, new_id, keep_ids)    # 2. retire / demote old ones
conn.commit()                                      # 3. ONE commit for both
```

| Input | Effect (done inside the stored procedure) |
|---|---|
| `retire_ids` (ticked) | `superseded = TRUE`, `superseded_by = new_id`, `priority = 1` |
| `keep_ids` (unticked: "both true") | `priority = priority − 2` (minimum 1), ranked below the new fact |
| `save_new = False` | Cancellation: nothing new is stored, the old fact is just retired |

**Atomicity:** the INSERT and the CALL share one transaction. If either
fails, both roll back, so the DB can never hold the new fact *and* the old
one it was meant to replace. The `trg_guard_supersede` trigger fills in
`superseded_at`.

### `version_history()`: a self-join

```sql
SELECT o.content, n.content, o.superseded_at
FROM memories o
LEFT JOIN memories n ON o.superseded_by = n.memory_id
WHERE o.superseded = TRUE
```

Shows `old -> new` for every replaced fact. It is a `LEFT JOIN` because a
cancelled or expired fact has no replacement.

---

## 13. Retrieval: `retrieve(agent_id, query, k=5)`

Filters to memories that are active, not expired and have
similarity > 0.25, then ranks by:

```
score = 1.0 × similarity                              (relevance)
      + 0.5 × priority / 5                            (importance)
      + 0.3 × 1 / (1 + days since created)            (recency: is it fresh?)
      + 0.7 × 1 / (1 + days until valid_until)        (urgency: is it soon? 0 if no date)
```

- **Recency vs urgency:** recency favours newly stored facts. Urgency makes
  a dated event (an exam) **more** important as its date approaches, which
  recency alone would get backwards.
- The whole score is computed **inside PostgreSQL** (`ORDER BY ... LIMIT k`),
  so only the top k rows travel over the network.
- **Reinforcement:** returned memories get `access_count + 1` and
  `last_accessed = NOW()`. The audit trigger ignores this, so reads don't
  flood the log.

---

## 14. Context assembly: `assemble_context(persona, memories, recent, message)`

Builds one prompt string:

```
<persona>

What you remember about the user ... :
- fact 1
- fact 2

Recent conversation:
user: ...
agent: ...

User: <message>
Assistant:
```

The LLM is stateless; this string is its only "memory".

---

## 15. Housekeeping and stats (DB functions)

| Python | Calls | Does |
|---|---|---|
| `purge_expired()` | `SELECT fn_purge_expired()` | Retires (does not delete) every expired memory; returns the count |
| `memory_stats(agent_id)` | `SELECT * FROM fn_memory_stats(%s)` | Active / retired / expired / avg priority / total recalls, computed with `COUNT(*) FILTER (WHERE ...)` |
| `memory_count()` | `SELECT COUNT(*) ...` | Active memories across all agents |
| `clear_data()` | `TRUNCATE ... RESTART IDENTITY CASCADE` | Wipes the data and restarts the IDs at 1 |

---

## 16. DBMS concepts in this file

| Concept | Where |
|---|---|
| Transactions / atomicity | `resolve_conflict`, `delete_agent`, `log_message` |
| Stored procedure (`CALL`) | `sp_supersede` |
| Functions | `fn_memory_stats`, `fn_purge_expired` |
| Triggers | `trg_memory_audit` (audit log), `trg_guard_supersede` (stamps the time, blocks self-supersede) |
| Self-join / LEFT JOIN | `version_history` |
| Self-referencing FK | `superseded_by` (handled in `delete_memory`, `delete_agent`) |
| FK with `ON DELETE SET NULL` | `source_message_id` |
| CHECK constraints | `scope`, `priority`, `role` |
| Soft delete / versioning | `superseded` flag instead of `DELETE` |
| Valid time (temporal DB) | `valid_until` + `extract_expiry` |
| Vector search | pgvector `<=>` cosine distance |
| Parameterized queries | every `%s`, which prevents SQL injection |
| Aggregates with `FILTER` | `fn_memory_stats` |

---

## 17. Test results (small hand-built test set)

| Test | Result |
|---|---|
| Conflict pairs caught by similarity alone (> 0.50) | 6 / 10 |
| Conflict pairs caught with the same-first-3-words rule | **9 / 10** |
| Unrelated pairs correctly ignored | 7 / 8 (false positive: "DBMS exam" vs "OS exam", 0.635) |
| City change "Trivandrum -> Mumbai" similarity | 0.36 (why the prefix rule exists) |
| Fake dates rejected / real dates kept | 5 / 5 and 5 / 5 |
| Time-zone bug | expiry was 5.5 h late; fixed with TIMESTAMPTZ |

The test set was 18 sentence pairs and 11 date sentences, written by hand.
It is not a standard benchmark.

---

## 18. Viva questions

- **Why soft-delete (`superseded`) instead of DELETE?** It keeps the history:
  that is the "temporal / versioned" part. Retrieval simply filters
  `superseded = FALSE`.
- **Why not store questions?** They aren't facts, and similar questions
  would look like conflicts.
- **What does `<=>` do?** It is pgvector's cosine distance operator;
  similarity = 1 − distance.
- **Why compute the score in SQL, not Python?** Only the top 5 rows are sent
  back, which means less data over the network.
- **Isn't `memories.content` a duplicate of `messages.content`?** On purpose:
  memories are edited and re-embedded independently. The FK
  `source_message_id` keeps the link.
- **Is there a vector index?** No; every search scans the whole table, which
  is fine at this size. At scale, add a pgvector **HNSW** or **IVFFlat**
  index.
- **What are the limitations?** Fixed thresholds give some false positives;
  priority uses keyword rules; the weights were tuned by hand.
