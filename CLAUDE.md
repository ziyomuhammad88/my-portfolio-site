# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Telegram bot (`bot.py`) that turns a trader's raw trade note + screenshot into a polished channel post via Gemini, and only publishes to the channel after the admin approves a draft in a Telegram chat. Everything — handlers, prompt, formatting logic — lives in `bot.py`; there is no package structure.

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

All config comes from environment variables loaded via `.env` (see `.env.example`): `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `ADMIN_USER_ID`, `CHANNEL_ID`, and optional `GEMINI_MODEL` (default `gemini-3.8-flash`). `.env` is gitignored and must never be committed — only `.env.example` (with empty values) is tracked.

## Architecture

**Gemini via the OpenAI SDK.** Gemini exposes an OpenAI-compatible endpoint, so `bot.py` talks to it with the plain `openai` package pointed at `https://generativelanguage.googleapis.com/v1beta/openai/` (see `gemini_client`) rather than a Google-specific SDK. All model calls go through `get_completion()`.

**Draft/confirm state machine, not a stateless bot.** The core flow is: photo+caption → `create_draft_from_source` builds a draft via Gemini → `send_draft` posts it with inline "Опубликовать"/"Отмена" buttons and stores it in `context.user_data["draft"]` → admin either edits (free-text message or `/edit`, routed through `apply_revision` → `revise_post`) which replaces the draft, or confirms (button callback or `/post`) which calls `publish_draft` to send to `CHANNEL_ID`. Only one draft is held per user at a time; a new draft/edit invalidates the previous message's buttons via `clear_draft_keyboard`. Each draft carries a `uuid4` `id` so stale button presses on an old draft (after a newer one replaced it) are detected and rejected in `handle_callback`.

**Every request is scoped to one admin.** `is_admin()` gates every handler and every callback against `ADMIN_USER_ID` — this is the only access control in the bot, there is no multi-user support.

**Post structure and tone are entirely prompt-defined.** `SYSTEM_PROMPT` in `bot.py` encodes the required post structure (opening $ result line, bullet stats, narrative paragraphs, closing reflection) and hard rules (no invented facts, no markdown, Russian, ≤3500 chars). To change how posts read, edit this prompt only — no other code changes needed.

**Telegram's 4096-char message limit is handled manually.** `split_telegram_text()` splits a formatted post on the nearest newline/space boundary without losing or duplicating text; both draft display (`send_draft`) and final publishing (`publish_draft`) go through it. This is the one piece of logic covered by `test_bot.py`.

## Git workflow

Every change made through Claude Code in this repo should be committed (and pushed to `origin/main` on GitHub) with a descriptive message — the commit history is the review trail the project owner's mentor uses. No separate changelog file is kept; the git log is the log.
