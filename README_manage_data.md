# pages/1_Manage_Data.py — The CRUD Management Page

This is the **admin page** of the app. It lets you **see and change what is
stored in the database** directly: memories, the raw message log, the
trigger-written audit log, and agents. It is the page to open in the viva to
**show all four CRUD operations** and the effect of the **triggers**.

Like `app.py`, it contains **no SQL**: it only calls functions in `memory.py`.

---

## 1. How the page appears

- Streamlit treats every `.py` file in the **`pages/`** folder as an extra
  page of a **multipage app**.
- The `1_` prefix sets its order in the page list. The name shown is
  "Manage Data".
- It is opened from the **user card → Manage data** link in the chat
  sidebar, and `st.page_link("app.py", ...)` at the top goes **back to the
  chat**.

---

## 2. Page setup (lines 6–23)

```python
from memory import (list_memories, update_memory, delete_memory,
                    list_agents, create_agent, update_agent, delete_agent,
                    load_messages, list_audit)

agents = list_agents()
by_name = {a[1]: a for a in agents}          # "Study Buddy" -> (id, name, desc)

tab_mem, tab_msg, tab_audit, tab_agent = st.tabs(
    ["Memories", "Messages", "Audit log", "Agents"])
```

- One query loads all agents. `by_name` maps the name chosen in a dropdown
  back to its `agent_id`.
- If there are no agents yet, a warning is shown and only the **Agents** tab
  is usable (so you can create one).

| Tab | Operations | Table |
|---|---|---|
| Memories | **R**ead, **U**pdate, **D**elete | `memories` |
| Messages | **R**ead | `messages` |
| Audit log | **R**ead | `memory_audit` (written by a trigger) |
| Agents | **C**reate, **R**ead, **U**pdate, **D**elete | `agents` |

Memories are **created** from the chat, not here, because a new memory needs
the full pipeline: fact check, conflict check, embedding and expiry.

---

## 3. Memories tab: Read / Update / Delete (lines 28–78)

### 3.1 Read

```python
who = st.selectbox("Show memories for", list(by_name.keys()))
show_all = st.checkbox("Include superseded (retired) memories", value=False)
rows = list_memories(agent_id, include_superseded=show_all)
```

- `list_memories` returns this agent's **private** memories plus **all
  shared** ones (`WHERE scope = 'shared' OR agent_id = %s`).
- By default only **active** memories are shown. Ticking the checkbox also
  shows **retired** (superseded) ones, so you can **demonstrate that old
  versions are kept, not deleted**.

Each memory is an expander whose header summarises it:

```
#12 · private · p5 · used 3× · ~~retired~~ · expired
```

| Part | Meaning |
|---|---|
| `#12` | `memory_id` |
| `private` / `shared` | `scope` |
| `p5` | priority (1–5) |
| `used 3×` | `access_count`: how often it was recalled or restated (reinforcement) |
| `~~retired~~` | `superseded = TRUE` |
| `expired` | `valid_until` is in the past |

**Provenance:** if the memory has a `source_message_id`, the expander shows
*"from message #N"*: the FK link back to the message that created it.

**Time zones:**
```python
now = _dt.datetime.now().astimezone()      # time-zone-aware "now"
valid_until = valid_until.astimezone()     # TIMESTAMPTZ -> shown in local (IST) time
```
`valid_until` is a **TIMESTAMPTZ**, so it comes back time-zone-aware. Both
sides of the comparison must be aware: Python refuses to compare aware and
naive datetimes.

### 3.2 Update

Inside each expander:

| Widget | Edits |
|---|---|
| Content (text area) | `content`; the memory is **re-embedded** when the text changes |
| Priority (slider 1–5) | `priority`; the slider range matches the DB **CHECK** constraint |
| Has expiry date (checkbox) + **Expires on** (date picker) | `valid_until` |

```python
new_exp = _dt.datetime.combine(d, _dt.time(23, 59, 59)).astimezone()
...
if has_exp:
    update_memory(mid, content=new_text, priority=new_pri, valid_until=new_exp)
else:
    update_memory(mid, content=new_text, priority=new_pri, clear_expiry=True)
```

- The chosen date becomes **23:59:59 local time**, so the fact is valid
  through that whole day, the same rule as the automatic `extract_expiry`.
- Unticking "Has expiry date" **removes** the expiry (`valid_until = NULL`).
- **Restoring:** if a memory was retired by **Purge expired**, giving it a
  **future** date (or removing the expiry) **un-retires** it. The logic is in
  `memory.update_memory`, and the trigger logs it as **RESTORE**.
- Any change to content or priority fires **`trg_memory_audit`**, which logs
  an **UPDATE** row. Python writes nothing to the audit table itself.

### 3.3 Delete

```python
if c2.button("Delete", ...):
    delete_memory(mid)
```

- `delete_memory` first clears any `superseded_by` references to this row
  (the **self-referencing FK** would otherwise block the delete), then runs
  `DELETE`.
- The row is gone, but **`trg_memory_audit` has already logged a DELETE row
  with the old content**, so the history survives. This is a **hard
  delete**, unlike superseding, which is a soft delete.

---

## 4. Messages tab: Read (lines 83–93)

```python
msgs = load_messages(aid_m)       # SELECT role, content ... ORDER BY message_id
```

- Shows the agent's **full raw transcript** across all threads, oldest
  first, drawn as chat bubbles.
- **Purpose in the viva:** compare it with the Memories tab. Messages hold
  **everything** (questions, greetings, replies); memories hold only the
  **facts** that passed `is_fact()`. That's the difference between a **log**
  and a **knowledge store**.

---

