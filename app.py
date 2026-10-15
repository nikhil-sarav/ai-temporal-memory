"""
Streamlit UI for the AI Agent Temporal Memory Management System.
Run with:  streamlit run app.py

Layout:
  sidebar -> brand, New chat, search, Agents (with thread counts),
             chats grouped Pinned / Today / Yesterday / Previous 7 days / Older,
             user card at the bottom -> popup menu with Manage data (user menu.png)
  main    -> greeting (empty thread) or messages, then the chat bar:
             input + memory-tools menu (...) + memory-scope pills, kept in view
"""


import datetime as dt
import html
import streamlit as st
from memory import (store_memory, retrieve, assemble_context,
                    find_conflicts, resolve_conflict,
                    version_history, clear_data, memory_count, memory_stats,
                    purge_expired, is_fact, log_message)
from conversations import (create_conversation, list_conversations,
                           rename_conversation, autoname_conversation,
                           set_pinned, delete_conversation, load_chat,
                           list_agents_with_counts)
from llm import generate_reply

st.set_page_config(page_title="MemoryChat", page_icon=":material/psychology:")

# shown on the user card; set USER_NAME / USER_EMAIL in .streamlit/secrets.toml
USER_NAME = st.secrets.get("USER_NAME", "User")
USER_EMAIL = st.secrets.get("USER_EMAIL", "")

st.markdown("""
<style>
/* dark frame with the main panel as a rounded card */
[data-testid="stApp"]    { background: #0B0F1A; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMain"]   { background: #050814; border: 1px solid #161B2E;
                           border-radius: 16px; margin: 10px 10px 10px 0; }
[data-testid="stMainBlockContainer"] { max-width: 48rem; }

/* sidebar: plain, left-aligned rows */
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: .1rem; }
[data-testid="stSidebar"] button[kind="tertiary"] {
    padding: .4rem .6rem; border-radius: 8px; color: #C9CDDA; }
[data-testid="stSidebar"] button[kind="tertiary"] > div { justify-content: flex-start; width: 100%; }
[data-testid="stSidebar"] button code { background: #161B2E; color: #8B91A7; font-size: .7rem; }
[data-testid="stSidebar"] [data-testid="stPopover"] button { border: none; background: transparent; }
[data-testid="stSidebar"] button[kind="tertiary"]:hover { background: rgba(255,255,255,.05); color: #fff; }
[data-testid="stSidebar"] button[kind="tertiary"] p {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.st-key-brand p { font-size: 1.35rem; font-weight: 600; margin: 0 0 .8rem .4rem; }
.st-key-brand [data-testid="stIconMaterial"] { color: #5B6CF9; }

/* user card pinned to the bottom of the sidebar (opens the user menu) */
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {
    min-height: calc(100vh - 6rem); }
[data-testid="stLayoutWrapper"]:has(> .st-key-user_menu) { margin-top: auto; padding-top: 1rem; }
.st-key-user_menu button {
    background: linear-gradient(90deg, #161B2E 35%, #3B4CC0) !important;
    border: none !important; border-radius: 12px; padding: .75rem .9rem; }
.st-key-user_menu button div   { justify-content: flex-start; }
.st-key-user_menu button > div { justify-content: space-between; width: 100%; }
.who        { display: flex; gap: .7rem; align-items: center; }
.who-avatar { width: 2.4rem; height: 2.4rem; border-radius: 50%; background: #5B6CF9;
              display: grid; place-items: center; font-weight: 600; color: #fff; }
.who b      { display: block; }
.who span   { color: #8B91A7; font-size: .8rem; }
.sec   { font-size: .72rem; letter-spacing: .06em; text-transform: uppercase;
         color: #7D8399; margin: 0; padding: 1rem 0 .45rem .6rem; }

/* empty-thread greeting */
.hero       { text-align: center; margin-top: 22vh; }
.hero-title { font-size: 2.1rem; font-weight: 500; color: #6F7FFB;
              background: linear-gradient(90deg, #5B6CF9, #9AA8FF);
              -webkit-background-clip: text; background-clip: text; color: transparent; }
.hero p     { color: #9AA0B4; max-width: 30rem; margin: .4rem auto 1.6rem; }
[data-testid="stPills"] { justify-content: center; }

[data-testid="stChatInput"]   { border-radius: 16px; border: 1px solid #1E2438; }
/* chat bar sticks to the bottom of the panel once the chat is long */
.st-key-chatbar { position: sticky; bottom: 0; z-index: 5; background: #050814;
                  padding: .5rem 0 1rem; }
.st-key-chatbar [data-testid="stPopover"] button { height: 3rem; border-radius: 14px; }
[data-testid="stChatMessage"] { background: transparent; }
</style>
""", unsafe_allow_html=True)

# ---------- agents ----------
agents = list_agents_with_counts()
if not agents:
    st.warning("No agents yet. Add one on the Manage Data page first.")
    st.page_link("pages/1_Manage_Data.py", label="Manage Data", icon=":material/database:")
    st.stop()

