# MemoryChat — A Temporal Memory Management System for AI Agents

*Versioned, time-aware memory enforced by PostgreSQL.*

A DBMS course project (Semester 5). MemoryChat is a chat app whose AI agents
**remember facts about the user**, **notice when a fact changes**, **keep the
old version as history**, and **forget dated facts once they expire**. All of
this is stored and enforced in a PostgreSQL database.

The language model itself has no memory: every reply is built from what the
database retrieves.

---

## Features

- **Long-term memory per agent**: facts from the chat are stored as memories.
  Questions and greetings are not.
- **Private and shared memory**: a fact can belong to one agent or be visible
  to all of them.
- **Conflict detection**: "I live in Pune now" is flagged against "I live in
  Mumbai" (semantic similarity plus a same-first-words rule).
- **Versioning**: the user picks which facts are outdated; they are
  **superseded**, not deleted, and the version history shows `old → new`.
- **Expiry (valid time)**: dates in a message ("exam on 20 Nov 2026") set
  `valid_until`, and expired facts drop out of retrieval.
- **Weighted retrieval**: ranks memories by similarity, priority, recency and
  urgency (closer events rank higher).
- **Audit log**: a trigger records every update, supersede, restore and
  delete, even after the row itself is gone.
- **Chat threads**: ChatGPT-style sidebar with pin, rename, delete and search.
- **Manage Data page**: full CRUD on agents and memories, plus the message
  log and audit log.

---

## Tech stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Database | PostgreSQL on **Neon** (cloud) with the **pgvector** extension |
| DB driver | psycopg2 |
| Embeddings | sentence-transformers `all-MiniLM-L6-v2` (384-dim, runs locally) |
| LLM | Groq API, `openai/gpt-oss-20b` |
| Date parsing | dateparser |

Total cost: **₹0** (free tiers + open-source libraries).

---

## How it works

```
User message
   │
   ├─► log_message()       save to messages (raw log)
   ├─► retrieve()          top-5 active memories, weighted score (pgvector)
   ├─► assemble_context()  persona + memories + recent chat + message
   ├─► generate_reply()    Groq LLM writes the reply
   └─► is_fact()?
          └─► find_conflicts()
                 ├─ none     → store_memory()
                 └─ conflict → user ticks outdated facts
                               → resolve_conflict(): INSERT + CALL sp_supersede
                                 (one transaction)
```

---

## DBMS concepts used

| Concept | Where |
|---|---|
| CRUD | Manage Data page; memory and agent functions |
| Primary / foreign keys, self-referencing FK | `superseded_by` → `memories` |
| `ON DELETE CASCADE` / `SET NULL` | threads → messages; `source_message_id` |
| CHECK constraints | `scope`, `role`, `priority BETWEEN 1 AND 5` |
| Triggers (BEFORE and AFTER) | `trg_guard_supersede`, `trg_memory_audit` |
| Stored procedure | `sp_supersede` |
| Functions | `fn_memory_stats`, `fn_purge_expired` |
| Transactions / atomicity | `resolve_conflict`, `delete_agent` |
| Joins, GROUP BY, aggregates with FILTER | `version_history`, `list_agents_with_counts`, `fn_memory_stats` |
| Indexes | `idx_conversations_agent`, `idx_messages_conversation` |
| Temporal data, TIMESTAMPTZ | `valid_until`, expiry, purge |
| Soft delete / versioning | `superseded` flag |
| Vector search | pgvector `<=>` cosine distance |
| Parameterized queries | every `%s` (prevents SQL injection) |

---

## Project structure

| File | Purpose | Docs |
|---|---|---|
| `app.py` | Main chat UI | [README_app.md](README_app.md) |
| `pages/1_Manage_Data.py` | CRUD / admin page | [README_manage_data.md](README_manage_data.md) |
| `memory.py` | Memory engine: store, conflicts, versioning, expiry, retrieval | [README_memory.md](README_memory.md) |
| `conversations.py` | Chat threads | [README_conversations.md](README_conversations.md) |
| `llm.py` | Groq reply generation | [README_llm.md](README_llm.md) |
| `db.py` | Shared database connection | [README_db.md](README_db.md) |
| `db_upgrade.sql` | Constraints, triggers, procedure, functions | [README_db_upgrade.md](README_db_upgrade.md) |
| `.streamlit/config.toml` | Dark theme | – |

---

## Setup

1. **Install the dependencies** (Python 3.10+):
   ```
   pip install streamlit psycopg2-binary sentence-transformers groq dateparser
   ```

2. **Database:** create a Neon (or any PostgreSQL) database with the
   `vector` extension and the base tables `agents`, `conversations`,
   `messages` and `memories`. Then run **`db_upgrade.sql`** once in the SQL
   editor to add the constraints, triggers, procedure and functions.

3. **Secrets:** create `.streamlit/secrets.toml` (it is gitignored, so never
   commit it):
   ```toml
   NEON_CONN_STRING = "postgresql://<user>:<password>@<host>/<db>?sslmode=require"
   GROQ_API_KEY     = "<your Groq key>"
   # optional, shown on the user card
   USER_NAME  = "User"
   USER_EMAIL = ""
   ```

4. **Run:**
   ```
   streamlit run app.py
   ```
   Add an agent on the **Manage Data** page, then start chatting.

---

## Results (small hand-built test set)

| Test | Result |
|---|---|
| Conflicts caught by similarity alone | 6 / 10 |
| Conflicts caught with the same-first-words rule | **9 / 10** |
| Unrelated facts correctly ignored | 7 / 8 |
| Fake dates rejected / real dates kept | 5 / 5 and 5 / 5 |
| Expiry time-zone bug (5.5 h late) | fixed with TIMESTAMPTZ |
| Page reload after reusing the DB connection | ~5–6 s → ~1.7 s |

---

## Limitations and future scope

- Fixed similarity thresholds give some false positives ("DBMS exam" vs
  "OS exam").
- Priority is keyword-based, and the retrieval weights were tuned by hand.
- No vector index yet; add pgvector **HNSW** for large data.
- One shared DB connection, built for a single user; use a connection pool
  and add login for multiple users.
