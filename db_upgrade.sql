-- ============================================================================
--  db_upgrade.sql
--  Adds TRIGGERS, PROCEDURES/FUNCTIONS, and INTEGRITY CONSTRAINTS on top of the
--  existing agents / memories / messages schema.
--
--  HOW TO RUN (once):
--    Neon Console -> your project -> "SQL Editor" -> paste this whole file -> Run.
--  It is idempotent: safe to run more than once (uses CREATE OR REPLACE /
--  IF NOT EXISTS / DROP-then-ADD / guarded DO blocks everywhere).
-- ============================================================================


-- ============================================================================
--  PART 1 — REFERENTIAL & DOMAIN INTEGRITY
-- ============================================================================
-- 1a. PROVENANCE LINK (referential integrity)
--     A memory's text starts as a copy of a message's text. The copy is kept on
--     purpose — memories are edited and re-embedded independently of the
--     message — so instead we add a foreign key linking every memory back to
--     the message that created it. This lets the app answer "why do you
--     remember this?".
--     ON DELETE SET NULL: deleting a message must not delete the memory.
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS source_message_id INTEGER
    REFERENCES messages(message_id) ON DELETE SET NULL;

-- 1b. DOMAIN CONSTRAINTS (data integrity)
--     `scope` and `role` were free TEXT columns — any typo would be accepted.
--     These CHECK constraints restrict them to their valid domain, and clamp
--     priority to the 1..5 range the app assumes everywhere.
--     (A CHECK is the right tool here — cheaper and clearer than a trigger.)
ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_scope;
ALTER TABLE memories ADD  CONSTRAINT chk_scope    CHECK (scope IN ('private', 'shared'));

ALTER TABLE memories DROP CONSTRAINT IF EXISTS chk_priority;
ALTER TABLE memories ADD  CONSTRAINT chk_priority CHECK (priority BETWEEN 1 AND 5);

ALTER TABLE messages DROP CONSTRAINT IF EXISTS chk_role;
ALTER TABLE messages ADD  CONSTRAINT chk_role     CHECK (role IN ('user', 'agent'));

-- 1c. TIME-ZONE-AWARE EXPIRY
--     valid_until was TIMESTAMP (no zone) but is compared with now(), which is
--     TIMESTAMPTZ; Postgres converts using the session zone (UTC on Neon), so a
--     date written as local (IST) end-of-day expired 5.5 h late. TIMESTAMPTZ
--     stores an absolute instant. Existing values were written in local time,
--     so they are reinterpreted as Asia/Kolkata. Guarded so it runs only once.
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


-- ============================================================================
--  PART 2 — TRIGGERS
-- ============================================================================
-- 2a. AUDIT LOG
--     A permanent record of every meaningful change to a memory. Unlike the
--     superseded_* columns (which vanish on a hard DELETE), this table survives
--     deletion, so you have a true, tamper-evident history. It deliberately has
--     NO foreign key to memories — the log must outlive the row it describes.
CREATE TABLE IF NOT EXISTS memory_audit (
    audit_id     SERIAL PRIMARY KEY,
    memory_id    INTEGER,                 -- no FK on purpose: log outlives the row
    agent_id     INTEGER,
    operation    TEXT NOT NULL,           -- 'UPDATE', 'SUPERSEDE', 'RESTORE' or 'DELETE'
    old_content  TEXT,
    new_content  TEXT,
    old_priority INTEGER,
    new_priority INTEGER,
    changed_at   TIMESTAMP NOT NULL DEFAULT now()
);

-- The trigger FUNCTION. Note the filter on UPDATE: retrieval bumps access_count
-- on every recall, and we do NOT want an audit row for each read — only for
-- real changes to content, priority, or the superseded flag.
-- A change to the superseded flag is logged as its own operation (SUPERSEDE /
-- RESTORE), because content and priority may be unchanged in that case and an
-- 'UPDATE' row would show no visible difference.
CREATE OR REPLACE FUNCTION log_memory_change() RETURNS TRIGGER AS $$
DECLARE
    op TEXT;
BEGIN
    IF (TG_OP = 'DELETE') THEN
        INSERT INTO memory_audit(memory_id, agent_id, operation,
                                 old_content, old_priority)
        VALUES (OLD.memory_id, OLD.agent_id, 'DELETE', OLD.content, OLD.priority);
        RETURN OLD;

    ELSIF (TG_OP = 'UPDATE') THEN
        IF (OLD.superseded IS DISTINCT FROM NEW.superseded) THEN
            op := CASE WHEN NEW.superseded THEN 'SUPERSEDE' ELSE 'RESTORE' END;
        ELSIF (OLD.content  IS DISTINCT FROM NEW.content
            OR OLD.priority IS DISTINCT FROM NEW.priority) THEN
            op := 'UPDATE';
        END IF;

        IF op IS NOT NULL THEN
            INSERT INTO memory_audit(memory_id, agent_id, operation,
                                     old_content, new_content,
                                     old_priority, new_priority)
            VALUES (NEW.memory_id, NEW.agent_id, op,
                    OLD.content, NEW.content, OLD.priority, NEW.priority);
        END IF;
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_memory_audit ON memories;
CREATE TRIGGER trg_memory_audit
    AFTER UPDATE OR DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION log_memory_change();