# ---------- session state ----------
st.session_state.setdefault("pending", None)     # transient conflict prompt
st.session_state.setdefault("conv_id", None)     # active chat thread
st.session_state.setdefault("scope", "private")  # where new memories are saved
if st.session_state.get("agent_id") not in [a[0] for a in agents]:
    st.session_state.agent_id = agents[0][0]
agent_id = st.session_state.agent_id
agent = next(a for a in agents if a[0] == agent_id)

# make sure there is always a valid active thread for this agent
convs = list_conversations(agent_id)
if not convs:
    create_conversation(agent_id)
    convs = list_conversations(agent_id)
if st.session_state.conv_id not in [c[0] for c in convs]:
    st.session_state.conv_id = convs[0][0]
conv_id = st.session_state.conv_id

# highlight the active agent + thread (Streamlit tags each widget with st-key-<key>)
st.markdown(f"""<style>
.st-key-agent_{agent_id} button, .st-key-chat_{conv_id} button {{
    background: rgba(91,108,249,.16) !important; color: #fff !important; }}
</style>""", unsafe_allow_html=True)


def bucket(pinned, updated):
    """Sidebar group for a thread. updated_at is stored in UTC."""
    if pinned:
        return "Pinned"
    days = (dt.date.today()
            - updated.replace(tzinfo=dt.timezone.utc).astimezone().date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return "Previous 7 days" if days < 7 else "Older"


def scope_pills():
    st.pills("Save new memories as", ["private", "shared"], key="scope",
             label_visibility="collapsed",
             format_func=lambda s: {"private": ":material/lock: Private memory",
                                    "shared": ":material/group: Shared memory"}[s],
             help="Private = only this agent remembers it; shared = every agent can see it")


def memory_tools():
    st.markdown("**Memory tools**")
    st.caption(f"Active memories: **{memory_count()}**")
    active, superseded, expired, avg_pri, recall = memory_stats(agent_id)
    st.caption(f"Active {active} · Retired {superseded} · Expired {expired}")
    st.caption(f"Avg priority {avg_pri if avg_pri is not None else '—'} · "
               f"Recalls {recall}")

    if st.button("Purge expired now", icon=":material/mop:", width="stretch"):
        n = purge_expired()
        st.success(f"Retired {n} expired memory(ies).")
        st.rerun()

    if st.toggle("Show version history"):
        rows = version_history()
        if rows:
            for old, new, when in rows:
                st.caption(f"~~{old}~~ → **{new}**" if new else f"~~{old}~~ → _retired_")
        else:
            st.caption("No facts have been superseded yet.")

    st.caption("Danger zone")
    if st.checkbox("Yes, I'm sure"):
        if st.button("Clear all data", type="primary", width="stretch"):
            clear_data(include_messages=True)
            st.session_state.pending = None
            st.session_state.conv_id = None
            st.rerun()


# ---------- sidebar ----------
with st.sidebar:
    with st.container(key="brand"):
        st.markdown(":material/psychology: MemoryChat")

    if st.button("New chat", icon=":material/edit_square:", type="tertiary",
                 width="stretch", key="new_chat"):
        unused = next((c[0] for c in convs if c[1] == "New chat"), None)  # reuse an empty thread
        st.session_state.conv_id = unused or create_conversation(agent_id)
        st.session_state.pending = None
        st.rerun()

    query = st.text_input("Search", placeholder="Search chats", icon=":material/search:",
                          label_visibility="collapsed")

    st.markdown('<p class="sec">Agents</p>', unsafe_allow_html=True)
    for aid, name, _, n in agents:
        if st.button(f"{name} `{n:02d}`", key=f"agent_{aid}", icon=":material/folder:",
                     type="tertiary", width="stretch"):
            st.session_state.agent_id = aid
            st.session_state.conv_id = None
            st.session_state.pending = None
            st.rerun()

    shown = None
    for cid, title, pinned, updated in convs:   # already ordered pinned, then newest
        title = title or "New chat"
        if query and query.lower() not in title.lower():
            continue
        group = bucket(pinned, updated)
        if group != shown:
            st.markdown(f'<p class="sec">{group}</p>', unsafe_allow_html=True)
            shown = group
        col_main, col_menu = st.columns([0.84, 0.16], vertical_alignment="center")
        if col_main.button(title, key=f"chat_{cid}", type="tertiary", width="stretch"):
            st.session_state.conv_id = cid
            st.session_state.pending = None
            st.rerun()
        with col_menu.popover(":material/more_horiz:"):
            if st.button("Unpin" if pinned else "Pin", icon=":material/keep:",
                         key=f"pin_{cid}", width="stretch"):
                set_pinned(cid, not pinned)
                st.rerun()
            new_title = st.text_input("Rename", value=title, key=f"rn_{cid}")
            if st.button("Save name", key=f"sv_{cid}", width="stretch"):
                rename_conversation(cid, new_title.strip() or "New chat")
                st.rerun()
            if st.button("Delete chat", icon=":material/delete:", key=f"dl_{cid}",
                         width="stretch"):
                delete_conversation(cid)
                st.session_state.conv_id = None
                st.rerun()

    # user card -> popup menu
    with st.container(key="user_menu"):
        with st.popover(USER_NAME, icon=":material/account_circle:", width="stretch"):
            initials = "".join(w[0] for w in USER_NAME.split()[:2]).upper()
            st.markdown(f"""<div class="who"><div class="who-avatar">{html.escape(initials)}</div>
                <div><b>{html.escape(USER_NAME)}</b><span>{html.escape(USER_EMAIL)}</span></div>
            </div>""", unsafe_allow_html=True)
            st.divider()
            st.page_link("pages/1_Manage_Data.py", label="Manage data",
                         icon=":material/database:")

persona = f"You are {agent[1]}. {agent[2]}. Never use emojis."
AVATAR = {"user": ":material/person:", "assistant": ":material/smart_toy:"}
history = load_chat(conv_id)

# ---------- main area ----------
hero = None
if history:
    st.markdown(f"**{agent[1]}**")
    for role, content in history:
        who = "user" if role == "user" else "assistant"
        with st.chat_message(who, avatar=AVATAR[who]):
            st.write(content)
else:
    hero = st.empty()   # cleared as soon as the first message is sent
    hero.markdown(f"""<div class="hero">
        <div class="hero-title">Hey, how can I assist?</div>
        <p>{html.escape(agent[2] or f"{agent[1]} remembers what you tell it.")}</p>
    </div>""", unsafe_allow_html=True)

# ---------- CONFLICT CONFIRMATION ----------
p = st.session_state.pending
if p:
    conflicts = p["conflicts"]
    st.warning(f"This may contradict {len(conflicts)} stored fact(s).", icon=":material/warning:")
    st.caption(f"**New message:** {p['content']}")
    st.caption("Tick the facts that are no longer true:")

    chosen = []
    for i, (mid, text, sim) in enumerate(conflicts):
        if st.checkbox(f"{text}  ·  sim {sim:.2f}", value=(i == 0), key=f"conf_{mid}"):
            chosen.append(mid)

    save_new = st.checkbox(
        "Also save this message as a new memory",
        value=True, key="conf_save",
        help="Untick for cancellations — nothing new is remembered, the old fact is just retired.",
    )

    c1, c2 = st.columns(2)
    if c1.button("Confirm", key="conf_ok", type="primary"):
        untouched = [c[0] for c in conflicts if c[0] not in chosen]
        resolve_conflict(p["content"], agent_id, p["scope"], p["source_message_id"],
                         chosen, untouched, save_new)
        st.session_state.pending = None
        st.rerun()
    if c2.button("Cancel — change nothing", key="conf_skip"):
        st.session_state.pending = None
        st.rerun()

feed = st.container()   # new turns render here, above the chat bar

# ---------- chat bar: input + memory tools + scope pills ----------
with st.container(key="chatbar"):
    col_input, col_tools = st.columns([0.92, 0.08], vertical_alignment="center")
    with col_input:     # inside a container -> chat_input renders inline
        user_message = st.chat_input(f"Message {agent[1]}…", key="chat")
    with col_tools.popover(":material/more_horiz:", help="Memory tools"):
        memory_tools()
    scope_pills()
scope = st.session_state.scope or "private"   # pills can be deselected

# ---------- chat input -> full workflow loop ----------
if user_message:
    st.session_state.pending = None
    if hero:
        hero.empty()

    autoname_conversation(conv_id, user_message)   # title the thread from 1st msg
    user_msg_id = log_message(agent_id, "user", user_message, conversation_id=conv_id)
    history.append(("user", user_message))
    with feed, st.chat_message("user", avatar=AVATAR["user"]):
        st.write(user_message)

    # 1) retrieve relevant memories (per agent, private + shared, non-superseded)
    memories = retrieve(agent_id, user_message, k=5)

    # 2) assemble the prompt
    context = assemble_context(persona, memories, history[-6:], user_message)

    # 3) generate the reply
    with feed, st.spinner("Thinking…"):
        reply = generate_reply(context)
    log_message(agent_id, "agent", reply, conversation_id=conv_id)
    history.append(("agent", reply))
    with feed, st.chat_message("assistant", avatar=AVATAR["assistant"]):
        st.write(reply)
        if memories:
            with st.expander("Memories used (ranked)", icon=":material/psychology:"):
                for m in memories:
                    st.caption(f"[priority {m[2]} · sim {m[4]:.2f}]  {m[1]}")

    # 4) only statements become memories — questions are just retrieval cues
    if is_fact(user_message):
        conflicts = find_conflicts(agent_id, user_message)
        if conflicts:
            st.session_state.pending = {"content": user_message,
                                        "scope": scope,
                                        "conflicts": conflicts,
                                        "source_message_id": user_msg_id}
            st.rerun()
        else:
            store_memory(user_message, agent_id, scope, source_message_id=user_msg_id)

    st.rerun()   # refresh so a newly-named thread shows its title in the sidebar
