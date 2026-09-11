"""
Core memory operations for the AI Agent Temporal Memory Management System.

Pipeline on every message:
    1. embed()            -> text into a 384-number meaning vector
    2. find_conflicts()   -> does this contradict anything already stored?
    3. retrieve()         -> top-k relevant, non-superseded memories
    4. assemble_context() -> package everything into a prompt for the LLM

Guarantees:
    * exact duplicates are never stored twice (they reinforce instead)
    * ALL contradictory facts are surfaced, not just the closest one
    * superseded facts are kept for history but excluded from retrieval
"""

from sentence_transformers import SentenceTransformer
from db import get_conn

try:
    from dateparser.search import search_dates
except Exception:      # dateparser optional; manual valid_until still works
    search_dates = None

# Loaded once (downloads ~90 MB the first time, then runs offline & free).
_model = SentenceTransformer("all-MiniLM-L6-v2")

# --- tuning knobs (explain these in your report) ---
CONFLICT_THRESHOLD = 0.60   # above this similarity = probably about the SAME fact
MIN_SIMILARITY     = 0.25   # below this = unrelated, don't retrieve at all

# retrieval score weights: similarity, priority, recency-of-creation, event-urgency
W_SIM, W_PRI, W_RECENCY, W_URGENCY = 1.0, 0.5, 0.3, 0.7


# ---------- EMBEDDING ----------
def embed(text):
    """Text -> list of 384 floats (its meaning-vector)."""
    return _model.encode(text).tolist()


def _to_pgvector(vec):
    """Format a Python list as a pgvector literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(x) for x in vec) + "]"


# ---------- MESSAGES (raw conversation log) ----------
def log_message(agent_id, role, content):
    """CREATE: append one turn to the conversation log."""
    priority = detect_priority(content)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO messages (agent_id, role, content, priority)
                   VALUES (%s, %s, %s, %s)""",
                (agent_id, role, content, priority),
            )
        conn.commit()
    finally:
        conn.close()


def load_messages(agent_id, limit=200):
    """READ: the stored conversation for an agent, oldest first."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT role, content FROM messages
                   WHERE agent_id = %s
                   ORDER BY message_id ASC
                   LIMIT %s""",
                (agent_id, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------- EXPLICIT CRUD (for demonstrating all four operations) ----------
def list_memories(agent_id, include_superseded=False):
    """READ: every memory visible to this agent (private + shared)."""
    sql = """
        SELECT memory_id, scope, content, priority, access_count,
               created_at, superseded, valid_until
        FROM memories
        WHERE (scope = 'shared' OR agent_id = %s)
    """
    if not include_superseded:
        sql += " AND superseded = FALSE"
    sql += " ORDER BY memory_id"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (agent_id,))
            return cur.fetchall()
    finally:
        conn.close()


def update_memory(memory_id, content=None, priority=None, valid_until=..., clear_expiry=False):
    """UPDATE: edit a memory's text, priority, and/or expiry. Re-embeds if text changed."""
    sets, params = [], []
    if content is not None:
        sets.append("content = %s"); params.append(content)
        sets.append("embedding = %s::vector"); params.append(_to_pgvector(embed(content)))
    if priority is not None:
        sets.append("priority = %s"); params.append(int(priority))
    if clear_expiry:
        sets.append("valid_until = NULL")
    elif valid_until is not ...:
        sets.append("valid_until = %s"); params.append(valid_until)
    if not sets:
        return
    params.append(memory_id)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE memories SET {', '.join(sets)} WHERE memory_id = %s", params)
        conn.commit()
    finally:
        conn.close()


