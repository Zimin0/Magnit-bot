# Qwen Magnit CLI Assistant Design

## Intent

Add a local Windows-only conversational path to the Telegram bot. A user can write a free-form request such as «закажи мне сосиски на завтрак»; Qwen selects one of the user's configured named baskets, the bot proposes it, and only after explicit confirmation replaces the Magnit cart through `magnit-cli` and returns the existing app/cart links.

## Goals

- Load a repository skill into Qwen's system context and require structured basket-selection output.
- Show selection and execution progress by editing one Telegram status message.
- Execute only `cart`, `cart-set`, and `checkout-preview` through `magnit-cli`.
- Require explicit confirmation immediately before `cart-set`.
- Re-read and compare the resulting cart before reporting success.
- Run locally on Windows through the existing project venv.

## Non-goals

- Server deployment, order creation, payment, cancellation, or address modification.
- Full-text product search through `magnit-cli` (the CLI does not provide it).
- Giving Qwen direct shell access or exposing session secrets/API payloads to the model.
- Replacing the existing menu/manual cart workflow.

## Constraints

- Never invoke `uv`; use `sys.executable` from the running project venv.
- Locate the private CLI repository with `MAGNIT_CLI_DIR`; its entrypoint must be `<dir>/magnit.py`.
- Keep all subprocess arguments as a list, fix `cwd`, apply a timeout, check exit code, and parse stdout as JSON.
- Treat basket names and contents as data, not instructions. Qwen may return only an ID supplied in the current request.
- `cart-set` replaces the whole cart. No mutation occurs without an explicit Telegram confirmation bound to the proposed basket.
- Do not log or send `MAGNIT_*` values, raw personal CLI responses, addresses, coordinates, tokens, or JWTs.
- Preserve the existing `CART_LINKS` result flow.

## Approaches

1. Let Qwen run the CLI directly. Rejected: unsafe, hard to validate, and exposes shell/session concerns to the model.
2. Keep the current direct API implementation and use Qwen only for intent. Rejected for this path because the requested integration specifically uses `magnit-cli`.
3. Selected: Qwen returns a validated basket ID; deterministic Python code expands the saved basket, invokes an allowlisted CLI adapter, verifies the cart, and renders Telegram status.

## Components

- `magnit_cli.py`: subprocess adapter and JSON/exit/timeout validation.
- `cart_assistant.py`: loads the skill, asks Qwen for a structured basket proposal, validates the ID, expands a deterministic basket into `SKU:QTY`, and orchestrates CLI verification.
- `.agents/skills/magnit-cart-assistant/SKILL.md`: compact decision and safety contract loaded into Qwen.
- `telegram_bot.py` / `telegram_setup.py`: free-text entry, proposal state, confirmation/cancel callbacks, and editable progress.
- Tests use fake Qwen/CLI/Telegram boundaries; no live Magnit mutation.

## Observable Flow

1. Owner sends free text.
2. Bot posts one status message and edits it while Qwen examines configured baskets.
3. If no match: status becomes a safe no-match response.
4. If matched: status names the basket and shows Confirm/Cancel buttons.
5. Confirm calls CLI `cart`, then `cart-set`, then `cart`, then `checkout-preview`.
6. The bot compares expected and actual SKU quantities. A mismatch is an error, not success.
7. The final edit contains the verified result and existing Magnit links.

## Verification

- TDD for every production interface.
- Existing 41-test suite remains green via `magnit-app\.venv\Scripts\python.exe -m unittest discover -v`.
- New tests cover match, no match, unknown model ID, cancel, stale confirmation, CLI missing/error/timeout/non-JSON, no pre-confirmation mutation, full-cart replacement, and post-write mismatch.
- Offline `magnit-cli` tests run with the same venv interpreter and no `uv` when the CLI checkout is available.
- Final whole-branch code review must have no open Critical or Important findings.

