import os
import requests
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

SUPPORTED_LANGUAGES = {
    "English (EN)": "en",
    "German (DE)": "de",
    "French (FR)": "fr",
    "Spanish (ES)": "es",
}

st.set_page_config(page_title="Stories", page_icon="📚", layout="wide")
st.title("📚 Stories")

PAGE_SIZE = 25
page = st.number_input("Seite", min_value=1, value=1, step=1)

try:
    resp = requests.get(
        f"{API_BASE}/stories/",
        params={"limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
        timeout=10,
    )
    resp.raise_for_status()
    stories = resp.json()
except requests.exceptions.ConnectionError:
    st.error("❌ Cannot connect to API.")
    st.stop()
except Exception as e:
    st.error(f"❌ Error: {e}")
    st.stop()

if not stories:
    st.info("No stories on this page. Go to **Ingest** to add one.")
    st.stop()

for story in stories:
    # Build expander label — show parent link for follow-ups
    _mode_icons = {"sequel": "🔁", "prequel": "⏮", "expansion": "🔍", "continuation": "➡"}
    _followup_label = ""
    if story.get("followup_mode") and story.get("parent_story_id"):
        _icon = _mode_icons.get(story["followup_mode"], "↩")
        # Find parent title from already-loaded stories list
        _parent = next((s for s in stories if s["id"] == story["parent_story_id"]), None)
        _parent_name = (_parent["title"] or f"#{story['parent_story_id']}") if _parent else f"#{story['parent_story_id']}"
        _followup_label = f" {_icon} {story['followup_mode'].capitalize()} von: {_parent_name}"

    _open_ending_badge = " 🔓 Offenes Ende" if story.get("open_ending") else ""
    if story.get("rewritten_from_id"):
        _open_ending_badge += f" ✍ Rewrite von #{story['rewritten_from_id']}"
    if story.get("rewrite_requested"):
        _open_ending_badge += " ✍ zum Rewrite markiert"

    with st.expander(
        f"Story #{story['id']} — {story['title'] or story['created_at'][:19].replace('T', ' ')}{_followup_label}{_open_ending_badge}",
        expanded=False,
    ):
        st.text_area(
            f"Original Text ({story.get('source_language', 'en').upper()})",
            value=story["original_text_en"][:500] + ("..." if len(story["original_text_en"]) > 500 else ""),
            height=100,
            disabled=True,
            key=f"text_{story['id']}",
        )
        if story.get("source_url"):
            st.markdown(f"**Source:** [{story['source_url']}]({story['source_url']})")

        if story.get("open_ending"):
            st.info("🔓 **Offenes Ende** — letzter Part endet als Cliffhanger (kein Abschluss)")
        else:
            st.caption("✅ Geschlossenes Ende — Story hat eine Auflösung")

        translations = story.get("translations", [])
        if not translations:
            st.caption("No translations yet — pipeline may still be running.")
        else:
            for tr in translations:
                st.markdown(f"#### 🌐 Language: `{tr['language_code'].upper()}`")
                parts = tr.get("video_parts", [])
                if not parts:
                    st.caption("No video parts yet.")
                    continue

                rows = []
                for p in parts:
                    jobs = p.get("render_jobs", [])
                    latest_status = jobs[-1]["status"] if jobs else "—"
                    rows.append({
                        "Part #": p["part_number"],
                        "Cliffhanger Note": p.get("cliffhanger_note") or "—",
                        "Status": latest_status,
                        "Snippet (100 chars)": p["text_snippet"][:100] + "...",
                    })

                st.dataframe(rows, use_container_width=True)

                # ── Copy Text toggle ──────────────────────────────────────
                copy_key = f"copy_text_{story['id']}_{tr['language_code']}"
                if st.button(
                    "📋 Text anzeigen / kopieren",
                    key=f"btn_copy_{story['id']}_{tr['language_code']}",
                ):
                    st.session_state[copy_key] = not st.session_state.get(copy_key, False)

                if st.session_state.get(copy_key, False):
                    assembled = "\n\n".join(
                        p["text_snippet"]
                        for p in sorted(parts, key=lambda x: x["part_number"])
                    )
                    st.code(assembled, language=None, wrap_lines=True)

        # ── Follow-up generator ───────────────────────────────────────────
        st.markdown("---")
        sequel_key = f"sequel_{story['id']}"
        sequel_lang_key = f"sequel_lang_{story['id']}"
        sequel_mode_key = f"sequel_mode_{story['id']}"
        sequel_oe_key = f"sequel_oe_{story['id']}"

        FOLLOWUP_MODES = {
            "🔁 Sequel — direkte Fortsetzung": "sequel",
            "⏮ Prequel — Vorgeschichte / Ursprung der Bedrohung": "prequel",
            "🔍 Expansion — zusätzliche Szenen, andere Perspektive": "expansion",
        }

        col_mode, col_lang, col_btn = st.columns([3, 2, 1])
        with col_mode:
            mode_label = st.selectbox(
                "Art der Fortsetzung",
                options=list(FOLLOWUP_MODES.keys()),
                key=f"mode_sel_{story['id']}",
            )
        with col_lang:
            lang_label = st.selectbox(
                "Sprache",
                options=list(SUPPORTED_LANGUAGES.keys()),
                key=f"lang_sel_{story['id']}",
            )
        with col_btn:
            st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
            gen_sequel = st.button("✨ Generieren", key=f"gen_seq_{story['id']}")
        open_ending_fup = st.checkbox(
            "\U0001f513 Offenes Ende (letzter Part als Cliffhanger)",
            value=False,
            key=f"oe_fup_{story['id']}",
        )
        if gen_sequel:
            lang_code = SUPPORTED_LANGUAGES[lang_label]
            mode_code = FOLLOWUP_MODES[mode_label]
            with st.spinner(f"Das {mode_label.split()[1]} wird geschrieben... (kann 1-3 Minuten dauern)"):
                try:
                    r = requests.post(
                        f"{API_BASE}/stories/followup-preview",
                        json={
                            "parent_story_id": story["id"],
                            "language": lang_code,
                            "mode": mode_code,
                            "open_ending": open_ending_fup,
                        },
                        timeout=360,
                    )
                    r.raise_for_status()
                    st.session_state[sequel_key] = r.json()
                    st.session_state[sequel_lang_key] = lang_code
                    st.session_state[sequel_mode_key] = mode_code
                    st.session_state[sequel_oe_key] = open_ending_fup
                except requests.exceptions.HTTPError as e:
                    st.error(f"API-Fehler {e.response.status_code}: {e.response.text}")
                except Exception as e:
                    st.error(f"Fehler: {e}")

        if sequel_key in st.session_state:
            preview = st.session_state[sequel_key]
            lang_code = st.session_state.get(sequel_lang_key, "en")

            st.success(
                f"Sequel generiert — {preview['total_parts']} Parts, "
                f"ca. {preview['total_words']} Woerter"
            )
            if preview.get("title"):
                st.markdown(f"**Titel:** {preview['title']}")

            with st.expander("Sequel-Vorschau", expanded=True):
                for part in preview["parts"]:
                    st.markdown(f"**Part {part['part_number']}** — {part['word_count']} Woerter")
                    st.markdown(f"*Cliffhanger: {part['cliffhanger_note']}*")
                    st.text(part["text_snippet"])
                    st.divider()

            if st.button("✅ Sequel an Render-Queue senden", key=f"submit_seq_{story['id']}"):
                payload = {
                    "original_text_en": preview["assembled_text"],
                    "source_language": lang_code,
                    "parts": preview["parts"],
                    "title": preview.get("title") or None,
                    "source_url": None,
                    "languages": [lang_code],
                    "parent_story_id": story["id"],
                    "followup_mode": st.session_state.get(sequel_mode_key),
                    "open_ending": st.session_state.get(sequel_oe_key, False),
                }
                with st.spinner("Sequel wird gespeichert und Pipeline gestartet..."):
                    try:
                        r2 = requests.post(f"{API_BASE}/stories/", json=payload, timeout=30)
                        r2.raise_for_status()
                        new_id = r2.json()["id"]
                        st.success(f"Sequel als Story #{new_id} in der Queue!")
                        del st.session_state[sequel_key]
                        del st.session_state[sequel_lang_key]
                    except requests.exceptions.HTTPError as e:
                        st.error(f"API-Fehler {e.response.status_code}: {e.response.text}")
                    except Exception as e:
                        st.error(f"Fehler: {e}")
