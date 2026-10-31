import os
import time
import requests
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

POLL_INTERVAL = 3  # seconds

STATUS_COLORS = {
    "pending":     "⬜",
    "audio_done":  "🔵",
    "sync_done":   "🩵",
    "rendering":   "🟡",
    "completed":   "🟢",
    "failed":      "🔴",
}

st.set_page_config(page_title="Render Queue", page_icon="🎬", layout="wide")
st.title("🎬 Render Queue")

auto_refresh = st.toggle("Auto-refresh (every 3s)", value=True)
refresh_placeholder = st.empty()

def fetch_jobs() -> list:
    try:
        resp = requests.get(f"{API_BASE}/jobs/", timeout=10)
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot connect to API.")
        return []
    except Exception as e:
        st.error(f"❌ Error fetching jobs: {e}")
        return []


def retry_job(job_id: int) -> None:
    try:
        resp = requests.post(f"{API_BASE}/jobs/{job_id}/retry", timeout=10)
        resp.raise_for_status()
        st.success(f"Retry enqueued for Job #{job_id}")
    except Exception as e:
        st.error(f"Retry failed: {e}")


def render_queue(jobs: list) -> None:
    if not jobs:
        st.info("No render jobs found.")
        return

    cols = st.columns([1, 3, 1, 2, 3, 2, 1])
    cols[0].markdown("**Job**")
    cols[1].markdown("**Story**")
    cols[2].markdown("**Part**")
    cols[3].markdown("**Status**")
    cols[4].markdown("**Error / File**")
    cols[5].markdown("**Updated**")
    cols[6].markdown("**Action**")
    st.divider()

    for job in jobs:
        icon = STATUS_COLORS.get(job["status"], "❓")
        story_id = job.get("story_id") or "?"
        story_title = job.get("story_title") or f"Story #{story_id}"
        part_number = job.get("part_number") or "?"
        cols = st.columns([1, 3, 1, 2, 3, 2, 1])
        cols[0].write(f"#{job['id']}")
        cols[1].write(story_title)
        cols[2].write(f"Part {part_number}")
        cols[3].write(f"{icon} {job['status']}")

        if job["status"] == "failed" and job.get("error_message"):
            cols[4].caption(job["error_message"][:120])
        elif job["status"] == "completed" and job.get("file_path"):
            cols[4].caption(job["file_path"].split("/")[-1])
        else:
            cols[4].write("—")

        updated = (job.get("updated_at") or "")[:19].replace("T", " ")
        cols[5].write(updated)

        if job["status"] in ("failed", "completed"):
            if cols[6].button("↩ Retry", key=f"retry_{job['id']}"):
                retry_job(job["id"])
        else:
            cols[6].write("")


# ── Retry-all ─────────────────────────────────────────────────────────────────
st.markdown("---")
confirm_retry_all = st.checkbox(
    "✅ Ja, ich will ALLE Stories neu rendern (TTS + Sync + Render)"
)
if st.button("🔄 Alle neu rendern", disabled=not confirm_retry_all, type="primary"):
    try:
        r = requests.post(f"{API_BASE}/jobs/retry-all", timeout=30)
        r.raise_for_status()
        st.success(f"✓ {r.json()['queued']} Stories in die Queue gestellt.")
    except Exception as e:
        st.error(f"Fehler: {e}")
st.markdown("---")

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:
    jobs = fetch_jobs()
    with refresh_placeholder.container():
        render_queue(jobs)

    if not auto_refresh:
        break
    time.sleep(POLL_INTERVAL)
    st.rerun()
