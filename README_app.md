# app.py — The MemoryChat User Interface (Streamlit)

`app.py` is the **main page** of the app. It draws the chat UI and connects
everything together: it calls `conversations.py` for threads, `memory.py` for
memory, and `llm.py` for replies. It contains **no SQL of its own**: every
database call goes through those modules.

Run with:

```
streamlit run app.py
```

---

## 1. How Streamlit works (needed to understand everything else)

- **The whole script reruns from top to bottom on every interaction**: every
  click, every message, every toggle.
- Ordinary variables are **reset** on each rerun. Anything that must survive
  is kept in **`st.session_state`**, a dictionary that lasts for the browser
  session.
- A widget like `st.button(...)` returns `True` **only on the rerun caused by
  clicking it**.
- `st.rerun()` starts a fresh rerun immediately, so the page redraws with
  the new data.
- The page **fades** while a rerun is running. That's why fewer and faster
  DB calls make the app feel quicker (see `README_db.md`).

---

## 2. Layout

```
┌──────────────── sidebar ────────────────┬──────────────── main ────────────────┐
│ MemoryChat (brand)                      │ agent name                           │
│ [New chat]                              │ chat messages  (or "Hey, how can     │
│ [Search chats]                          │                 I assist?" greeting) │
│ AGENTS                                  │                                      │
│   Study Buddy  03                       │ ⚠ conflict box (when needed)         │
│ PINNED / TODAY / YESTERDAY / ...        │                                      │
│   chat title            [⋯]             │ ┌──────────────────────────┐ [⋯]     │
│                                         │ │ Message <agent>…         │ tools   │
│ [user card -> Manage data]              │ └──────────────────────────┘         │
│                                         │ (Private memory) (Shared memory)     │
└─────────────────────────────────────────┴──────────────────────────────────────┘
```

---

## 3. Walk-through, top to bottom

### 3.1 Imports and page setup (lines 14–31)

```python
from memory import (store_memory, retrieve, assemble_context, find_conflicts,
                    resolve_conflict, version_history, clear_data, memory_count,
                    memory_stats, purge_expired, is_fact, log_message)
from conversations import (...)
from llm import generate_reply

st.set_page_config(page_title="MemoryChat", page_icon=":material/psychology:")
USER_NAME = st.secrets.get("USER_NAME", "User")
USER_EMAIL = st.secrets.get("USER_EMAIL", "")
```

- `app.py` only **imports and calls** functions; the logic lives in the
  other modules (**separation of concerns**).
- The user name and email shown on the user card are read from
  `secrets.toml`, not written in the code.

### 3.2 CSS styling (lines 33–87)

A `<style>` block injected with `st.markdown(..., unsafe_allow_html=True)`:
- dark theme, with the main panel drawn as a rounded card;
- left-aligned sidebar rows and uppercase section headers (`.sec`);
- the user card pinned to the bottom of the sidebar;
- the empty-thread greeting (`.hero`);
- the chat bar `position: sticky; bottom: 0`, so it stays visible on long
  chats.

Streamlit tags each widget that has `key="x"` with a CSS class
`st-key-x`, which is how specific widgets are styled.

### 3.3 Load agents (lines 90–94)

```python
agents = list_agents_with_counts()
if not agents:
    st.warning("No agents yet. Add one on the Manage Data page first.")
    st.page_link("pages/1_Manage_Data.py", ...)
    st.stop()
```

- One query returns every agent **with its thread count** (LEFT JOIN +
  GROUP BY in `conversations.py`).
- With no agents the chat can't work, so the script **stops** and links to
  the Manage Data page.

### 3.4 Session state (lines 97–112)

```python
st.session_state.setdefault("pending", None)     # conflict waiting for the user
st.session_state.setdefault("conv_id", None)     # open chat thread
st.session_state.setdefault("scope", "private")  # where new memories are saved
if st.session_state.get("agent_id") not in [a[0] for a in agents]:
    st.session_state.agent_id = agents[0][0]
```

| Key | Holds | Why it is needed |
|---|---|---|
| `agent_id` | The selected agent | Falls back to the first agent if the saved one was deleted |
| `conv_id` | The open thread | Same fallback: if missing or deleted, the newest thread is used |
| `pending` | A conflict waiting for the user | The conflict box must survive the rerun after the message is sent |
| `scope` | `"private"` or `"shared"` | Bound to the scope pills by `key="scope"` |

```python
convs = list_conversations(agent_id)
if not convs:
    create_conversation(agent_id)
    convs = list_conversations(agent_id)
```