## 5. Audit log tab: Read (lines 98–117)

```python
entries = list_audit(aid_a)
```

- Reads `memory_audit`, newest first, for this agent. It also includes
  entries with `agent_id IS NULL`, which are **shared** memories.
- Every row was written by the **`trg_memory_audit` trigger**. The app never
  inserts into this table.
- Each entry: `#audit_id · OPERATION on memory #id · date time`.

| Operation | What is shown |
|---|---|
| `DELETE` | The deleted content and its priority at deletion |
| `UPDATE` | Content `~~old~~ → new` and/or priority `old → new` |
| `SUPERSEDE` | "Retired: ..." (plus the priority drop to 1) |
| `RESTORE` | "Restored: ..." |

**Key point:** the audit table has **no foreign key** to `memories`, so
entries for **deleted** memories remain. The caption on the page says this.

---

## 6. Agents tab: Create / Read / Update / Delete (lines 122–148)

| Op | UI | Calls | SQL |
|---|---|---|---|
| **Create** | Name + Description → **Create agent** | `create_agent(n, d)` | `INSERT ... RETURNING agent_id` |
| **Read** | One expander per agent: `#id · name` | `list_agents()` | `SELECT ... ORDER BY agent_id` |
| **Update** | Edit fields → **Save** | `update_agent(aid, name, desc)` | `UPDATE agents SET ...` |
| **Delete** | **Delete agent** | `delete_agent(aid)` | several statements, one transaction |

- **Validation:** an empty name is rejected (`if n.strip()`), and spaces are
  trimmed before saving.
- The **description** becomes part of the persona prompt
  (`"You are {name}. {description}."`), so editing it **changes how the
  agent replies**. No code change is needed to add or change an agent.

**What `delete_agent` removes (one transaction):**

```sql
UPDATE memories SET superseded_by = NULL
 WHERE superseded_by IN (SELECT memory_id FROM memories WHERE agent_id = %s);  -- clear self-FK refs
DELETE FROM memories WHERE agent_id = %s;   -- its private memories (audit logs each one)
DELETE FROM messages WHERE agent_id = %s;   -- its messages
DELETE FROM agents   WHERE agent_id = %s;   -- the agent; ON DELETE CASCADE removes its conversations
```

- **Shared memories are kept**: they have `agent_id = NULL` and belong to
  every agent.
- One commit at the end: either everything is deleted or nothing is
  (**atomicity**).

---

## 7. Streamlit details used here

- **Unique widget keys** (`key=f"txt_{mid}"`, `f"del_{mid}"`, ...): the same
  widgets repeat for every row, and Streamlit needs a distinct key for each
  one.
- **`st.rerun()`** after every save or delete redraws the page with fresh
  data from the database.
- **`st.tabs`** keeps the four sections on one page.

---

## 8. Demo script for the viva (about 1 minute)

1. **Chat:** tell the agent "I live in Mumbai", then "I live in Pune now",
   and confirm the conflict.
2. **Memories tab:** tick **Include superseded**. "I live in Mumbai" shows
   `~~retired~~`, and "I live in Pune now" is active with *from message #N*.
3. **Audit log tab:** a **SUPERSEDE** row exists, written by the trigger,
   not by Python.
4. **Memories tab:** change a priority and **Save**. The Audit log gets an
   **UPDATE** row (`3 → 5`).
5. **Delete** a memory. It disappears, but the Audit log still shows a
   **DELETE** row with its content.
6. **Messages tab:** the full transcript, including questions that were
   **not** stored as memories.

---

## 9. DBMS concepts shown on this page

| Concept | Where |
|---|---|
| All four CRUD operations | Agents tab (C/R/U/D), Memories tab (R/U/D) |
| Triggers | Audit log tab: every row comes from `trg_memory_audit` |
| Soft delete vs hard delete | "Include superseded" (soft) vs **Delete** (hard, still audited) |
| Referential integrity | Self-FK cleared before delete; provenance link "from message #N" |
| Domain integrity | Priority slider 1–5 matches the CHECK constraint |
| Temporal data (valid time) | Expiry date, the expired tag, restore on extend |
| TIMESTAMPTZ | Converting to local time for display and comparison |
| Transactions | `delete_agent` removes everything in one commit |
| ON DELETE CASCADE | Deleting an agent removes its conversations |
| Log vs knowledge base | Messages tab vs Memories tab |

---

## 10. Viva questions

- **Why can't memories be created on this page?** Creating a memory needs the
  full pipeline (fact check, conflict detection, embedding, expiry), which
  runs from the chat. This page handles reads, edits and deletes.
- **How do you prove old facts are kept?** Tick "Include superseded": retired
  memories still exist, marked `retired`.
- **Who writes the audit log?** The `trg_memory_audit` trigger, automatically.
  Python only reads it.
- **Why does the audit log still show deleted memories?** It has no FK to
  `memories`, so its rows outlive the memory they describe.
- **What happens to shared memories when an agent is deleted?** They're kept,
  because they have `agent_id = NULL` and belong to all agents.
- **Why `.astimezone()`?** `valid_until` is TIMESTAMPTZ (aware); Python can't
  compare aware and naive datetimes, and users should see IST, not UTC.
- **Why does editing the text re-embed the memory?** The embedding must match
  the text, or retrieval and conflict detection would use the old meaning.
- **What are the limitations?**
  - Deleting an agent has no confirmation step (unlike **Clear all data** in
    the chat).
  - Agents are looked up by name in the dropdowns, so two agents with the
    same name would clash; a UNIQUE constraint on `agents.name` would
    prevent it.
  - There is no login or access control: the page is for a single user.
