"""
Chat-thread (conversation) management — powers the ChatGPT-style sidebar list.

A conversation is one chat thread belonging to an agent. The agent keeps the
long-term memory; conversations just organise the raw chat into threads you can
switch between, rename, pin, and delete.
"""

from db import get_conn


def _exec(sql, params):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def list_agents_with_counts():
    """READ every agent plus how many chat threads it has.
    LEFT JOIN keeps agents with zero threads; GROUP BY the primary key lets us
    select name/description too (they are functionally dependent on it).
    -> [(agent_id, name, description, n_conversations), ...]"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT a.agent_id, a.name, a.description, COUNT(c.conversation_id)
                   FROM agents a
                   LEFT JOIN conversations c ON c.agent_id = a.agent_id
                   GROUP BY a.agent_id
                   ORDER BY a.agent_id"""
            )
            return cur.fetchall()
    finally:
        conn.close()


def create_conversation(agent_id, title="New chat"):
    """CREATE a new empty chat thread. Returns its id."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (agent_id, title) VALUES (%s, %s) "
                "RETURNING conversation_id",
                (agent_id, title),
            )
            cid = cur.fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


def list_conversations(agent_id):
    """READ all threads for an agent — pinned first, then most recently used.
    -> [(conversation_id, title, pinned, updated_at), ...]"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT conversation_id, title, pinned, updated_at
                   FROM conversations
                   WHERE agent_id = %s
                   ORDER BY pinned DESC, updated_at DESC""",
                (agent_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def rename_conversation(cid, title):
    """UPDATE a thread's title (manual rename from the ⋯ menu)."""
    _exec("UPDATE conversations SET title = %s WHERE conversation_id = %s",
          (title, cid))


def autoname_conversation(cid, text):
    """Set the title from the first user message — but only while it's still the
    default 'New chat', so a manual rename is never overwritten."""
    title = (text[:40] + "…") if len(text) > 40 else text
    _exec("UPDATE conversations SET title = %s "
          "WHERE conversation_id = %s AND title = 'New chat'",
          (title, cid))


def set_pinned(cid, pinned):
    """UPDATE the pinned flag."""
    _exec("UPDATE conversations SET pinned = %s WHERE conversation_id = %s",
          (pinned, cid))


def delete_conversation(cid):
    """DELETE a thread and its messages (ON DELETE CASCADE handles the messages)."""
    _exec("DELETE FROM conversations WHERE conversation_id = %s", (cid,))


def load_chat(conversation_id):
    """READ the messages in one thread, oldest first. -> [(role, content), ...]"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT role, content FROM messages
                   WHERE conversation_id = %s
                   ORDER BY message_id ASC""",
                (conversation_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()
