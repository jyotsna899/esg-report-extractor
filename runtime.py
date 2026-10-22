import os


def is_streamlit_cloud():
    return os.getcwd().startswith("/mount")


def apply_streamlit_secrets():
    try:
        import streamlit as st
    except ImportError:
        return
    try:
        key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        return
    if key:
        os.environ["GEMINI_API_KEY"] = str(key)
