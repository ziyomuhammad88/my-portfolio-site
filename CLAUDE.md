# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Telegram bot (`bot.py`) that walks a trader through a step-by-step questionnaire about a trading session (screenshot + template fields), turns the answers into a polished channel post via Gemini, and only publishes to the channel after the admin approves a draft in a Telegram chat. It also keeps a local history (SQLite) of published sessions and generates weekly/monthly summary reports (totals, winrate, per-instrument breakdown, recurring-mistake analysis) on a schedule, with the same draft/confirm gate before anything reaches the channel. Everything — handlers, prompts, formatting/aggregation logic — lives in `bot.py`; there is no package structure (it does now own one local data file, `history.db`, alongside the code).

## Commands

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # then fill in all values
python3 bot.py                 # run the bot (polling)

python3 -m unittest test_bot.py                        # run all tests
python3 -m unittest test_bot.SplitTelegramTextTests.test_long_text_is_split_without_loss  # single test
```

There is no linter or build step configured.

## Configuration

All config comes from environment variables loaded via `.env` (see `.env.example`): `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `ADMIN_USER_ID`, `CHANNEL_ID` are required. Optional: `GEMINI_MODEL` (default `gemini-3.8-flash`), `INSTRUMENTS`/`DAY_RESULTS` (comma-separated lists that turn the matching wizard step into inline buttons instead of free text — see `HEADER_CHOICE_OPTIONS`), `REPORT_TIMEZONE` (default `Asia/Tashkent`, drives the report schedule and the date a session is filed under), `DB_PATH` (default: `history.db` next to `bot.py`). Optional vars use `os.environ.get(NAME) or default` (not the two-arg `.get(NAME, default)` form) so an explicitly-blank line in `.env` still falls back to the default instead of e.g. handing `sqlite3.connect("")` an empty path. `.env` is gitignored and must never be committed — only `.env.example` (with empty/illustrative values) is tracked. `history.db` (matched by a `*.db` gitignore pattern) holds real trading history and must never be committed either.

## Architecture

**Gemini via the OpenAI SDK.** Gemini exposes an OpenAI-compatible endpoint, so `bot.py` talks to it with the plain `openai` package pointed at `https://generativelanguage.googleapis.com/v1beta/openai/` (see `gemini_client`) rather than a Google-specific SDK. All model calls go through `get_completion()`.

**Wizard state machine collects structured answers before Gemini ever sees them.** `begin_wizard`/`start_wizard` reset `context.user_data["wizard"]`, then `ask_current_wizard_step`/`handle_wizard_photo`/`handle_wizard_answer` walk the admin through: a screenshot, header fields (`HEADER_STEPS`), then per-trade fields (`TRADE_STEPS`) repeated `trade_count` times. Some header fields show inline-keyboard buttons instead of free text when `HEADER_CHOICE_OPTIONS` has options for that key (`choice_keyboard`, `handle_header_choice_selection`) — the "Новости" step additionally special-cases a "Да" tap to ask a same-step follow-up instead of advancing (see `NEWS_FOLLOWUP_PROMPT`). `finish_wizard` serializes the collected `answers`/`trades` into a text note (`build_raw_comment`) and hands both the note *and* the structured dicts to `create_draft_from_source`.

**Draft/confirm state machine, not a stateless bot.** `create_draft_from_source` builds a draft via Gemini (`format_post`) → `send_draft` posts it with inline "Опубликовать"/"Отмена" buttons and stores it (photo, formatted text, *and* the structured `answers`/`trades` from the wizard) in `context.user_data["draft"]` → admin either edits (free-text message or `/edit`, routed through `apply_revision` → `revise_post`) which replaces the draft, or confirms (button callback or `/post`) which calls `publish_draft` to send to `CHANNEL_ID` and `persist_published_session` to write a row into `history.db`. Only one draft is held per user at a time; a new draft/edit invalidates the previous message's buttons via `clear_draft_keyboard`. Each draft carries a `uuid4` `id` so stale button presses on an old draft (after a newer one replaced it) are detected and rejected in `handle_callback`. `send_draft` must carry `answers`/`trades` over from the previous draft on every call (it rebuilds the dict from scratch each time) — losing that wiring silently breaks history persistence for any session that gets edited before publish.

**Every request is scoped to one admin.** `is_admin()` gates every handler and every callback against `ADMIN_USER_ID` — this is the only access control in the bot, there is no multi-user support.

**Post structure and tone are entirely prompt-defined.** `SYSTEM_PROMPT` in `bot.py` encodes the required post template (labeled fields, per-trade blocks) and hard rules (no invented facts, no markdown, Russian, ≤3500 chars, rewrite free-text fields livelier rather than copying verbatim). To change how posts read, edit this prompt only — no other code changes needed.

**Telegram's 4096-char message limit is handled manually.** `split_telegram_text()` splits a formatted post on the nearest newline/space boundary without losing or duplicating text; draft display, final publishing, and reports all go through it (photo posts additionally collapse into a single photo-caption message when they fit `MAX_TELEGRAM_CAPTION_LENGTH`, via `send_draft`/`publish_draft`).

**Weekly/monthly reports: Python computes every number, Gemini only writes prose and finds the mistake pattern.** Every published session is persisted by `save_session` (table `sessions` + `trades` in `history.db`, opened per-call via `get_connection` — always explicitly `.close()`d, since `with conn:` in sqlite3 commits but does *not* close the connection). `parse_signed_amount`/`classify_trade_outcome` turn free-text results (`"-80$"`, `"-15 пунктов"`) into numbers/win-loss-breakeven at write time. `aggregate_report_stats` (pure, DB-free) computes the period total, winrate, and a per-instrument good/bad/insufficient-data verdict from `get_sessions_in_range`'s output; `collect_mistake_notes` gathers the raw mistake/psych-mistake text for the period. Only then does `build_report_text` call Gemini (`REPORT_SYSTEM_PROMPT`, a separate prompt from `SYSTEM_PROMPT`) — it's handed the already-computed numbers and told never to recalculate them, and does original reasoning only to name the period's one recurring mistake from `collect_mistake_notes`'s text. `run_report_pipeline` is the shared entry point for both the scheduled jobs (`weekly_report_job`/`monthly_report_job`, registered via `application.job_queue` in `main()` — requires the `python-telegram-bot[job-queue]` extra and, on Windows, the `tzdata` package for `zoneinfo`) and the manual `/weekreport`/`/monthreport` commands. Report drafts use a *separate* confirm/cancel flow from daily posts — `context.user_data["report_draft"]` and `report_publish:`/`report_cancel:` callback prefixes (`handle_report_callback`), dispatched in `handle_callback` before the daily-draft branch — because reports have no single photo and must not collide with an in-flight daily draft.

## Git workflow

Every change made through Claude Code in this repo should be committed (and pushed to `origin/main` on GitHub) with a descriptive message — the commit history is the review trail the project owner's mentor uses. No separate changelog file is kept; the git log is the log.
