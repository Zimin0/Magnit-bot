# Qwen Magnit CLI Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe local Telegram conversation path where Qwen proposes a configured basket and confirmed execution uses `magnit-cli` through the project venv.

**Architecture:** Qwen is a constrained planner, never a shell executor. A deterministic adapter validates structured output, expands configured SKUs, calls an allowlisted CLI subprocess, verifies the complete resulting cart, and updates one Telegram message.

**Tech Stack:** Python 3.13, `unittest`, Pydantic, httpx/Ollama, Telegram Bot HTTP API, Windows venv, `magnit-cli`.

**Spec:** `docs/superpowers/specs/2026-11-17-qwen-magnit-cli-design.md`

## Global Constraints

- Do not use `uv` anywhere; invoke CLI as `[sys.executable, <MAGNIT_CLI_DIR>/magnit.py, ...]`.
- Run all project tests with `E:\local-ai-chat\magnit-app\.venv\Scripts\python.exe`.
- No live Magnit mutation in automated tests.
- Never call `cart-set` before a fresh explicit Telegram confirmation.
- Never pass secrets, raw personal CLI responses, or untrusted text into shell command strings.
- Preserve existing Telegram menu behavior and `CART_LINKS`.

## Review Focus

- Free text arriving while a menu step is active must keep the existing menu semantics.
- A model-selected ID absent from the supplied basket set must not reach the CLI.
- Confirmation must be bound to the exact proposal and rejected after cancel/replacement.
- A CLI timeout, nonzero exit, empty/non-JSON stdout, or post-write mismatch must be visible as failure without leaking payloads.
- Duplicate SKU entries from basket groups must be combined into a single positive quantity before `cart-set`.

---

### Task 1: Safe `magnit-cli` Adapter

**Files:**
- Create: `magnit_cli.py`
- Create: `test_magnit_cli.py`

**Interfaces:**
- Produces: `MagnitCli`, `MagnitCliError`, `MagnitCli.run(command: str, *args: str) -> object`, and typed helpers for `cart`, `cart_set`, `checkout_preview`.

- [ ] Write tests for fixed interpreter/cwd/list arguments, allowlist, missing entrypoint, timeout, nonzero exit, empty/non-JSON stdout, and secret-free errors.
- [ ] Run the focused tests and record the expected RED failures.
- [ ] Implement the minimal adapter using `asyncio.create_subprocess_exec` and `sys.executable`.
- [ ] Run focused and full `unittest` suites GREEN.
- [ ] Commit the task.

### Task 2: Qwen Basket Proposal and Skill

**Files:**
- Create: `cart_assistant.py`
- Create: `test_cart_assistant.py`
- Create: `.agents/skills/magnit-cart-assistant/SKILL.md`

**Interfaces:**
- Consumes: `CartSettings`, `Basket`, and `MagnitCli`.
- Produces: `BasketProposal`, `CartAssistant.propose(text, settings)`, and `CartAssistant.execute(basket_id, progress)`.

- [ ] Run a baseline agent scenario without the new skill and record unsafe/incorrect behavior.
- [ ] Write tests for skill loading, structured schema, valid/no-match/unknown basket IDs, prompt-injection-shaped basket text, deterministic SKU aggregation, CLI command order, and post-write verification.
- [ ] Run focused tests RED.
- [ ] Implement minimal proposal/execution code and concise skill.
- [ ] Validate the skill structure and run a forward scenario with the skill.
- [ ] Run focused and full suites GREEN.
- [ ] Commit the task.

### Task 3: Telegram Proposal and Confirmation Flow

**Files:**
- Modify: `telegram_bot.py`
- Modify: `telegram_menu.py`
- Modify: `telegram_setup.py`
- Modify: `test_telegram_bot.py`
- Modify: `test_telegram_setup.py`

**Interfaces:**
- Consumes: `CartAssistant.propose` and `CartAssistant.execute`.
- Produces: owner-only free-text proposal flow, proposal-bound callback data, cancel/confirm handling, and single-message status edits.

- [ ] Write tests for free-text proposal, no match, confirm, cancel, stale confirmation, owner isolation, active-menu preservation, no mutation before confirm, error status, and final `CART_LINKS`.
- [ ] Run focused tests RED.
- [ ] Implement the minimal state/callback/status integration.
- [ ] Run focused and full suites GREEN.
- [ ] Commit the task.

### Task 4: Local Windows Documentation and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `.gitignore` only if a new local-only path requires it.

**Interfaces:**
- Documents: venv invocation, `MAGNIT_CLI_DIR`, Ollama/model prerequisites, local startup, safety boundary, and reproducible test commands without `uv`.

- [ ] Add documentation assertions/checks where useful and update README without secrets or machine-specific session values.
- [ ] Run the complete bot suite with the project venv.
- [ ] If the CLI checkout is available, run `tests.py`, `integration_tests.py`, and `tests_workflows.py` with the same venv Python and no `uv`; otherwise report the exact availability blocker.
- [ ] Review the complete diff for security, correctness, regressions, and scope.
- [ ] Fix every Critical/Important review finding and repeat the full test commands.
- [ ] Commit the final documentation/verification task.

