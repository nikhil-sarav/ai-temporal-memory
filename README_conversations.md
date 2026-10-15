# conversations.py — Chat Threads (Sidebar)

`conversations.py` manages **chat threads**: the ChatGPT-style list in the
sidebar. A **conversation** is one chat thread that belongs to one agent.

**Key idea:** the **agent** owns the long-term **memory**; conversations only
organise the **raw chat messages** into threads you can switch between,
rename, pin and delete. Deleting a thread removes its messages but **not**
the memories learned from it.

```
agents (1) ──< conversations (many) ──< messages (many)
   │
   └──< memories   (per agent, not per thread)
```

---

## 1. The `conversations` table (checked in Neon)

| Column | Type | Default | Meaning |
|---|---|---|---|
| `conversation_id` | INTEGER, **PK** | `nextval(...)` (SERIAL) | Thread ID |
| `agent_id` | INTEGER, **FK** → `agents` | – | Owner agent, `ON DELETE CASCADE` |
| `title` | TEXT NOT NULL | `'New chat'` | Shown in the sidebar |
| `pinned` | BOOLEAN NOT NULL | `false` | Pinned threads stay at the top |
| `created_at` | TIMESTAMP NOT NULL | `now()` | When the thread was created |
| `updated_at` | TIMESTAMP NOT NULL | `now()` | Last activity (used for sorting) |

### Constraints and indexes

| Object | Definition | Why |
|---|---|---|
| `conversations_agent_id_fkey` | `FOREIGN KEY (agent_id) REFERENCES agents ON DELETE CASCADE` | Deleting an agent deletes its threads |
| `messages_conversation_id_fkey` | `FOREIGN KEY (conversation_id) REFERENCES conversations ON DELETE CASCADE` | Deleting a thread deletes its messages |
| `idx_conversations_agent` | B-tree on `(agent_id, pinned, updated_at)` | Speeds up the sidebar query (filter by agent, sort by pinned/updated) |
| `idx_messages_conversation` | B-tree on `messages(conversation_id)` | Speeds up loading one thread's messages |

**Cascade chain:** delete agent → its conversations → their messages.

---

## 2. The helper `_exec`

```python
def _exec(sql, params):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
```

- A shared helper for write-only statements (UPDATE/DELETE) that return
  nothing. It avoids repeating the connect / execute / commit / close block.
- Values are always passed as `%s` parameters, which prevents **SQL
  injection**.
- `conn.close()` only ends the transaction; the connection is reused (see
  `README_db.md`).

---

## 3. Functions (CRUD)

| Op | Function | SQL | Used in `app.py` |
|---|---|---|---|
| Read | `list_agents_with_counts()` | `LEFT JOIN` + `GROUP BY` | Sidebar agent list with thread counts |
| Create | `create_conversation(agent_id, title)` | `INSERT ... RETURNING` | **New chat** button |
| Read | `list_conversations(agent_id)` | `SELECT ... ORDER BY pinned DESC, updated_at DESC` | Sidebar thread list |
| Update | `rename_conversation(cid, title)` | `UPDATE title` | ⋯ menu → Rename |
| Update | `autoname_conversation(cid, text)` | Conditional `UPDATE` | When the first message is sent |
| Update | `set_pinned(cid, pinned)` | `UPDATE pinned` | ⋯ menu → Pin / Unpin |
| Delete | `delete_conversation(cid)` | `DELETE` (+ cascade) | ⋯ menu → Delete |
| Read | `load_chat(conversation_id)` | `SELECT role, content ...` | Shows the open thread |

### 3.1 `list_agents_with_counts()`: LEFT JOIN + GROUP BY

```sql
SELECT a.agent_id, a.name, a.description, COUNT(c.conversation_id)
FROM agents a
LEFT JOIN conversations c ON c.agent_id = a.agent_id
GROUP BY a.agent_id
ORDER BY a.agent_id
```

- **`LEFT JOIN`** keeps agents with **zero** threads. An `INNER JOIN` would
  drop them from the sidebar.
- **`COUNT(c.conversation_id)`, not `COUNT(*)`:** for an agent with no
  threads, the LEFT JOIN produces one row with `c.conversation_id = NULL`.
  `COUNT(column)` skips NULLs and gives **0**; `COUNT(*)` would wrongly give
  **1**.
- **`GROUP BY a.agent_id` only**, yet `name` and `description` are selected:
  PostgreSQL allows this because `agent_id` is the **primary key**, so name
  and description are **functionally dependent** on it (each group has one
  value).
- Returns `[(agent_id, name, description, n_conversations), ...]`.

### 3.2 `create_conversation(agent_id, title="New chat")`

```sql
INSERT INTO conversations (agent_id, title) VALUES (%s, %s)
RETURNING conversation_id
```

- **`RETURNING`** gives back the new auto-generated ID in the same statement,
  with no second query needed.
- `pinned`, `created_at` and `updated_at` are filled in by their **column
  defaults**.

### 3.3 `list_conversations(agent_id)`

```sql
SELECT conversation_id, title, pinned, updated_at
FROM conversations
WHERE agent_id = %s
ORDER BY pinned DESC, updated_at DESC
```

- **Two-level sort:** pinned threads first (`TRUE` sorts before `FALSE` with
  `DESC`), then the most recently used.
- The index `idx_conversations_agent (agent_id, pinned, updated_at)` matches
  this query's filter and sort columns.
