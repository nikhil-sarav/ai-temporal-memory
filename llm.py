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