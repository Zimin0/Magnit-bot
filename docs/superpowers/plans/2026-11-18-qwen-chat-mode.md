# Qwen Chat Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task.

**Goal:** Add an explicit Telegram keyboard mode that sends free-form messages to Qwen without conflicting with the legacy product menu.

**Architecture:** `BotMenu` owns the boolean chat-mode state and resets any manual cart flow on transition. `telegram_bot` handles the two mode buttons before generic menu routing; only free text in the enabled mode starts a Qwen basket proposal.

**Tech Stack:** Python, aiogram-compatible Telegram update handling, unittest.

**Spec:** User request in this chat.

## Global Constraints

- Keep the existing product/cart editor usable outside Qwen mode.
- Use the project virtual environment; do not use `uv run`.

## Review Focus

- A free-text request must reach Qwen only after the mode is enabled.
- Mode buttons must not be interpreted as a basket request.
- Leaving or resetting the mode must restore the normal keyboard and invalidate a pending proposal.

## Task 1: Cover the mode transition

- [ ] Add unit tests for enable, free-text routing, exit, and `/start` reset.
- [ ] Run the focused tests and observe failure before production changes.

## Task 2: Implement the switch

- [ ] Add Qwen mode buttons and explicit state transitions to `telegram_menu.py`.
- [ ] Route mode-state free text to `start_proposal` in `telegram_bot.py`; preserve manual menu flow otherwise.
- [ ] Clear pending proposal state on mode transition.
- [ ] Run focused and complete test suites using `.venv\\Scripts\\python.exe`.
