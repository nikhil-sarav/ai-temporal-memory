# db.py — Database Connection Helper

`db.py` gives every other file one function, `get_conn()`, that returns a
connection to the Neon PostgreSQL database (with pgvector). All SQL in
`memory.py` and `conversations.py` goes through it.

---

## 1. The original version

```python
import streamlit as st
import psycopg2

def get_conn():
    return psycopg2.connect(st.secrets["NEON_CONN_STRING"])
```

| Line | Meaning |
|---|---|
| `import psycopg2` | The standard Python driver for PostgreSQL. It sends SQL from Python to the DB. |
| `st.secrets["NEON_CONN_STRING"]` | Reads the connection string (`postgresql://user:password@host/dbname?sslmode=require`) from `.streamlit/secrets.toml`. The password is never in the code, and the file is gitignored. |
| `get_conn()` | Opened a **new** connection every time it was called. |

---

## 2. How the rest of the code uses it

Every DB function follows the same pattern:

```python
conn = get_conn()               # 1. get a connection
try:
    with conn.cursor() as cur:  # 2. a cursor runs SQL
        cur.execute("... %s", (value,))
    conn.commit()               # 3. make the changes permanent
finally:
    conn.close()                # 4. always runs, even after an error
```

- **Transaction:** psycopg2 starts one automatically on the first query.
  Nothing is saved until `conn.commit()`.
- **Atomicity:** `resolve_conflict()` runs the INSERT and `CALL sp_supersede`
  on one connection with one commit, so either both are saved or neither is.
- **Cursor:** the object that runs a query and fetches the results
  (`fetchone()` / `fetchall()`).
- **`%s` parameters:** psycopg2 escapes the values safely, which prevents
  **SQL injection**.
- **`finally`:** the connection is always released, even when a query fails.

---

## 3. The problem: the app was slow

Streamlit **reruns the whole of `app.py` on every click**. Each rerun calls
`list_agents_with_counts`, `list_conversations`, `load_chat`, `memory_count`
and `memory_stats`, which means 5 queries.

Measured timings:

| | Time |
|---|---|
| Opening a new connection to Neon (TCP + SSL + login) | **0.65 – 1.45 s** |
| Running one query on an already-open connection | ~0.15 – 0.3 s |

5 new connections per click meant about **5–6 s of loading** (the screen
fades) on every click.

---

## 4. The current version: one reused connection

```python
import time
import streamlit as st
import psycopg2
import psycopg2.extensions


class _ReusedConn(psycopg2.extensions.connection):
    def close(self):
        self.rollback()


@st.cache_resource
def _shared():
    return {"conn": None, "last": 0.0}


def get_conn():
    s = _shared()
    conn = s["conn"]
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
```

### 4.1 Imports
- `time`: records when the connection was last used.
- `streamlit`: provides `st.secrets` (the connection string) and
  `st.cache_resource` (keeps one object alive).
- `psycopg2.extensions`: holds psycopg2's base `connection` class.

### 4.2 `_ReusedConn`: a connection whose `close()` doesn't close
- It is a **subclass** of the normal psycopg2 connection. Only `close()` is
  changed.
- The other files still call `conn.close()`, so **no other file had to
  change**. That call now only **rolls back**, and the connection stays open.
- Why rollback is safe:
  - after a successful `commit()` there is nothing left to undo;
  - after an error it **discards the half-done work** (atomicity) and clears
    the "transaction aborted" state, so the next query works.

### 4.3 `_shared()`: storage that survives reruns
- Normally every variable is reset on each rerun.
- `@st.cache_resource` runs the function **once** and returns the **same
  dictionary** every time. The dictionary holds:
  - `conn`: the open connection (None at first);
  - `last`: when it was last used.

### 4.4 `get_conn()`
1. **Health check:** Neon's free tier suspends after a few idle minutes and
   drops open connections. If more than **60 s** have passed since the last
   use, a cheap `SELECT 1` tests the connection, and a failure marks it dead.
   It is only checked when idle, so normal clicks don't pay the extra round
   trip.
2. **(Re)connect:** if there is no connection yet, or it is dead, a new one
   is opened and saved. `connection_factory=_ReusedConn` makes psycopg2 build
   our subclass.
3. **Record the time of use** and return the connection.

### 4.5 Flow

```
first click   -> no connection -> connect (~1 s) -> save it
next clicks   -> reuse it (no connect cost)
idle > 60 s   -> SELECT 1 test -> OK: reuse | dead: reconnect
conn.close()  -> rollback only; the socket stays open
```

### 4.6 Result

| | Before | After |
|---|---|---|
| One rerun (same 5 queries) | ~5–6 s (estimated) | **1.7–2.5 s** (measured) |

The "before" figure was not timed directly; it comes from the connection and
query timings in section 3.

The remaining time is network round trips to Neon's Singapore region
(~0.3 s per query from India). Sending a message also waits for the Groq LLM
reply.

---

## 5. Viva questions

- **Why have a separate `db.py`?** There is one place to change the
  database: switching from Neon to a local PostgreSQL only changes the
  connection string.
- **Why reuse the connection?** Opening one needs a network handshake, SSL
  and a login, about 1 s. Reusing it cut a rerun from about 5–6 s to about
  1.7 s.
- **Why rollback inside `close()`?** It ends the transaction cleanly: it does
  nothing after a commit, and undoes partial changes after an error, so
  atomicity is kept.
- **How is SQL injection prevented?** Every value is passed as a `%s`
  parameter, never pasted into the SQL string.
- **What is the limitation?** One shared connection suits a single user. With
  many users at once, use a **connection pool**
  (`psycopg2.pool.ThreadedConnectionPool`).
- **What is a connection pool?** A set of already-open connections that
  requests borrow and return, instead of opening a new one each time.
