# Qwen Favorite Products and Baskets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Qwen replace the Magnit cart with either configured favorite products and quantities or one configured ready basket.

**Architecture:** Extend `Product` with an optional migration-safe SKU, replace the basket-only LLM result with a strict discriminated proposal, and resolve all IDs to an exact SKU map in Python only after Telegram confirmation. Reuse the existing CLI replacement/verification sequence and editor infrastructure.

**Tech Stack:** Python 3.13, Pydantic, unittest, vanilla JavaScript editor, YAML, local Ollama/Qwen, magnit-cli.

**Spec:** `docs/superpowers/specs/2026-11-18-qwen-products-and-baskets-design.md`

## Global Constraints

- Both products and basket actions replace the complete cart; neither appends.
- Qwen never receives SKU values and cannot select IDs outside YAML.
- `cart-set` runs only after the existing owner/message/nonce confirmation.
- Legacy products without SKU remain readable but unavailable to Qwen.
- Use the project `.venv`; never use `uv run`.

## Review Focus

- A mixed basket-plus-product request returns no actionable proposal.
- Duplicate product IDs, duplicate orderable SKU values, and quantities outside 1..12 fail before CLI.
- A selected product or basket changed after proposal cannot execute.
- Legacy products without SKU do not break startup/editor and never enter the Qwen catalog.
- Exact reread verification rejects missing, extra, or incorrectly quantified cart positions.

---

### Task 1: Migration-safe favorite-product SKU

**Files:**
- Modify: `magnit_cart.py`
- Modify: `magnit-products.example.yaml`
- Test: `test_magnit_cart.py`

**Interfaces:**
- Produces: `Product.sku: str | None`; `Product.orderable_identity() -> tuple`; validation preventing duplicate non-null SKU values.

- [ ] Add failing tests for numeric SKU, legacy missing SKU, duplicate orderable SKU, and identity fields.
- [ ] Run focused tests and confirm RED.
- [ ] Implement the minimal model and catalog validation.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit schema and example changes.

### Task 2: Discriminated Qwen proposal and exact execution

**Files:**
- Modify: `cart_assistant.py`
- Modify: `.agents/skills/magnit-cart-assistant/SKILL.md`
- Test: `test_cart_assistant.py`

**Interfaces:**
- Consumes: `Product.sku`, product identity and existing `Basket` models.
- Produces: `ProductSelection(product_id, quantity)`, `CartProposal(kind, items, basket_id, message)`, general `CartExecution(title, positions, total, unavailable)`, proposal resolution/execution for either action kind.

- [ ] Add failing tests for product request/default quantity, explicit quantity, basket selection, no-match, mixed/unknown/duplicate selections, hidden SKU values, and exact CLI replacement.
- [ ] Run focused tests and confirm RED.
- [ ] Implement strict proposal validation and safe catalog payload without SKU.
- [ ] Implement identity snapshots and shared SKU-map execution through `cart -> cart-set -> cart -> checkout-preview`.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit assistant contract and execution changes.

### Task 3: Telegram confirmation for both target kinds

**Files:**
- Modify: `telegram_bot.py`
- Test: `test_telegram_bot.py`

**Interfaces:**
- Consumes: `CartProposal`, proposal identities, general `CartExecution`.
- Produces: product/basket confirmation rendering and one shared confirmed execution path.

- [ ] Add failing tests for product proposal text, default/explicit quantities, owner-bound confirmation, config-change invalidation, basket compatibility, and one-message progress/errors.
- [ ] Run focused tests and confirm RED.
- [ ] Store the typed pending proposal with its identity snapshot and render the exact replacement before confirmation.
- [ ] Execute either target through the same background task and progress renderer.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit Telegram integration.

### Task 4: Favorite-product editor migration UI

**Files:**
- Modify: `config_ui/index.html`
- Modify: `config_ui/app.js`
- Modify: `test_config_editor.py`

**Interfaces:**
- Consumes: optional `Product.sku` and server-side validation.
- Produces: required numeric SKU for new/edited products, visible SKU cards, legacy warning, renamed tab.

- [ ] Add failing editor tests/fixture assertions for the label, numeric SKU validation, visible SKU and legacy warning.
- [ ] Run focused tests and confirm RED.
- [ ] Implement minimal UI changes without altering basket editing, photos, or revision checks.
- [ ] Run focused tests and confirm GREEN.
- [ ] Commit editor changes.

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: plan checkboxes as tasks complete.

**Interfaces:**
- Consumes: final YAML, editor and Telegram behavior.

- [ ] Document favorite-product SKU migration and example phrases for both action kinds.
- [ ] Run `python -m unittest discover -q` from `.venv` and all offline `magnit-cli` suites with direct Python.
- [ ] Commit documentation and plan completion.
- [ ] Produce a whole-branch review package and request one fresh-context review.