-- 2b. SUPERSEDE GUARD
--     Enforces three invariants at the data layer, so no code path can break them:
--       (1) a memory can never supersede itself (that would corrupt the
--           self-join in version_history).
--       (2) whenever a row is newly retired, superseded_at is stamped
--           automatically even if the caller forgot to set it.
--       (3) whenever a row is restored, superseded_at is cleared, so a later
--           retirement gets a fresh timestamp.
CREATE OR REPLACE FUNCTION guard_supersede() RETURNS TRIGGER AS $$
BEGIN
    IF (NEW.superseded_by IS NOT NULL AND NEW.superseded_by = NEW.memory_id) THEN
        RAISE EXCEPTION 'memory % cannot supersede itself', NEW.memory_id;
    END IF;

    IF (NEW.superseded = TRUE AND OLD.superseded = FALSE
        AND NEW.superseded_at IS NULL) THEN
        NEW.superseded_at := now();
    END IF;

    IF (NEW.superseded = FALSE AND OLD.superseded = TRUE) THEN
        NEW.superseded_at := NULL;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_guard_supersede ON memories;
CREATE TRIGGER trg_guard_supersede
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION guard_supersede();


-- ============================================================================
--  PART 3 — PROCEDURES / FUNCTIONS
-- ============================================================================
-- 3a. STATISTICS FUNCTION
--     One-row summary for an agent's memory store (active / retired / expired /
--     average priority / total recalls). Feeds the sidebar + Manage page.
CREATE OR REPLACE FUNCTION fn_memory_stats(p_agent_id INTEGER)
RETURNS TABLE (
    total_active     BIGINT,
    total_superseded BIGINT,
    total_expired    BIGINT,
    avg_priority     NUMERIC,
    total_recall     BIGINT
) AS $$
    SELECT
        COUNT(*) FILTER (WHERE superseded = FALSE
                           AND (valid_until IS NULL OR valid_until > now())),
        COUNT(*) FILTER (WHERE superseded = TRUE),
        COUNT(*) FILTER (WHERE superseded = FALSE
                           AND valid_until IS NOT NULL AND valid_until <= now()),
        ROUND(AVG(priority) FILTER (WHERE superseded = FALSE), 2),
        COALESCE(SUM(access_count), 0)
    FROM memories
    WHERE (scope = 'shared' OR agent_id = p_agent_id);
$$ LANGUAGE sql STABLE;


-- 3b. SUPERSEDE PROCEDURE
--     Bundles the "resolve a conflict" logic in one place inside the database:
--     retire the contradicted facts AND demote the ones the user kept.
--     Atomicity comes from the caller's transaction: the app inserts the new
--     memory and CALLs this procedure on the same connection with a single
--     COMMIT (memory.resolve_conflict), so the insert, the retirements and
--     the demotions all succeed or all roll back.
--     (superseded_at is filled in by the guard trigger above.)
CREATE OR REPLACE PROCEDURE sp_supersede(
    p_retire_ids INTEGER[],
    p_new_id     INTEGER,
    p_demote_ids INTEGER[]
) AS $$
BEGIN
    IF p_retire_ids IS NOT NULL AND array_length(p_retire_ids, 1) IS NOT NULL THEN
        UPDATE memories
           SET superseded    = TRUE,
               superseded_by = p_new_id,
               priority      = 1
         WHERE memory_id = ANY(p_retire_ids);
    END IF;

    IF p_demote_ids IS NOT NULL AND array_length(p_demote_ids, 1) IS NOT NULL THEN
        UPDATE memories
           SET priority = GREATEST(priority - 2, 1)
         WHERE memory_id = ANY(p_demote_ids);
    END IF;
END;
$$ LANGUAGE plpgsql;


-- 3c. PURGE-EXPIRED FUNCTION (housekeeping)
--     Retires (does not delete) every active memory whose valid_until has
--     passed, and returns how many were retired. Call it from a button, or
--     schedule it with pg_cron. Rows are kept for history, just excluded from
--     retrieval — consistent with how supersession already works.
--     Extending a purged memory's expiry on the Manage page restores it
--     (memory.update_memory).
CREATE OR REPLACE FUNCTION fn_purge_expired() RETURNS INTEGER AS $$
DECLARE
    n INTEGER;
BEGIN
    UPDATE memories
       SET superseded    = TRUE,
           superseded_by = NULL,
           priority      = 1
     WHERE superseded = FALSE
       AND valid_until IS NOT NULL
       AND valid_until <= now();
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
--  Quick verification (optional — run these to confirm everything is in place)
-- ============================================================================
-- SELECT tgname FROM pg_trigger WHERE tgrelid = 'memories'::regclass AND NOT tgisinternal;
-- SELECT proname FROM pg_proc WHERE proname IN
--        ('log_memory_change','guard_supersede','fn_memory_stats','sp_supersede','fn_purge_expired');
-- SELECT data_type FROM information_schema.columns
--  WHERE table_name = 'memories' AND column_name = 'valid_until';   -- timestamp with time zone
-- SELECT * FROM fn_memory_stats(1);
