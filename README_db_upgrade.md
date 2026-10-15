# db_upgrade.sql — Constraints, Triggers, Procedure and Functions

`db_upgrade.sql` adds the **database-side logic** on top of the base tables
(`agents`, `messages`, `memories`, `conversations`). It was run once in the
**Neon SQL Editor**.

It is **idempotent** (safe to run more than once) because it uses
`IF NOT EXISTS`, `CREATE OR REPLACE`, `DROP ... IF EXISTS` and guarded
`DO` blocks.

| Part | Objects |
|---|---|
| 1. Integrity | FK `source_message_id`, CHECK constraints, `valid_until` changed to TIMESTAMPTZ |
| 2. Triggers | `trg_memory_audit` (+ table `memory_audit`), `trg_guard_supersede` |
| 3. Procedure / functions | `sp_supersede`, `fn_memory_stats`, `fn_purge_expired` |

**Idea behind it:** rules that must *always* hold are enforced **inside the
database**, so no code path (the app, the SQL editor, a future script) can
break them.

---

## PART 1 — Integrity

### 1a. Provenance foreign key (referential integrity)

```sql
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_message_id INTEGER
    REFERENCES messages(message_id) ON DELETE SET NULL;
```

- Links every memory to the **message it came from**, which answers "why do
  you remember this?".
- `REFERENCES` means the value must be an existing `message_id`, or NULL.
- `ON DELETE SET NULL`: deleting a message **keeps** the memory and only
  clears the link. (`CASCADE` would wrongly delete the memory too.)
- The text is copied into `memories.content` on purpose: memories are edited
  and re-embedded independently, and the FK keeps the relationship.

### 1b. CHECK constraints (domain integrity)

```sql
ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_scope;
ALTER TABLE memories ADD  CONSTRAINT chk_scope    CHECK (scope IN ('private', 'shared'));
ALTER TABLE memories ADD  CONSTRAINT chk_priority CHECK (priority BETWEEN 1 AND 5);
ALTER TABLE messages ADD  CONSTRAINT chk_role     CHECK (role IN ('user', 'agent'));
```

- `scope` and `role` were free TEXT, so a typo like `'privte'` would have
  been accepted. Now the DB rejects it.
- `priority` is limited to the 1–5 range the app assumes.
- **DROP then ADD** makes the script safe to re-run (Postgres has no
  `ADD CONSTRAINT IF NOT EXISTS`).
- A CHECK is the simplest, fastest tool for this. A trigger would be overkill.

### 1c. Time-zone-aware expiry

```sql
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'memories'
           AND column_name = 'valid_until') = 'timestamp without time zone' THEN
        ALTER TABLE memories
            ALTER COLUMN valid_until TYPE TIMESTAMPTZ
            USING valid_until AT TIME ZONE 'Asia/Kolkata';
    END IF;
END $$;
```

- **The bug:** `valid_until` was `TIMESTAMP` (no zone) but was compared with
  `now()` (which has a zone). Neon runs in **UTC**, so an IST end-of-day
  expired **5.5 hours late**.
- **The fix:** `TIMESTAMPTZ` stores an absolute instant.
- `USING ... AT TIME ZONE 'Asia/Kolkata'` converts the existing values, which
  were written in IST.
- `DO $$ ... $$` is an **anonymous PL/pgSQL block**. It reads
  `information_schema` (the system catalog) and converts the column **only
  if it hasn't been converted yet**, so a re-run won't shift the times again.

---

## PART 2 — Triggers

A **trigger** is a function the DB runs **automatically** when a row is
inserted, updated or deleted.

| | BEFORE trigger | AFTER trigger |
|---|---|---|
| Runs | before the row is written | after the row is written |
| Can change `NEW` | yes | no (too late) |
| Good for | validation, filling in values | logging, auditing |

Inside a trigger function:
- `OLD` is the row before the change; `NEW` is the row after it.
- `TG_OP` is `'INSERT'`, `'UPDATE'` or `'DELETE'`.

### 2a. Audit log: table `memory_audit` + trigger `trg_memory_audit`

```sql
CREATE TABLE IF NOT EXISTS memory_audit (
    audit_id     SERIAL PRIMARY KEY,
    memory_id    INTEGER,          -- no FK on purpose
    agent_id     INTEGER,
    operation    TEXT NOT NULL,    -- 'UPDATE' | 'SUPERSEDE' | 'RESTORE' | 'DELETE'
    old_content  TEXT,  new_content  TEXT,
    old_priority INTEGER, new_priority INTEGER,
    changed_at   TIMESTAMP NOT NULL DEFAULT now()
);
```

- **No FK to memories, on purpose:** the log must **outlive** the row it
  describes. An FK would block the delete or erase the history.

