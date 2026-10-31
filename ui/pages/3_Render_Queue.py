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

col_toggle, col_status, col_limit = st.columns([2, 2, 1])
with col_toggle:
    auto_refresh = st.toggle("Auto-refresh (every 3s)", value=True)
with col_status:
    status_filter = st.selectbox("Status", options=["alle"] + list(STATUS_COLORS), index=0)
with col_limit:
    limit = st.number_input("Anzahl", min_value=10, max_value=2000, value=100, step=50)
refresh_placeholder = st.empty()


def fetch_jobs() -> tuple[list, int]:
    params = {"limit": int(limit)}
    if status_filter != "alle":
        params["status"] = status_filter
    try:
        resp = requests.get(f"{API_BASE}/jobs/", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("jobs", []), data.get("total", 0)
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot connect to API.")
        return [], 0
    except Exception as e:
        st.error(f"❌ Error fetching jobs: {e}")
        return [], 0


def retry_job(job_id: int) -> None:
    try:
        resp = requests.post(f"{API_BASE}/jobs/{job_id}/retry", timeout=10)
        resp.raise_for_status()
        st.success(f"Retry enqueued for Job #{job_id}")
    except Exception as e:
        st.error(f"Retry failed: {e}")


def render_queue(jobs: list, total: int) -> None:
    if not jobs:
        st.info("No render jobs found.")
        return

    st.caption(f"{len(jobs)} von {total} Jobs")
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

        # Only failed jobs: completed ones are usually exported to Drive and
        # deleted locally, so a retry would re-render and re-export them.
        if job["status"] == "failed":
            if cols[6].button("↩ Retry", key=f"retry_{job['id']}"):
                retry_job(job["id"])
        else:
            cols[6].write("")


# ── Retry failed ──────────────────────────────────────────────────────────────
st.markdown("---")
if st.button("🔄 Alle fehlgeschlagenen Jobs erneut versuchen", type="primary"):
    try:
        r = requests.post(f"{API_BASE}/jobs/retry-failed", timeout=30)
        r.raise_for_status()
        st.success(f"✓ {r.json()['queued']} Stories in die Queue gestellt.")
    except Exception as e:
        st.error(f"Fehler: {e}")
st.markdown("---")

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:
    jobs, total = fetch_jobs()
    with refresh_placeholder.container():
        render_queue(jobs, total)

    if not auto_refresh:
        break
    time.sleep(POLL_INTERVAL)
    st.rerun()