def delete_memory(memory_id):
    """DELETE: permanently remove one memory row."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # clear any references so the foreign key doesn't block the delete (referential integrity)
            cur.execute("UPDATE memories SET superseded_by = NULL WHERE superseded_by = %s",
                        (memory_id,))
            cur.execute("DELETE FROM memories WHERE memory_id = %s", (memory_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- AGENT CRUD ----------
def list_agents():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_id, name, description FROM agents ORDER BY agent_id")
            return cur.fetchall()
    finally:
        conn.close()


def create_agent(name, description):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (name, description) VALUES (%s, %s) RETURNING agent_id",
                (name, description),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def update_agent(agent_id, name, description):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE agents SET name = %s, description = %s WHERE agent_id = %s",
                        (name, description, agent_id))
        conn.commit()
    finally:
        conn.close()


def delete_agent(agent_id):
    """Deletes an agent and all of its private memories/messages."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE memories SET superseded_by = NULL WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM memories WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM messages WHERE agent_id = %s", (agent_id,))
            cur.execute("DELETE FROM agents WHERE agent_id = %s", (agent_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- WHAT COUNTS AS A MEMORY ----------
_QUESTION_STARTS = ("what", "when", "where", "who", "whom", "whose", "which",
                    "why", "how", "do ", "does ", "did ", "can ", "could ",
                    "should ", "would ", "is ", "are ", "was ", "were ",
                    "will ", "tell me", "give me", "list ", "show ", "explain")


def is_fact(text):
    """
    Only statements about the user are worth remembering. Questions are
    retrieval cues, not facts — storing them pollutes the memory store and
    creates false contradictions between similarly-worded questions.
    """
    t = text.strip().lower()
    if not t:
        return False
    if t.endswith("?"):
        return False
    if t.startswith(_QUESTION_STARTS):
        return False
    if len(t.split()) < 3:            # "hello", "ok thanks" -> not a fact
        return False
    return True


# ---------- AUTOMATIC PRIORITY (transparent rule-based scorer) ----------
def detect_priority(text):
    """Return 1..5 based on keyword cues. Easy to explain in your report."""
    t = text.lower()
    high = ["important", "remember", "deadline", "always", "never",
            "allergic", "exam", "my name is", "i prefer", "i live"]
    if any(k in t for k in high):
        return 5
    if "?" in text:          # a plain question matters less long-term
        return 2
    return 1


# ---------- DUPLICATE HANDLING ----------
def find_duplicate(agent_id, content):
    """Return memory_id of an identical active memory, or None."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT memory_id FROM memories
                   WHERE (scope = 'shared' OR agent_id = %s)
                     AND superseded = FALSE
                     AND lower(btrim(content)) = lower(btrim(%s))
                   LIMIT 1""",
                (agent_id, content),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def reinforce(memory_id):
    """Re-stating a fact strengthens it instead of duplicating the row."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE memories
                   SET access_count = access_count + 1, last_accessed = NOW()
                   WHERE memory_id = %s""",
                (memory_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------- CONFLICT DETECTION ----------
def find_conflicts(agent_id, content):
    """
    Return EVERY active memory that is about the same thing but says something
    different -> [(memory_id, content, similarity), ...], most similar first.

    Returning all of them (not just the closest) means that when the user says
    "it changed", every stale version is superseded in one go, so contradictions
    cannot silently accumulate.
    """
    vec = _to_pgvector(embed(content))
    sql = """
        SELECT memory_id, content, (1 - (embedding <=> %s::vector)) AS similarity
        FROM memories
        WHERE (scope = 'shared' OR agent_id = %s)
          AND superseded = FALSE
          AND (valid_until IS NULL OR valid_until > NOW())
          AND embedding IS NOT NULL
          AND lower(btrim(content)) <> lower(btrim(%s))
          AND (1 - (embedding <=> %s::vector)) > %s
        ORDER BY similarity DESC
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (vec, agent_id, content, vec, CONFLICT_THRESHOLD))
            return cur.fetchall()
    finally:
        conn.close()


# ---------- EXPIRY (valid-time) ----------
def extract_expiry(text):
    """
    Find a future date mentioned in the text and return it as the memory's
    valid_until. Returns None if no clear date is found (falls back to manual).
    A fact like 'exam on 17th Aug 2026' expires the day AFTER that date.
    """
    if not search_dates:
        return None
    try:
        found = search_dates(
            text,
            settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
        )
    except Exception:
        return None
    if not found:
        return None
    # use the latest date mentioned, valid through the end of that day
    latest = max(dt for _, dt in found)
    return latest.replace(hour=23, minute=59, second=59, microsecond=0)


# ---------- STORE ----------
def store_memory(content, agent_id, scope="private"):
    """
    Save a memory. If an identical one already exists, reinforce it instead of
    inserting a duplicate. Returns (memory_id, was_duplicate).
    """
    dup = find_duplicate(agent_id, content)
    if dup:
        reinforce(dup)
        return dup, True

    vec = _to_pgvector(embed(content))
    aid = None if scope == "shared" else agent_id   # shared memories have NULL agent_id
    priority = detect_priority(content)
    valid_until = extract_expiry(content)           # auto-detected date, or None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO memories (agent_id, scope, content, priority, embedding, valid_until)
                   VALUES (%s, %s, %s, %s, %s::vector, %s)
                   RETURNING memory_id""",
                (aid, scope, content, priority, vec, valid_until),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id, False
    finally:
        conn.close()


# ---------- SUPERSEDE (memory versioning) ----------
def supersede_many(old_ids, new_id=None):
    """
    Mark outdated facts as replaced. Rows are KEPT (so the version history is
    auditable) but excluded from all future retrieval.

    new_id may be None: a fact can be retired without a replacement, e.g. when
    an event is cancelled rather than rescheduled.
    """
    if not old_ids:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE memories
                   SET superseded = TRUE,
                       superseded_by = %s,
                       superseded_at = NOW(),
                       priority = 1
                   WHERE memory_id = ANY(%s)""",
                (new_id, list(old_ids)),
            )
        conn.commit()
    finally:
        conn.close()


def demote_many(old_ids):
    """
    'Both are true' option: keep older facts active but drop their priority so
    the newest one always outranks them in retrieval.
    """
    if not old_ids:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET priority = GREATEST(priority - 2, 1) WHERE memory_id = ANY(%s)",
                (list(old_ids),),
            )
        conn.commit()
    finally:
        conn.close()


def version_history():
    """The chain of replaced facts: (old, new_or_None, when). For your demo."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT o.content, n.content, o.superseded_at
                   FROM memories o
                   LEFT JOIN memories n ON o.superseded_by = n.memory_id
                   WHERE o.superseded = TRUE
                   ORDER BY o.superseded_at DESC"""
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------- HOUSEKEEPING ----------
def clear_data(include_messages=True):
    """Wipe stored memories (and optionally the chat log) and restart IDs at 1."""
    target = "memories, messages" if include_messages else "memories"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {target} RESTART IDENTITY CASCADE")
        conn.commit()
    finally:
        conn.close()


def memory_count():
    """How many active (non-superseded) memories are stored."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memories WHERE superseded = FALSE AND (valid_until IS NULL OR valid_until > NOW())")
            return cur.fetchone()[0]
    finally:
        conn.close()


# ---------- RETRIEVE (weighted temporal score) ----------
def retrieve(agent_id, query, k=5,
             w_sim=None, w_pri=None, w_recency=None, w_urgency=None):
    """
    Return the top-k memories for this agent, ranked by a blend of:
        semantic_similarity  — relevance to the current message
        priority             — intrinsic importance
        recency              — how recently the fact was STORED   (decays with age)
        urgency              — how soon the event HAPPENS          (rises as valid_until nears)

    'recency' answers "is this information fresh?" (based on created_at).
    'urgency' answers "is this event imminent?"    (based on valid_until).
    A dated fact like an exam becomes MORE important as its date approaches, which
    plain recency alone would get backwards — urgency fixes that.
    Memories with no valid_until get no urgency term (they are timeless).
    """
    w_sim     = W_SIM      if w_sim     is None else w_sim
    w_pri     = W_PRI      if w_pri     is None else w_pri
    w_recency = W_RECENCY  if w_recency is None else w_recency
    w_urgency = W_URGENCY  if w_urgency is None else w_urgency

    qvec = _to_pgvector(embed(query))
    sql = """
        SELECT memory_id, content, priority, created_at,
               (1 - (embedding <=> %s::vector)) AS similarity
        FROM memories
        WHERE (scope = 'shared' OR agent_id = %s)
          AND superseded = FALSE
          AND (valid_until IS NULL OR valid_until > NOW())
          AND embedding IS NOT NULL
          AND (1 - (embedding <=> %s::vector)) > %s
        ORDER BY (
              %s * (1 - (embedding <=> %s::vector))
            + %s * (priority::float / 5)
            + %s * (1.0 / (1 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400))
            + %s * (CASE
                       WHEN valid_until IS NULL THEN 0
                       ELSE 1.0 / (1 + GREATEST(EXTRACT(EPOCH FROM (valid_until - NOW())) / 86400, 0))
                    END)
        ) DESC
        LIMIT %s
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (qvec, agent_id, qvec, MIN_SIMILARITY,
                              w_sim, qvec, w_pri, w_recency, w_urgency, k))
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            if ids:  # reinforcement: recall strengthens a memory
                cur.execute(
                    """UPDATE memories
                       SET access_count = access_count + 1, last_accessed = NOW()
                       WHERE memory_id = ANY(%s)""",
                    (ids,),
                )
        conn.commit()
        return rows
    finally:
        conn.close()


# ---------- CONTEXT ASSEMBLY ----------
def assemble_context(persona, memories, recent_messages, user_message):
    """Bundle persona + retrieved memories + recent chat + new message into one prompt."""
    mem_text = "\n".join(f"- {m[1]}" for m in memories) or "(nothing remembered yet)"
    hist_text = "\n".join(f"{role}: {content}" for role, content in recent_messages) or "(none)"
    return f"""{persona}

What you remember about the user. These are current, verified, non-contradictory
facts — trust them and do not second-guess them:
{mem_text}

Recent conversation:
{hist_text}

User: {user_message}
Assistant:"""
