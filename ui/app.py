import os
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

st.set_page_config(
    page_title="Infinity Mystery Video Factory",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Infinity Mystery Video Factory")
st.caption("Automated horror story → vertical video pipeline")

st.markdown(
    """
    **Navigation:**
    - **Ingest** — Submit a new story and select target languages
    - **Stories** — Browse stories, translations, and video parts
    - **Render Queue** — Monitor and retry render jobs
    """
)
