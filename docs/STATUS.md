# Project Status & Handoff

Last updated: 2026-10-31. Read this together with `CLAUDE.md` at the start of a session.

## 1. Where things stand

| Branch | Content | State |
|---|---|---|
| `master` | Original code as found on 2026-10-31 (initial commit) | Live in the running containers |
| `fix/pipeline-stuck-states` | Poller/recovery fixes (first 2 commits) | Contained in `fix/review-findings` |
| `fix/review-findings` | All review fixes + Claude integration + recaps (15 commits) | Pushed, **not merged, no PR** (user asked for no PR yet) |

Repo: private `github.com/dstN/videomaker`. Commits never carry a `Co-Authored-By` trailer (user rule).

`./app` is bind-mounted into the `api` container: checking out `fix/review-findings` changes the code the running API loads. Keep the working tree on `master` until the user merges.

## 2. Product decisions (from the user — do not change without asking)

- **Part length 60-180 s, 180 s is a hard limit** (YouTube Shorts). 120-240 s was tried and rejected for that reason.
- **Max. 6 parts per story.** Longer stories become a series of linked stories ("<title> – Part 1/2…"), front-loaded: 7 → 4/3, 8 → 4/4, 9 → 5/4, 10 → 5/5, 13 → 5/4/4. Implemented in `store_parts()`.
- **Recap sentence** at the start of every part after the first (orientation for viewers starting mid-series).
- **Claude writes stories** (new, follow-ups, rewrites), Gemini splits / describes / translates and is the fallback. Budget: ~$100/month on the user's Anthropic key.
- **Own stories (no `source_url`, #1-#50) get a full Claude rewrite** into new stories; originals and their exported videos stay untouched.
- **Reddit stories / `rewrite_story()`** (466 stories, docstring says "bypass copyright"): left unchanged on purpose. Copyright/ToS risk was raised; the decision is the user's. Do not improve that path.
- `CLAUDE.md` was aligned to the code (Voxtral on Windows GPU, ffmpeg instead of MoviePy, File API instead of `caches.create`, float16). The user may still veto individual points.

## 3. What the user still has to run (in this order)

1. Merge `fix/review-findings`, then `docker compose build api ui whisperx && docker compose up -d`.
   Without the rebuild the API crash-loops (`num2words`, `anthropic`, `pypdf` missing) and WhisperX keeps doing ASR.
2. Add `ANTHROPIC_API_KEY=…` to `.env` (optional: `VOXTRAL_URL`, see `.env.example`). Host scripts need `pip install anthropic pypdf num2words`.
3. `python cleanup_orphan_rows.py` → `--apply` (199 translations / 447 parts / 1745 jobs without a story).
4. `python resplit_unstarted_stories.py` → `--apply` (335 unstarted stories still have the old Gemini split; their parts would now fail the 180 s check).
5. Render **one** story end to end and check it before letting the queue run.
6. `python mark_for_rewrite.py --apply`, then `python rewrite_marked_stories.py --apply --limit 1`, check the result, then without `--limit` (≈ $6 estimated for 50 stories).

All scripts are dry runs unless `--apply` is passed. None of them was run against the live DB.

## 4. Not verified live (only mocked / offline)

- Claude API calls (no key in the session), Gemini split plans, recap generation.
- WhisperX forced alignment on the GPU (single segment up to ~3 min; falls back to ASR on errors).
- Voxtral retries, `_fit_duration` speed-up, nvenc rendering of the new limits.

Offline evidence: 70 pytest tests (`pytest`, see `requirements-dev.txt`); the local splitter was run over all 516 stories (no text loss, parts 238-440 words, 419 single stories / 94 two-part / 3 three-part series).

## 5. Open items / ideas

- Voxtral client sends `euler_steps=8` (`TTS_EULER_STEPS`), overriding the PS1's 3 — probable cause of the 154 Voxtral timeouts. Suggested `4`; user's call.
- German stories use English voice presets; Voxtral has 20 voices in 9 languages (names not checked).
- `RenderJob.file_path` points to deleted files after the Drive export (documented only).
- Voxtral host IP is still hard-coded as default (documented in `.env.example`).
- Speech rate (2.65-3.2 words/s) was measured on only 11 WAVs; if jobs fail with "over the … narration limit", lower `PART_MAX_WORDS`.
- No real migrations: `init_db()` only adds missing columns (Alembic would be the next step).

## 6. Working conventions that proved useful

- Inspect the live DB read-only (`sqlite3 'file:data/videomaker.db?mode=ro'`); test scripts against a copy in a scratch dir.
- Tests: `pytest` from the repo root (`pytest.ini` limits collection to `tests/`; `test_retry.py` in the root is an ad-hoc script, not a test).
- Commit only when `pytest` exits 0; one commit per topic.