- `app.py` groups the results into Pinned / Today / Previous 7 days / Older
  with its `bucket()` helper.

### 3.4 `rename_conversation(cid, title)`

```sql
UPDATE conversations SET title = %s WHERE conversation_id = %s
```

A manual rename from the ⋯ menu. An empty title falls back to "New chat"
(handled in `app.py`).

### 3.5 `autoname_conversation(cid, text)`: conditional update

```python
title = (text[:40] + "…") if len(text) > 40 else text
```
```sql
UPDATE conversations SET title = %s
WHERE conversation_id = %s AND title = 'New chat'
```

- Names the thread after the **first message**, cut to 40 characters.
- **`AND title = 'New chat'`** makes it run only while the title is still the
  default. On later messages, or after a manual rename, the WHERE matches
  **0 rows** and nothing changes.
- The condition lives in SQL, so Python never needs to read the title
  first: **one query instead of two**.

### 3.6 `set_pinned(cid, pinned)`

```sql
UPDATE conversations SET pinned = %s WHERE conversation_id = %s
```

`app.py` passes `not pinned`, so the same button toggles.

### 3.7 `delete_conversation(cid)`: ON DELETE CASCADE

```sql
DELETE FROM conversations WHERE conversation_id = %s
```

- Only one DELETE in Python: the database **automatically deletes the
  thread's messages** because of `ON DELETE CASCADE`.
- **Memories are kept**, because they belong to the agent, not the thread.
  A memory's `source_message_id` FK is `ON DELETE SET NULL`, so it simply
  loses its link to the deleted message.

### 3.8 `load_chat(conversation_id)`

```sql
SELECT role, content FROM messages
WHERE conversation_id = %s
ORDER BY message_id ASC
```

- Returns the open thread's messages, **oldest first**:
  `[(role, content), ...]`.
- `message_id` is SERIAL (always increasing), so sorting by it gives the
  order the messages were sent.
- `idx_messages_conversation` makes the `WHERE conversation_id = ...` lookup
  fast.

---

## 4. How `updated_at` stays current

`conversations.py` never updates `updated_at` itself. **`memory.log_message()`**
does it in the **same transaction** as inserting the message:

```sql
INSERT INTO messages (...) VALUES (...) RETURNING message_id;
UPDATE conversations SET updated_at = now() WHERE conversation_id = %s;
-- one commit
```

So every new message moves its thread to the top of the sidebar.

---

## 5. Flow in the app

```
open app      -> list_agents_with_counts()  -> sidebar agents (with counts)
pick agent    -> list_conversations()       -> sidebar threads (pinned, recent)
              -> none yet? create_conversation()
open thread   -> load_chat()                -> messages shown
send message  -> autoname_conversation()    -> title from 1st message
              -> log_message()              -> bumps updated_at
⋯ menu        -> rename / set_pinned / delete_conversation
```

---

## 6. DBMS concepts in this file

| Concept | Where |
|---|---|
| One-to-many relationship | agent → conversations → messages |
| Foreign key + `ON DELETE CASCADE` | delete agent → threads → messages |
| `LEFT JOIN` (outer join) | `list_agents_with_counts` |
| Aggregate + `GROUP BY` | `COUNT(c.conversation_id)` |
| Functional dependency on the PK | selecting name/description while grouping by `agent_id` |
| `COUNT(col)` vs `COUNT(*)` with NULLs | gives 0 for agents without threads |
| `RETURNING` | `create_conversation` |
| Column `DEFAULT`s | `title`, `pinned`, `created_at`, `updated_at` |
| Multi-column `ORDER BY` | pinned first, then most recent |
| Composite B-tree index | `idx_conversations_agent (agent_id, pinned, updated_at)` |
| Conditional UPDATE | `autoname_conversation` (`AND title = 'New chat'`) |
| Parameterized queries | every `%s` |

---

## 7. Viva questions

- **Why separate conversations from memories?** Threads organise chat;
  memory is long-term knowledge about the user. Deleting a chat should not
  make the agent forget facts.
- **Why LEFT JOIN in `list_agents_with_counts`?** So agents with zero
  threads still appear, with a count of 0.
- **Why `COUNT(c.conversation_id)` instead of `COUNT(*)`?** `COUNT(*)`
  counts the NULL row from the LEFT JOIN and would show 1 instead of 0.
- **How can you SELECT name without putting it in GROUP BY?** `agent_id` is
  the primary key, so name is functionally dependent on it; PostgreSQL allows
  this.
- **What does ON DELETE CASCADE do here?** Deleting a thread deletes its
  messages automatically; deleting an agent deletes its threads, and those
  delete their messages.
- **CASCADE vs SET NULL?** Messages can't exist without their thread, so they
  CASCADE. Memories should survive, so `source_message_id` is SET NULL.
- **Why put the 'New chat' check inside the UPDATE?** One query instead of a
  read followed by a write, and a manual rename is never overwritten.
- **What do the indexes do?** `idx_conversations_agent` matches the sidebar
  query's filter and sort; `idx_messages_conversation` speeds up loading a
  thread.
- **What does `RETURNING` do?** It gives back the generated ID from the
  INSERT itself, with no second query.
- **What is the limitation?** `created_at`/`updated_at` are `TIMESTAMP`
  without a time zone (unlike `valid_until`). That works because they're only
  used for sorting, but `TIMESTAMPTZ` would be more correct.
