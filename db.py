"""Database connection helper."""

import streamlit as st
import psycopg2

def get_conn():
    return psycopg2.connect(st.secrets["NEON_CONN_STRING"])