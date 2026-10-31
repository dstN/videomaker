import os
import requests
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

st.set_page_config(page_title="Story Generator", page_icon="✨", layout="centered")
st.title("✨ Story Generator")
st.caption("Gib einen Prompt ein — Claude (oder Gemini, je nach STORY_LLM) schreibt die r/nosleep Story, danach wird sie in Video-Parts aufgeteilt.")

SUPPORTED_LANGUAGES = {
    "English (EN)": "en",
    "German (DE)": "de",
    "French (FR)": "fr",
    "Spanish (ES)": "es",
}

# Step 1: Prompt input
with st.form("prompt_form"):
    prompt = st.text_area(
        "Story Prompt",
        height=120,
        placeholder='z.B. "Ich habe ein altes Radio auf dem Dachboden gefunden. Es sendet Stimmen aus der Zukunft."',
    )
    language_label = st.selectbox(
        "Sprache",
        options=list(SUPPORTED_LANGUAGES.keys()),
        index=0,
    )
    open_ending = st.checkbox(
        "Offenes Ende (letzter Part endet als Cliffhanger, lädt zu einem Sequel ein)",
        value=False,
    )
    generate_btn = st.form_submit_button("Story generieren", type="primary")

# Generate preview
if generate_btn:
    if not prompt.strip():
        st.error("Bitte gib einen Prompt ein.")
        st.stop()

    lang_code = SUPPORTED_LANGUAGES[language_label]

    with st.spinner("Die Story wird geschrieben... (kann 1-3 Minuten dauern)"):
        try:
            resp = requests.post(
                f"{API_BASE}/stories/generate-preview",
                json={"prompt": prompt.strip(), "language": lang_code, "open_ending": open_ending},
                timeout=360,
            )
            resp.raise_for_status()
            preview = resp.json()
            st.session_state["preview"] = preview
            st.session_state["preview_lang"] = lang_code
            st.session_state["preview_open_ending"] = open_ending
        except requests.exceptions.ConnectionError:
            st.error("Kein API-Kontakt. Laeuft der Backend-Container?")
            st.stop()
        except requests.exceptions.HTTPError as e:
            st.error(f"API-Fehler {e.response.status_code}: {e.response.text}")
            st.stop()
        except Exception as e:
            st.error(f"Unerwarteter Fehler: {e}")
            st.stop()

# Step 2: Preview
if "preview" in st.session_state:
    preview = st.session_state["preview"]
    lang_code = st.session_state["preview_lang"]

    st.success(
        f"Story generiert -- {preview['total_parts']} Parts, "
        f"ca. {preview['total_words']} Woerter"
    )

    if preview.get("title"):
        st.markdown(f"## {preview['title']}")

    with st.expander("Story-Vorschau (alle Parts)", expanded=True):
        for part in preview["parts"]:
            st.markdown(f"**Part {part['part_number']}** - {part['word_count']} Woerter")
            st.markdown(f"*Cliffhanger: {part['cliffhanger_note']}*")
            st.text(part["text_snippet"])
            st.divider()

    st.markdown("---")
    st.markdown("Wenn die Story passt, sende sie an die Render-Pipeline:")

    # Step 3: Send to render queue
    if st.button("An Render-Queue senden", type="primary"):
        payload = {
            "original_text_en": preview["assembled_text"],
            "source_language": lang_code,
            "parts": preview["parts"],
            "title": preview.get("title") or None,
            "source_url": None,
            "languages": [lang_code],
            "open_ending": st.session_state.get("preview_open_ending", False),
        }
        with st.spinner("Story wird gespeichert und Pipeline gestartet..."):
            try:
                resp = requests.post(
                    f"{API_BASE}/stories/",
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                story_id = data["id"]
                st.success(f"Story #{story_id} ist in der Queue!")
                st.info(
                    "Voxtral TTS -> WhisperX -> ffmpeg laeuft im Hintergrund. "
                    "Fortschritt auf der Render Queue-Seite verfolgen."
                )
                del st.session_state["preview"]
                del st.session_state["preview_lang"]
            except requests.exceptions.ConnectionError:
                st.error("Kein API-Kontakt.")
            except requests.exceptions.HTTPError as e:
                st.error(f"API-Fehler {e.response.status_code}: {e.response.text}")
            except Exception as e:
                st.error(f"Unerwarteter Fehler: {e}")
