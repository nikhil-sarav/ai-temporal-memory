"""
Streamlit UI for the AI Agent Temporal Memory Management System.
Run with:  streamlit run app.py
"""

import streamlit as st
from db import get_conn
from memory import (store_memory, retrieve, assemble_context,
                    find_conflicts, supersede_many, demote_many,
                    version_history, clear_data, memory_count, is_fact,
                    log_message, load_messages)
from llm import generate_reply

st.set_page_config(page_title="Temporal Memory Agents", page_icon="🧠")
st.title("AI Agent Temporal Memory System")


def load_agents():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_id, name, description FROM agents ORDER BY agent_id")
            return cur.fetchall()
    finally:
        conn.close()


agents = load_agents()
by_name = {a[1]: a for a in agents}

# ---------- session state ----------
# Chat history now lives in the database (messages table); only the transient
# conflict prompt is kept in session state.
if "pending" not in st.session_state:
    st.session_state.pending = None

# ---------- sidebar ----------
with st.sidebar:
    st.header("Agent")
    choice = st.selectbox("Active agent", list(by_name.keys()))
    agent = by_name[choice]
    st.caption(agent[2])
    scope = st.radio("Save new memories as", ["private", "shared"],
                     help="private = only this agent remembers it; shared = all agents can see it")

    st.divider()
    st.caption(f"Active memories stored: **{memory_count()}**")

    if st.toggle("Show version history"):
        rows = version_history()
        if rows:
            for old, new, when in rows:
                if new:
                    st.caption(f"~~{old}~~ → **{new}**")
                else:
                    st.caption(f"~~{old}~~ → _retired_")
        else:
            st.caption("No facts have been superseded yet.")

    st.divider()
    with st.expander("⚠️ Reset data"):
        st.caption("Deletes stored memories and restarts IDs at 1. "
                   "Agents and the schema are kept.")
        confirm = st.checkbox("Yes, I'm sure")
        if st.button("Clear all memories", disabled=not confirm, type="primary"):
            clear_data(include_messages=True)
            st.session_state.pending = None
            st.rerun()

agent_id = agent[0]
persona = f"You are {agent[1]}. {agent[2]}."

# History now comes from the messages table, so it survives a true fresh session
# (new browser / new machine), not just an open tab.
history = load_messages(agent_id)

# ---------- chat history ----------
for role, content in history:
    with st.chat_message("user" if role == "user" else "assistant"):
        st.write(content)


# ---------- CONFLICT CONFIRMATION ----------
# Every contradictory fact is listed with its own checkbox, so the user retires
# exactly the ones this message invalidates — corrections and cancellations
# both resolve correctly.
p = st.session_state.pending
if p:
    conflicts = p["conflicts"]
    st.warning(f"⚠️ This may contradict {len(conflicts)} stored fact(s).")
    st.caption(f"**New message:** {p['content']}")
    st.caption("Tick the facts that are no longer true:")

    chosen = []
    for i, (mid, text, sim) in enumerate(conflicts):
        # default-tick only the closest match; the rest are opt-in
        if st.checkbox(f"{text}  ·  sim {sim:.2f}", value=(i == 0), key=f"conf_{mid}"):
            chosen.append(mid)

    save_new = st.checkbox(
        "Also save this message as a new memory",
        value=True, key="conf_save",
        help="Untick for cancellations — nothing new is remembered, the old fact is just retired.",
    )

    c1, c2 = st.columns(2)
    if c1.button("Confirm", key="conf_ok", type="primary"):
        new_id = None
        if save_new:
            new_id, _ = store_memory(p["content"], agent_id, p["scope"])
        supersede_many(chosen, new_id)          # only the ticked facts are retired
        untouched = [c[0] for c in conflicts if c[0] not in chosen]
        if new_id and untouched:
            demote_many(untouched)              # kept facts rank below the newest
        st.session_state.pending = None
        st.rerun()
    if c2.button("Cancel — change nothing", key="conf_skip"):
        st.session_state.pending = None
        st.rerun()

# ---------- chat input -> full workflow loop ----------
user_message = st.chat_input("Type a message...")
if user_message:
    st.session_state.pending = None   # typing on = drop any unanswered conflict

    log_message(agent_id, "user", user_message)   # persist the user turn
    history.append(("user", user_message))
    with st.chat_message("user"):
        st.write(user_message)

    # 1) retrieve relevant memories (private + shared, non-superseded)
    memories = retrieve(agent_id, user_message, k=5)

    # 2) assemble the prompt
    context = assemble_context(persona, memories, history[-6:], user_message)

    # 3) generate the reply
    reply = generate_reply(context)
    log_message(agent_id, "agent", reply)          # persist the agent turn
    history.append(("agent", reply))
    with st.chat_message("assistant"):
        st.write(reply)
        if memories:
            with st.expander("🧠 memories used (ranked)"):
                for m in memories:
                    st.caption(f"[priority {m[2]} · sim {m[4]:.2f}]  {m[1]}")

    # 4) only statements become memories — questions are just retrieval cues
    if is_fact(user_message):
        conflicts = find_conflicts(agent_id, user_message)
        if conflicts:
            st.session_state.pending = {"content": user_message,
                                        "scope": scope,
                                        "conflicts": conflicts}
            st.rerun()
        else:
            store_memory(user_message, agent_id, scope)   # dedupes automatically
