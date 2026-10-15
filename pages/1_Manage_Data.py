"""
CRUD management page — view / edit / delete memories and agents.
Streamlit auto-adds this to the sidebar as a second page.
"""

import streamlit as st
import datetime as _dt
from memory import (list_memories, update_memory, delete_memory,
                    list_agents, create_agent, update_agent, delete_agent,
                    load_messages, list_audit)

st.set_page_config(page_title="Manage Data", page_icon=":material/database:")
st.page_link("app.py", label="Back to chat", icon=":material/arrow_back:")
st.title("Manage Data (CRUD)")

agents = list_agents()
if not agents:
    st.warning("No agents yet. Add one below.")

by_name = {a[1]: a for a in agents}

tab_mem, tab_msg, tab_audit, tab_agent = st.tabs(
    ["Memories", "Messages", "Audit log", "Agents"])

# ============================================================
#  MEMORIES  — Read / Update / Delete
# ============================================================
with tab_mem:
    if agents:
        who = st.selectbox("Show memories for", list(by_name.keys()))
        agent_id = by_name[who][0]
        show_all = st.checkbox("Include superseded (retired) memories", value=False)

        rows = list_memories(agent_id, include_superseded=show_all)
        st.caption(f"{len(rows)} memory row(s).")

        now = _dt.datetime.now().astimezone()   # aware, to compare with TIMESTAMPTZ
        for (mid, scope, content, priority, access, created,
             superseded, valid_until, src_msg) in rows:
            if valid_until is not None:
                valid_until = valid_until.astimezone()   # show in local time
            expired = valid_until is not None and valid_until < now
            tag = ""
            if superseded:
                tag += " · ~~retired~~"
            if expired:
                tag += " · :material/schedule: expired"
            with st.expander(f"#{mid} · {scope} · p{priority} · used {access}×{tag}"):
                # provenance: which message this fact came from (normalization link)
                if src_msg:
                    st.caption(f":material/attach_file: from message #{src_msg}")
                new_text = st.text_area("Content", value=content, key=f"txt_{mid}")
                new_pri = st.slider("Priority", 1, 5, int(priority), key=f"pri_{mid}")

                has_exp = st.checkbox("Has expiry date", value=valid_until is not None,
                                      key=f"hasexp_{mid}")
                new_exp = None
                if has_exp:
                    d = st.date_input("Expires on",
                                      value=(valid_until.date() if valid_until else _dt.date.today()),
                                      key=f"exp_{mid}")
                    new_exp = _dt.datetime.combine(d, _dt.time(23, 59, 59)).astimezone()
                    if valid_until:
                        st.caption(f"Currently set to {valid_until:%d %b %Y}"
                                   f"{' — already expired' if expired else ''}")

                c1, c2 = st.columns(2)
                if c1.button("Save changes", icon=":material/save:", key=f"upd_{mid}"):
                    if has_exp:
                        update_memory(mid, content=new_text, priority=new_pri, valid_until=new_exp)
                    else:
                        update_memory(mid, content=new_text, priority=new_pri, clear_expiry=True)
                    st.success("Updated.")
                    st.rerun()
                if c2.button("Delete", icon=":material/delete:", key=f"del_{mid}"):
                    delete_memory(mid)
                    st.success("Deleted.")
                    st.rerun()

# ============================================================
#  MESSAGES  — Read (the raw conversation log)
# ============================================================
with tab_msg:
    if agents:
        who_m = st.selectbox("Show messages for", list(by_name.keys()), key="msg_agent")
        aid_m = by_name[who_m][0]
        msgs = load_messages(aid_m)
        st.caption(f"{len(msgs)} message(s) logged — the full transcript, "
                   f"including questions and greetings that are NOT stored as memories.")
        for role, content in msgs:
            who = "user" if role == "user" else "assistant"
            with st.chat_message(who, avatar=":material/person:" if who == "user" else ":material/smart_toy:"):
                st.write(content)

# ============================================================
#  AUDIT LOG  — Read (written automatically by the trg_memory_audit trigger)
# ============================================================
with tab_audit:
    if agents:
        who_a = st.selectbox("Show audit entries for", list(by_name.keys()), key="audit_agent")
        aid_a = by_name[who_a][0]
        entries = list_audit(aid_a)
        st.caption(f"{len(entries)} audit entr(ies). Every edit and delete is "
                   f"recorded by a database trigger — this log survives even a "
                   f"hard delete of the memory itself.")
        for (audit_id, mem_id, op, old_c, new_c, old_p, new_p, when) in entries:
            with st.expander(f"#{audit_id} · {op} on memory #{mem_id} · {when:%d %b %Y %H:%M}"):
                if op == "DELETE":
                    st.caption(f"Deleted content: {old_c}")
                    st.caption(f"Priority at deletion: {old_p}")
                else:  # UPDATE / SUPERSEDE / RESTORE
                    if op != "UPDATE":
                        st.caption(f"{'Retired' if op == 'SUPERSEDE' else 'Restored'}: {new_c}")
                    if old_c != new_c:
                        st.caption(f"**Content:** ~~{old_c}~~ → {new_c}")
                    if old_p != new_p:
                        st.caption(f"**Priority:** {old_p} → {new_p}")

# ============================================================
#  AGENTS  — Create / Read / Update / Delete
# ============================================================
with tab_agent:
    st.subheader("Add a new agent")
    n = st.text_input("Name", key="new_agent_name")
    d = st.text_input("Description", key="new_agent_desc")
    if st.button("Create agent", icon=":material/add:"):
        if n.strip():
            create_agent(n.strip(), d.strip())
            st.success(f"Created agent '{n}'.")
            st.rerun()
        else:
            st.error("Name can't be empty.")

    st.divider()
    st.subheader("Existing agents")
    for (aid, name, desc) in agents:
        with st.expander(f"#{aid} · {name}"):
            en = st.text_input("Name", value=name, key=f"an_{aid}")
            ed = st.text_input("Description", value=desc or "", key=f"ad_{aid}")
            c1, c2 = st.columns(2)
            if c1.button("Save", icon=":material/save:", key=f"aupd_{aid}"):
                update_agent(aid, en.strip(), ed.strip())
                st.success("Updated.")
                st.rerun()
            if c2.button("Delete agent", icon=":material/delete:", key=f"adel_{aid}"):
                delete_agent(aid)
                st.warning(f"Deleted '{name}' and its private data.")
                st.rerun()