```sql
CREATE OR REPLACE FUNCTION log_memory_change() RETURNS TRIGGER AS $$
DECLARE op TEXT;
BEGIN
    IF (TG_OP = 'DELETE') THEN
        INSERT INTO memory_audit(memory_id, agent_id, operation, old_content, old_priority)
        VALUES (OLD.memory_id, OLD.agent_id, 'DELETE', OLD.content, OLD.priority);
        RETURN OLD;
    ELSIF (TG_OP = 'UPDATE') THEN
        IF (OLD.superseded IS DISTINCT FROM NEW.superseded) THEN
            op := CASE WHEN NEW.superseded THEN 'SUPERSEDE' ELSE 'RESTORE' END;
        ELSIF (OLD.content IS DISTINCT FROM NEW.content
            OR OLD.priority IS DISTINCT FROM NEW.priority) THEN
            op := 'UPDATE';
        END IF;
        IF op IS NOT NULL THEN
            INSERT INTO memory_audit(...) VALUES (... OLD.content, NEW.content, ...);
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memory_audit
    AFTER UPDATE OR DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION log_memory_change();
```

| Change | Logged as |
|---|---|
| Row deleted | `DELETE` (old content saved) |
| `superseded` FALSE → TRUE | `SUPERSEDE` |
| `superseded` TRUE → FALSE | `RESTORE` |
| `content` or `priority` changed | `UPDATE` (old and new values) |
| Only `access_count` / `last_accessed` changed | **not logged** |

- **Why skip `access_count`?** Every retrieval bumps it. Logging each read
  would flood the table with useless rows.
- **`IS DISTINCT FROM`** is a NULL-safe "not equal". `NULL <> 'x'` gives
  NULL, which counts as false, so a plain `<>` would miss changes to or from
  NULL.
- **`AFTER` trigger:** it only records what already happened.
- **`FOR EACH ROW`:** one audit row per changed memory, even when one UPDATE
  touches many rows.

### 2b. Supersede guard: trigger `trg_guard_supersede`

```sql
CREATE OR REPLACE FUNCTION guard_supersede() RETURNS TRIGGER AS $$
BEGIN
    IF (NEW.superseded_by IS NOT NULL AND NEW.superseded_by = NEW.memory_id) THEN
        RAISE EXCEPTION 'memory % cannot supersede itself', NEW.memory_id;
    END IF;
    IF (NEW.superseded = TRUE AND OLD.superseded = FALSE AND NEW.superseded_at IS NULL) THEN
        NEW.superseded_at := now();
    END IF;
    IF (NEW.superseded = FALSE AND OLD.superseded = TRUE) THEN
        NEW.superseded_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_guard_supersede
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION guard_supersede();
```

It enforces three rules:
1. **A memory cannot supersede itself.** That would corrupt the self-join
   in `version_history`. `RAISE EXCEPTION` aborts the whole transaction.
2. **When a memory is retired**, `superseded_at` is stamped automatically,
   even if the caller forgot.
3. **When it is restored**, `superseded_at` is cleared, so a later
   retirement gets a fresh time.

- It must be a **`BEFORE`** trigger, because it **modifies `NEW`**.

---

## PART 3 — Procedure and functions

| | PROCEDURE | FUNCTION |
|---|---|---|
| Run with | `CALL sp_x(...)` | `SELECT fn_x(...)` |
| Returns | nothing | a value or a table |
| Usable inside a query | no | yes |

### 3a. `fn_memory_stats(p_agent_id)`: statistics function

```sql
CREATE OR REPLACE FUNCTION fn_memory_stats(p_agent_id INTEGER)
RETURNS TABLE (total_active BIGINT, total_superseded BIGINT, total_expired BIGINT,
               avg_priority NUMERIC, total_recall BIGINT) AS $$
    SELECT
        COUNT(*) FILTER (WHERE superseded = FALSE AND (valid_until IS NULL OR valid_until > now())),
        COUNT(*) FILTER (WHERE superseded = TRUE),
        COUNT(*) FILTER (WHERE superseded = FALSE AND valid_until IS NOT NULL AND valid_until <= now()),
        ROUND(AVG(priority) FILTER (WHERE superseded = FALSE), 2),
        COALESCE(SUM(access_count), 0)
    FROM memories
    WHERE (scope = 'shared' OR agent_id = p_agent_id);
$$ LANGUAGE sql STABLE;
```

- Returns **one row with 5 numbers** for the dashboard.
- **`COUNT(*) FILTER (WHERE ...)`** computes several conditional counts in
  **one table scan**, instead of five separate queries.
- `COALESCE(..., 0)` turns NULL (no rows) into 0.
- `RETURNS TABLE`: the function returns rows, so it is called with
  `SELECT * FROM fn_memory_stats(1)`.
- `LANGUAGE sql`: a plain SQL body, no PL/pgSQL needed.
- `STABLE`: it only reads data, so Postgres can optimize the call.
- Called from `memory.memory_stats()`.

### 3b. `sp_supersede(retire[], new_id, demote[])`: stored procedure

