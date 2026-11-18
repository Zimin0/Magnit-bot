# Magnit CLI Browser Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Автоматически получать актуальный контекст доставки из сохранённого Chrome-профиля и передавать его всем subprocess `magnit-cli` без записи секретов на диск.

**Architecture:** Общий extractor наблюдает реальный GET корзины в headless Chrome, проверяет аккаунт и адрес и возвращает типизированный allowlist `MAGNIT_*`. `telegram_setup` выполняет extractor до polling и создаёт единственный `MagnitCli` с этим контекстом; адаптер добавляет его к копии окружения каждого subprocess.

**Tech Stack:** Python 3.13, Playwright persistent context, asyncio subprocess, unittest, Pydantic.

**Spec:** `docs/superpowers/specs/2026-11-19-magnit-cli-browser-context-design.md`

## Global Constraints

- Контекст считывается только из реального GET `/webgate/v1/carts/lite` в `magnit-chrome-profile`.
- Браузер закрывается до начала Telegram polling.
- Значения контекста не записываются в YAML, логи, Telegram или Git.
- `MAGNIT_CLI_DIR` остаётся явной локальной настройкой.
- Неполный контекст останавливает запуск до любого `cart-set`.
- Используется проектный `.venv`; `uv run` запрещён.

## Review Focus

- Заголовки приходят в другом регистре: extractor нормализует имена и не теряет значения.
- В ответе корзины отсутствует обязательный заголовок: запуск останавливается до создания polling-задачи.
- Chrome-профиль занят или сессия истекла: пользователю возвращается безопасная инструкция без секретов.
- Родительское окружение содержит устаревший `MAGNIT_*`: свежий браузерный снимок имеет приоритет только для allowlist.
- Исключение subprocess не содержит переданный JWT или другие значения контекста.

---

### Task 1: Extract browser session context

**Files:**
- Modify: `magnit_api.py`
- Test: `test_magnit_api.py`

**Interfaces:**
- Produces: `async capture_cli_context(page, address: str) -> dict[str, str]`, возвращающий ключи окружения `MAGNIT_DEVICE_ID`, `MAGNIT_DEVICE_TAG`, `MAGNIT_PERMISSIONS_JWT`, `MAGNIT_STORE_CODE`, `MAGNIT_ADDRESS_ID`, `MAGNIT_SERVICE`, `MAGNIT_DELIVERY_TYPE`.

- [ ] **Step 1: Write failing extractor tests**

Добавить тесты, что функция наблюдает GET корзины, проверяет профиль и адрес, нормализует регистр заголовков, возвращает только allowlist и отклоняет отсутствие любого из семи обязательных значений: `X-Device-Id`, `X-Device-Tag`, `X-Permissions-Jwt`, `X-Store-Code`, `X-Address-Id`, `X-Service`, `X-Delivery-Type`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m unittest test_magnit_api -v`

Expected: FAIL, потому что `capture_cli_context` отсутствует.

- [ ] **Step 3: Implement the extractor**

Вынести наблюдение корзины и безопасную проверку из `MagnitApiStore.connect` в переиспользуемую функцию; `connect` продолжает использовать тот же путь без дублирования.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest test_magnit_api -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `Extract Magnit CLI context from browser session`

### Task 2: Propagate an immutable context to every CLI subprocess

**Files:**
- Modify: `magnit_cli.py`
- Test: `test_magnit_cli.py`

**Interfaces:**
- Consumes: словарь окружения от `capture_cli_context`.
- Produces: `MagnitCli(cli_dir=None, *, context: Mapping[str, str], timeout=60.0)`; каждый `run()` передаёт subprocess копию `os.environ` с allowlisted context.

- [ ] **Step 1: Write failing subprocess environment tests**

Добавить тесты, что `cart`, `cart-set` и `checkout-preview` получают одинаковый контекст, свежий context заменяет одноимённые значения родителя, неизвестные ключи отвергаются, а ошибки не содержат секретов.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m unittest test_magnit_cli -v`

Expected: FAIL на новом параметре `context` или отсутствии `env` у subprocess.

- [ ] **Step 3: Implement immutable allowlisted environment propagation**

Скопировать и провалидировать context в конструкторе; в `create_subprocess_exec` передавать `env={**os.environ, **context}` без логирования значений.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest test_magnit_cli -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit: `Pass browser context to Magnit CLI commands`

### Task 3: Capture context before Telegram polling

**Files:**
- Modify: `telegram_setup.py`
- Modify: `README.md`
- Test: `test_telegram_setup.py`

**Interfaces:**
- Consumes: `capture_cli_context(page, settings.address)` and `MagnitCli(context=...)`.
- Produces: запуск polling только после успешного закрытия headless Chrome и создания CLI с полным контекстом.

- [ ] **Step 1: Write failing startup tests**

Добавить тест успешного порядка `open profile → capture → close profile → create CLI → poll` и тест отказа, где ошибка контекста не создаёт CLI и не запускает polling.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m unittest test_telegram_setup -v`

Expected: FAIL, потому что startup ещё не извлекает браузерный контекст.

- [ ] **Step 3: Wire startup and safe errors**

Загрузить settings, открыть `open_profile(headless=True)`, вызвать extractor, закрыть context manager, затем создать `MagnitCli(context=context)`. Обновить README: ручные `MAGNIT_*` больше не нужны, профиль должен быть авторизован и не занят другим Chrome.

- [ ] **Step 4: Run component and full verification**

Run: `.\.venv\Scripts\python.exe -m unittest test_telegram_setup -v`

Run: `.\.venv\Scripts\python.exe -m unittest discover -q`

Expected: все тесты PASS.

- [ ] **Step 5: Perform safe live verification**

Запустить только extraction и два `cli.cart()`; подтвердить одинаковый cart ID без вывода ID или заголовков. Не вызывать `cart-set` без нового подтверждения в Telegram.

- [ ] **Step 6: Commit and push**

Commit: `Capture Magnit CLI context from Chrome profile`

