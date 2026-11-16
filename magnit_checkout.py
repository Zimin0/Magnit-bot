from decimal import Decimal

from magnit_cart import CartError, CartSnapshot


CHECKOUT_PREVIEW = '/webgate/v1/checkout/preview'


def checkout_snapshot(snapshot: CartSnapshot, payload) -> CartSnapshot:
    if not isinstance(payload, dict):
        raise CartError('«Магнит» вернул неизвестный формат расчёта корзины.')
    summary = payload.get('summary')
    total = summary.get('totalFinalPrice') if isinstance(summary, dict) else None
    suborders, unavailable = payload.get('suborders'), payload.get('unavailableItems')
    if type(total) is not int or total < 0 or not isinstance(suborders, list) or not isinstance(unavailable, list):
        raise CartError('«Магнит» не подтвердил итоговую сумму и наличие товаров.')

    unavailable_ids = {item['goodId'] for item in unavailable
                       if isinstance(item, dict) and isinstance(item.get('goodId'), str)}
    confirmed = {}
    for order in suborders:
        if not isinstance(order, dict) or not isinstance(order.get('items'), list):
            raise CartError('«Магнит» вернул неизвестный формат списка товаров.')
        for item in order['items']:
            if not isinstance(item, dict):
                continue
            sku, quantity, maximum = item.get('goodId'), item.get('qnty'), item.get('maxQnty')
            if not isinstance(sku, str) or item.get('isAvailable') is not True or type(quantity) is not int:
                continue
            if type(maximum) is int:
                quantity = min(quantity, maximum)
            confirmed[sku] = confirmed.get(sku, 0) + max(quantity, 0)

    quantities = {}
    missing = set()
    for sku, previous in snapshot.quantities.items():
        if previous is None:
            quantities[sku] = None
            continue
        available = 0 if sku in unavailable_ids else min(previous, confirmed.get(sku, 0))
        quantities[sku] = available
        if available < previous:
            missing.add(sku)
    amount = f'{Decimal(total) / 100:.2f}'.replace('.', ',') + ' ₽'
    return CartSnapshot(quantities, amount, frozenset(missing))