**There is always a valid thread to write into.** A new agent automatically
gets its first "New chat".

### 3.5 Highlight the active agent and thread (lines 115–118)

A small CSS rule built with an f-string highlights the buttons
`agent_<id>` and `chat_<id>`, so the user can see what is selected.

### 3.6 Helper functions

**`bucket(pinned, updated)`** (lines 121–131) picks the sidebar group for a
thread:

| Condition | Group |
|---|---|
| pinned | Pinned |
| updated today | Today |
| 1 day ago | Yesterday |
| < 7 days | Previous 7 days |
| otherwise | Older |

`updated_at` is stored in **UTC** (Neon's time zone), so it is converted to
local time with `.replace(tzinfo=utc).astimezone()` before comparing dates;
otherwise late-night IST messages would land in the wrong day.

**`scope_pills()`** (lines 134–139) draws the two pills **Private memory /
Shared memory**. `key="scope"` writes the choice straight into
`st.session_state.scope`.
- **Private:** only this agent remembers the fact (`agent_id` set).
- **Shared:** every agent can see it (`agent_id = NULL`, `scope = 'shared'`).

**`memory_tools()`** (lines 142–169) is the contents of the **⋯ popover**
next to the chat box:

| UI | Calls | DB object behind it |
|---|---|---|
| Active / Retired / Expired / Avg priority / Recalls | `memory_count()`, `memory_stats()` | function `fn_memory_stats` |
| **Purge expired now** | `purge_expired()` | function `fn_purge_expired` |
| **Show version history** | `version_history()` | self-join on `superseded_by` |
| **Clear all data** (only after ticking "Yes, I'm sure") | `clear_data()` | `TRUNCATE ... RESTART IDENTITY CASCADE` |

The confirmation checkbox guards a destructive action from a misclick.

### 3.7 Sidebar (lines 173–234)

| Element | What it does | Calls |
|---|---|---|
| **New chat** | Reuses an existing empty "New chat" thread if there is one, otherwise creates a new thread | `create_conversation` |
| **Search chats** | Filters thread titles in Python (`query.lower() in title.lower()`) | – |
| **Agents** list | One button per agent, showing its thread count (`Study Buddy 03`); clicking switches agent and resets the thread | `list_agents_with_counts` (already loaded) |
| Thread list | Grouped by `bucket()`; a group header is printed when the group changes | `list_conversations` (already sorted pinned → newest) |
| Thread **⋯** menu | Pin / Unpin, Rename, Delete | `set_pinned`, `rename_conversation`, `delete_conversation` |
| User card | Popover with initials, name, email and a **Manage data** link | – |

- The thread list prints group headers in one pass because the SQL already
  returns the rows **sorted** (pinned first, then newest). No extra sorting
  is done in Python.
- Switching agent or thread clears `pending`, so a conflict box never
  appears on the wrong chat.
- User text shown as HTML goes through `html.escape()`, which stops HTML/JS
  injection (XSS).

### 3.8 Prepare the chat (lines 236–238)

```python
persona = f"You are {agent[1]}. {agent[2]}. Never use emojis."
history = load_chat(conv_id)
```

- The **persona** comes from the agent's name and description in the
  `agents` table. That is the only thing that makes one agent behave
  differently from another.
- `history` holds the open thread's messages, oldest first.

### 3.9 Main area: messages or greeting (lines 240–252)

- If the thread has messages, each one is drawn with `st.chat_message`.
- If it is empty, a **"Hey, how can I assist?"** greeting is shown in an
  `st.empty()` placeholder, so it can be removed the moment the first message
  is sent.

### 3.10 Conflict confirmation box (lines 255–281)

Shown when `st.session_state.pending` is set, meaning the last message
contradicted stored facts:

```
⚠ This may contradict 2 stored fact(s).
New message: I live in Pune now
Tick the facts that are no longer true:
 [x] I live in Mumbai          · sim 0.66
 [ ] I live in Trivandrum      · sim 0.36
 [x] Also save this message as a new memory
 [Confirm]   [Cancel — change nothing]
```

- The **most similar** fact is ticked by default (`value=(i == 0)`).
- **Ticked** facts are **retired** (superseded by the new fact).
- **Unticked** facts are "both true" and are kept but **demoted**.
- Unticking **"Also save..."** handles **cancellations** ("my exam is
  cancelled"): the old fact is retired and nothing new is stored.
- **Confirm** → `resolve_conflict(...)` does the INSERT and
  `CALL sp_supersede` in **one transaction** (atomic).
- **Cancel** changes nothing.

### 3.11 Chat bar (lines 283–293)

Inside one sticky container (`key="chatbar"`):
- `st.chat_input` is the message box;
- a **⋯** popover holds `memory_tools()`;
- `scope_pills()` sit underneath.

```python
scope = st.session_state.scope or "private"   # pills can be deselected
```

If the user clicks the active pill off, it falls back to private.

### 3.12 The main workflow when a message is sent (lines 296–338)

```python
if user_message:
    st.session_state.pending = None
    autoname_conversation(conv_id, user_message)                 # (a)
    user_msg_id = log_message(agent_id, "user", user_message,    # (b)
                              conversation_id=conv_id)

    memories = retrieve(agent_id, user_message, k=5)             # (1)
    context = assemble_context(persona, memories,                # (2)
                               history[-6:], user_message)
    reply = generate_reply(context)                              # (3)
    log_message(agent_id, "agent", reply, conversation_id=conv_id)

    if is_fact(user_message):                                    # (4)
        conflicts = find_conflicts(agent_id, user_message)
        if conflicts:
            st.session_state.pending = {...}; st.rerun()         #  -> conflict box
        else:
            store_memory(user_message, agent_id, scope,
                         source_message_id=user_msg_id)
    st.rerun()
```

| Step | What happens | Where |
|---|---|---|
| (a) | First message names the thread (only while it is "New chat") | `conversations.autoname_conversation` |
| (b) | User message saved; its ID is kept for **provenance** | `memory.log_message` |
| **1** | **Retrieve** the top-5 memories by weighted score (similarity, priority, recency, urgency) | `memory.retrieve` |
| **2** | **Assemble** persona + memories + last 6 messages + new message into one prompt | `memory.assemble_context` |
| **3** | **Generate** the reply with Groq, then save it | `llm.generate_reply` |
| **4** | **Learn**: if the message is a fact, check for conflicts, then store it or ask the user | `memory.is_fact`, `find_conflicts`, `store_memory` |

**Order matters.** Memories are retrieved **before** the new message is
stored, so the reply uses what was already known, and a new fact is never
stored before its conflicts are resolved.

Under each reply, a **"Memories used (ranked)"** expander shows which
memories were retrieved, with their priority and similarity: the retrieval
is visible and explainable.

The final `st.rerun()` refreshes the sidebar, so a newly named thread shows
its title.

---

## 4. Full picture

```
                 ┌──────────── app.py (UI) ────────────┐
 user ─ click ─► │ sidebar      main chat     tools ⋯  │
                 └───┬──────────────┬──────────────┬───┘
                     │              │              │
          conversations.py      memory.py        llm.py
          (threads)             (memory engine)  (Groq reply)
                     │              │
                     └──── db.py ───┘
                            │
            PostgreSQL on Neon (pgvector, triggers, procedure, functions)
```

---

## 5. Viva questions

- **Where is the SQL in `app.py`?** There is none. The UI only calls
  functions in `memory.py` and `conversations.py`; the data logic is kept
  separate from the presentation.
- **What is `st.session_state` and why use it?** Streamlit reruns the whole
  script on every click, which resets variables. `session_state` keeps the
  selected agent, open thread, pending conflict and scope between reruns.
- **Why retrieve before storing the new message?** The reply should use
  existing knowledge, and a fact must not be stored before its conflicts are
  resolved.
- **Why ask the user instead of replacing facts automatically?** Similarity
  can't always tell "changed" from "both true" (e.g. two exams). The user
  decides, and the choice is applied atomically.
- **How do private and shared memories differ?** Private is saved with the
  agent's ID and seen only by that agent. Shared is saved with
  `agent_id = NULL` and seen by all agents.
- **Why the "Yes, I'm sure" checkbox?** It's a confirmation step before an
  irreversible `TRUNCATE`.
- **Why `html.escape()`?** User-controlled text inserted as HTML could
  inject scripts (XSS); escaping makes it plain text.
- **Why does the screen fade on clicks?** Streamlit reruns the script and
  dims the old page until the new one is ready. Reusing the DB connection
  (`db.py`) made that much shorter.
- **Why is `updated_at` converted in `bucket()`?** It is stored in UTC;
  comparing with today's local (IST) date needs the conversion, or late-night
  chats fall in the wrong group.
- **What are the limitations?** Chat search filters titles only, not
  message text; the UI is built for a single user (no login).
