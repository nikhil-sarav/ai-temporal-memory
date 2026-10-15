# llm.py — The Reply Generator (Groq)

`llm.py` sends the finished prompt to a Large Language Model (LLM) hosted on
**Groq** and returns the reply text. It has one function: `generate_reply()`.

---

## 1. The code

```python
"""The LLM that writes the actual replies — Groq."""

import streamlit as st
from groq import Groq

client = Groq(api_key=st.secrets["GROQ_API_KEY"])

def generate_reply(context):
    try:
        resp = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": context}],
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"[LLM error] {e}"
```

---

## 2. Line by line

| Line | Meaning |
|---|---|
| `import streamlit as st` | Imported only to read `st.secrets`, which loads the API key from `.streamlit/secrets.toml`. The key is never written in the code, and the file is gitignored. |
| `from groq import Groq` | Groq's official Python SDK. |
| `client = Groq(api_key=...)` | Creates the API client **once**, when the module is first imported. Every reply reuses it. |
| `def generate_reply(context)` | Takes one string: the full prompt built by `assemble_context()` in `memory.py`. |
| `client.chat.completions.create(...)` | Sends the prompt to Groq's servers. Groq uses the same request format as OpenAI's API. |
| `model="openai/gpt-oss-20b"` | OpenAI's open-weight, 20-billion-parameter model, hosted by Groq. Free and fast. |
| `messages=[{"role": "user", ...}]` | The whole prompt goes in as a single user message. |
| `resp.choices[0].message.content` | The API can return several candidate replies; we take the first one's text. |
| `except Exception as e` | If the call fails (no internet, bad key, rate limit), it returns an error string instead of crashing. The chat shows `[LLM error] ...` and keeps running. |

---

## 3. Where it fits

`app.py` calls it once per user message:

```
user message
   -> retrieve()          (memory.py: top-5 relevant memories from PostgreSQL)
   -> assemble_context()  (memory.py: builds one prompt string)
   -> generate_reply()    (llm.py: sends it to Groq)
   -> reply shown in the chat and saved with log_message()
```

### What the prompt (`context`) looks like

```
You are <agent name>. <agent description>. Never use emojis.

What you remember about the user. These are current, verified, non-contradictory
facts — trust them and do not second-guess them:
- I live in Mumbai
- My DBMS exam is on 20 November 2026

Recent conversation:
user: ...
agent: ...

User: <new message>
Assistant:
```

---

## 4. How agents affect it

`llm.py` never sees an agent ID: every agent uses the **same** client and
model. Agents only change the `context` string that is passed in:

1. **Persona: who the model acts as** (`app.py`)
   ```python
   persona = f"You are {agent[1]}. {agent[2]}. Never use emojis."
   ```
   `agent[1]` is the name and `agent[2]` the description, both from the
   `agents` table.

2. **Memories: what the model knows** (`memory.retrieve`)
   ```sql
   WHERE (scope = 'shared' OR agent_id = %s)
   ```
   Only that agent's **private** memories plus the **shared** ones are
   included. Agent A's private facts never reach agent B's prompt.

```
agents table ------------------> persona ---+
memories (filtered by agent_id) ------------+--> assemble_context() --> context --> llm.py
messages (recent history) ------------------+
```

Adding a new agent needs **no code change**, only an `INSERT` into `agents`.

---

## 5. Key point: the LLM has no memory

Every call is **stateless**: the model remembers nothing between calls. The
bot seems to remember because each call packs the relevant facts from our
**PostgreSQL database** into the prompt. The memory lives in the DBMS, not
in the model, which is why this is a DBMS project.

---

## 6. Viva questions

- **Why Groq?** It has a free tier, it is very fast, and it runs open models.
  Cost is ₹0.
- **Why not send the whole chat history?** Prompts have a token limit, and
  cost and delay grow with size. We send only the top-5 relevant memories plus
  the last 6 messages.
- **Can the model be changed?** Yes. Change the one `model=` string, or point
  the client at another provider. Nothing else changes.
- **Why one user message instead of separate system and user roles?** A
  simplification: the persona text sits at the top of the prompt instead.
  Using a proper `system` role would be a valid improvement.
- **What happens if Groq is down?** The `try/except` returns an error message
  as the reply, so the app does not crash.
- **Is the API key safe?** Yes. It is read from `secrets.toml`, which is not
  committed to git.
