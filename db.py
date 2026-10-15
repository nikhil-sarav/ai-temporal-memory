"""Database connection helper."""

import time
import streamlit as st
import psycopg2
import psycopg2.extensions


class _ReusedConn(psycopg2.extensions.connection):
    """Callers still do conn.close(); here that only ends the transaction
    (rollback is a no-op after commit), so the socket stays open for reuse."""
    def close(self):
        self.rollback()


# ponytail: one shared connection, fine for a single-user demo; use a pool if many users.
@st.cache_resource
def _shared():
    return {"conn": None, "last": 0.0}


def get_conn():
    s = _shared()
    conn = s["conn"]
    # Neon drops idle connections (autosuspend), so re-check after a quiet minute.
    if conn is not None and not conn.closed and time.time() - s["last"] > 60:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
        except psycopg2.Error:
            conn = None
    if conn is None or conn.closed:
        conn = psycopg2.connect(st.secrets["NEON_CONN_STRING"],
                                connection_factory=_ReusedConn)
        s["conn"] = conn
    s["last"] = time.time()
    return conn