```sql
CREATE OR REPLACE PROCEDURE sp_supersede(
    p_retire_ids INTEGER[], p_new_id INTEGER, p_demote_ids INTEGER[]) AS $$
BEGIN
    IF p_retire_ids IS NOT NULL AND array_length(p_retire_ids, 1) IS NOT NULL THEN
        UPDATE memories
           SET superseded = TRUE, superseded_by = p_new_id, priority = 1
         WHERE memory_id = ANY(p_retire_ids);
    END IF;
    IF p_demote_ids IS NOT NULL AND array_length(p_demote_ids, 1) IS NOT NULL THEN
        UPDATE memories
           SET priority = GREATEST(priority - 2, 1)
         WHERE memory_id = ANY(p_demote_ids);
    END IF;
END;
$$ LANGUAGE plpgsql;
```

- **Retire:** the facts the user marked as no longer true are superseded,
  linked to the new fact, and set to the lowest priority.
- **Demote:** the facts the user kept ("both are true") lose 2 priority
  points, but never drop below 1 (`GREATEST`).
- `INTEGER[]` arrays + `= ANY(...)` handle any number of IDs in **one
  UPDATE**.
- `array_length(..., 1) IS NOT NULL` skips empty arrays.
- **Atomicity:** Python (`resolve_conflict`) inserts the new memory **and**
  CALLs this procedure on one connection with **one COMMIT**. If anything
  fails, all of it rolls back.
- Each UPDATE here also fires both triggers: the guard stamps
  `superseded_at`, and the audit logs `SUPERSEDE`/`UPDATE`.

### 3c. `fn_purge_expired()`: housekeeping function

```sql
CREATE OR REPLACE FUNCTION fn_purge_expired() RETURNS INTEGER AS $$
DECLARE n INTEGER;
BEGIN
    UPDATE memories
       SET superseded = TRUE, superseded_by = NULL, priority = 1
     WHERE superseded = FALSE AND valid_until IS NOT NULL AND valid_until <= now();
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;
```

- Retires (**soft-deletes**) every memory whose date has passed. The rows
  stay for history.
- `superseded_by = NULL`: an expired fact has no replacement.
- `GET DIAGNOSTICS n = ROW_COUNT` gets **how many rows the last statement
  changed**, and the function returns it ("Retired 3 memories").
- Run from the **Purge expired now** button; it could be scheduled with the
  `pg_cron` extension.
- Extending a purged memory's expiry on the Manage page **restores** it
  (`memory.update_memory`), and the trigger logs `RESTORE`.

---

## How it all works together (one conflict)

```
User: "I live in Pune now"  (old fact: "I live in Mumbai", id 7)
  Python  INSERT new memory (id 12)            ┐
  Python  CALL sp_supersede([7], 12, NULL)     │ one transaction
    -> UPDATE memory 7                         │
       BEFORE trg_guard_supersede: superseded_at = now()
       AFTER  trg_memory_audit:    audit row 'SUPERSEDE'
  Python  COMMIT                               ┘
version_history():  "I live in Mumbai" -> "I live in Pune now"
```

---

## Verification queries

```sql
-- triggers on memories
SELECT tgname FROM pg_trigger WHERE tgrelid = 'memories'::regclass AND NOT tgisinternal;
-- functions / procedure exist
SELECT proname FROM pg_proc WHERE proname IN
  ('log_memory_change','guard_supersede','fn_memory_stats','sp_supersede','fn_purge_expired');
-- column type is now 'timestamp with time zone'
SELECT data_type FROM information_schema.columns
 WHERE table_name = 'memories' AND column_name = 'valid_until';
SELECT * FROM fn_memory_stats(1);
```

---

## Viva questions

- **Why enforce rules in the DB instead of Python?** Every client is held
  to them: the app, the SQL editor, future code. Python checks can be
  bypassed.
- **CHECK vs trigger?** Use a CHECK for simple per-row rules (cheaper,
  clearer). Use a trigger when you must change values or write to another
  table.
- **BEFORE vs AFTER?** BEFORE can modify `NEW` (the guard fills in
  `superseded_at`). AFTER sees the final row and suits logging (the audit).
- **What are `OLD` and `NEW`?** The row before and after the change. A
  DELETE has only `OLD`; an INSERT has only `NEW`.
- **Procedure vs function?** A procedure is run with `CALL` and returns
  nothing; a function returns a value and can be used inside `SELECT`.
- **Why doesn't `memory_audit` have an FK?** The log must survive the
  deletion of the memory it describes.
- **Why `ON DELETE SET NULL` and not `CASCADE`?** Deleting a chat message
  should not erase a fact the user told us.
- **What does `IS DISTINCT FROM` do?** It compares values with NULL treated
  as a normal value, so changes to or from NULL are detected.
- **What does `FILTER` do?** It applies a condition to one aggregate, so
  several conditional counts come from one scan.
- **What is `GET DIAGNOSTICS ROW_COUNT`?** The number of rows the last
  statement affected.
- **What does `RAISE EXCEPTION` do?** It aborts the statement and rolls back
  the whole transaction.
- **What does idempotent mean here?** Running the script twice gives the same
  result as running it once.
