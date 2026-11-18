# Qwen Orders from Favorite Products and Baskets

## Intent

Allow the Telegram Qwen mode to understand two kinds of requests, while never ordering anything outside the local configuration:

- individual favorite products, for example `закажи 2 йогурта из моих любимых`;
- a configured ready basket, for example `закажи корзину йогуртов`.

Both operations replace the complete Magnit cart. They never append to the current cart and always require a separate Telegram confirmation before `cart-set`.

## Goals

- Qwen may select only configured favorite product IDs or a configured basket ID.
- A product request may contain several configured products and quantities from 1 through 12.
- A quantity stated by the user overrides the product default; an omitted quantity uses `quantity` from YAML.
- A basket request keeps the existing ordered/random group expansion.
- After confirmation, Python resolves IDs to SKU quantities, calls `cart-set`, rereads the cart, and requires an exact match before checkout preview.
- The existing editor supports creating and editing orderable favorite products and already supports full basket/category/group/option editing.
- Telegram shows the proposed product list or basket before confirmation and preserves the single-message progress display.

## Non-goals

- Searching for arbitrary products not present in YAML.
- Letting Qwen invent SKU values, product IDs, basket IDs, or CLI commands.
- Appending to an existing Magnit cart.
- Automatically discovering a missing SKU from a product name.
- Placing or paying for an order.

## Configuration

Keep the existing `products` and `baskets` layout. Add `sku` to `Product`:

```yaml
products:
  - id: favorite-yogurt
    sku: "1234567890"
    name: Йогурт натуральный
    purpose: завтрак
    quantity: 1
    required: [йогурт, натуральный]
```

`sku` is numeric text, like basket option SKU. New products created in the editor require it. Existing files without SKU remain readable so the editor can migrate them; such legacy products are visibly marked as unavailable to Qwen and are excluded from its catalog until the user supplies a SKU. Saving or editing a legacy product requires a valid SKU. This is the only safe migration because an exact SKU cannot be inferred from the old search fields.

The existing basket representation remains unchanged. Basket options continue to contain fixed SKU values and optional display/search metadata.

## Qwen Contract

Replace the basket-only proposal with a strict discriminated proposal containing:

- `kind`: `products`, `basket`, or `none`;
- `items`: product selections containing only `product_id` and `quantity: int | null`, used only for `products`; `null` means the user did not state a quantity and Python must apply the configured default;
- `basket_id`: used only for `basket`;
- `message`: short Russian explanation.

The prompt receives only safe catalog metadata: favorite product ID, name, default quantity and matching words; plus basket ID, name, category and group/option display names. SKU values are never sent to Qwen. Pydantic validates exclusivity, bounds and unknown IDs. Duplicate product selections are rejected rather than silently merged.

Words such as `набор` and `корзина` should favor a configured basket when one matches. Wording about a count of a product should favor favorite products. A mixed request for both a basket and extra products is intentionally unsupported and returns `kind: none` with an explanation. Other ambiguous or unmatched requests also return `kind: none` and do not offer confirmation.

## Confirmation and Execution

The proposal message lists either the selected basket or each selected favorite product with its final quantity. Confirmation state is bound to the owner, Telegram message ID and random nonce as it is today.

At confirmation time the bot reloads YAML and verifies that every selected ID still exists and still has the same orderable identity. Product identity is the tuple of `id`, `sku`, `name`, and default `quantity`; basket identity is the complete `Basket.model_dump()`. Unrelated configuration edits do not invalidate the proposal. It then builds the full expected SKU map:

- product proposal: configured `Product.sku` with proposed/default quantities;
- basket proposal: existing basket group expansion.

Execution uses the existing sequence `cart -> cart-set -> cart -> checkout-preview`. `cart-set` receives the complete desired map, so the old cart is replaced. Exact verification includes missing, extra, and incorrectly quantified positions. Any configuration change, CLI error or mismatch stops the flow with a safe error in the same progress message.

The basket-specific `CartExecution` result becomes a general operation result carrying a display title, expected positions, total and unavailable SKU values, so both proposal kinds share one Telegram execution path.

## Editor

- Rename the visible `Обычные продукты` section to `Любимые продукты`.
- Add a required numeric `Артикул Магнита` field to the product dialog for new/edited products.
- Show SKU on each favorite-product card.
- Mark legacy entries without SKU as `Недоступен для Qwen — укажите артикул`.
- Preserve the existing basket editor for categories, baskets, groups, selection strategy and SKU options.
- Keep revision conflict protection, photo handling and YAML serialization unchanged.

## Compatibility and Failure Cases

- Existing basket behavior remains compatible.
- Existing products without SKU can still be read and shown, but cannot be selected by Qwen.
- Two orderable favorite products cannot share one SKU; this avoids ambiguous collapsing into a single CLI position.
- Legacy search fields `required`, `exclude`, and `replacements` remain available to the old Playwright/API path but do not influence Qwen's fixed-SKU execution.
- An empty orderable product catalog does not prevent basket requests.
- A product request with no matching configured favorite returns no match.
- A quantity outside 1..12, mixed basket/product output, unknown IDs, malformed JSON, duplicate IDs, or missing SKU is rejected before CLI execution.
- A configuration edit between proposal and confirmation invalidates the proposal if it changes any selected product/basket identity.

## Approaches Considered

1. **Add SKU to favorite products and keep baskets unchanged — selected.** Minimal migration, deterministic CLI execution, and preserves the current editor and manual flows.
2. Normalize baskets to reference one shared product catalog. Cleaner long-term data model, but a much larger migration and it removes useful basket-local variants.
3. Resolve old query-only products through Playwright/API before CLI execution. Avoids SKU migration but mixes backends and makes results nondeterministic; rejected.

## Verification

- Model/schema tests for products, baskets, no-match, unknown IDs, duplicate IDs, default quantities and explicit quantities.
- Execution tests proving full replacement and exact reread verification for both action kinds.
- Telegram tests proving confirmation text, nonce invalidation, one-message progress and safe failures.
- Editor model/browser tests for SKU validation, legacy warnings and editing both favorite products and baskets.
- Full project unit suite plus existing offline `magnit-cli` suites.
