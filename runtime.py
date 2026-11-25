import os


def is_streamlit_cloud():
    return os.getcwd().startswith("/mount")


def apply_streamlit_secrets():
    try:
        import streamlit as st
    except ImportError:
        return
    # Settings that may be set in Streamlit Cloud > App settings > Secrets.
    # Copied to environment variables so the background job sees them too.
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "ESG_ENGINE", "ESG_PARALLEL"):
        try:
            value = st.secrets.get(name)
        except Exception:
            return
        if value:
            os.environ[name] = str(value)
