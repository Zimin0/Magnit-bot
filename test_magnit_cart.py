import asyncio
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from magnit_cart import AddUncertain, Basket, Candidate, CartError, CartSettings, CartSnapshot, CartStopped, Product, assemble_cart, notify_telegram
from magnit_browser import address_matches, cart_quantity, minimum_order_notice, product_id


class Store:
    def __init__(self):
        self.items = {"123": 1, "555": 3}
        self.clicks = 0
        self.uncertain = False

    async def search(self, query):
        if query == "Хлопья":
            return [Candidate("123", "Хлопья овсяные 400г", "95 ₽")]
        return []

    async def snapshot(self):
        return CartSnapshot(self.items.copy(), "475 ₽")

    async def add_one(self, candidate, previous):
        self.clicks += 1
        self.items[candidate.id] = previous + 1
        if self.uncertain:
            raise AddUncertain


async def choose(product, candidates):
    return candidates[0].id


class CartTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_between_units_keeps_first_unit_and_skips_next(self):
        stop = asyncio.Event()
        store = Store()
        store.items = {}
        add_one = store.add_one

        async def add_and_stop(candidate, previous):
            await add_one(candidate, previous)
            stop.set()

        store.add_one = add_and_stop
        product = Product(id='oats', name='Хлопья', purpose='завтрак', quantity=2)
        with self.assertRaises(CartStopped):
            await assemble_cart(store, [product], choose, stop=stop)
        self.assertEqual(store.items, {'123': 1})

    def test_minimum_order_keeps_store_amount_with_nonbreaking_spaces(self):
        self.assertEqual(minimum_order_notice('Итого 254 ₽\nЗаказ от 300\u00a0₽ — не хватает ещё 85\u202f₽\nК оформлению'),
                         'Заказ от 300 ₽ — не хватает ещё 85 ₽')
        self.assertIsNone(minimum_order_notice('Итого 254 ₽'))

    async def test_reports_progress_per_product_and_minimum_order_from_store(self):
        store = Store()
        store.items = {}
        states = []
        stages = []

        async def stage(*event):
            stages.append(event)

        async def progress(lines):
            states.append([line.status for line in lines])

        async def verify():
            self.assertEqual(stages[-1], ('verifying',))
            return CartSnapshot({'123': 1}, '254 ₽', minimum_order='Заказ от 300 ₽ — не хватает ещё 85 ₽')

        products = [Product(id='oats', name='Хлопья', purpose='завтрак'),
                    Product(id='missing', name='Нет в наличии', purpose='завтрак')]
        result = await assemble_cart(store, products, choose, verify_cart=verify, progress=progress, stage=stage)
        self.assertEqual(stages, [
            ('searching',), ('searching', 'Хлопья'), ('searching', 'Хлопья', 'found'),
            ('searching', 'Нет в наличии'), ('searching', 'Нет в наличии', 'unavailable'),
            ('adding',), ('verifying',),
        ])
        self.assertEqual(states, [['pending', 'unavailable'], ['added', 'unavailable']])
        self.assertIn('Заказ от 300 ₽ — не хватает ещё 85 ₽', result.message())
        self.assertIn('вручную', result.message())

    @patch('magnit_cart.shuffle', side_effect=lambda items: items.reverse())
    async def test_named_basket_uses_available_distinct_skus_and_ordered_replacement(self, shuffle):
        store = Store()
        store.items = {}
        async def search(query):
            return {
                'Нет в наличии': [],
                'Йогурт A': [Candidate('900', 'Другой йогурт', '10 ₽'), Candidate('111', 'Йогурт A', '11 ₽')],
                'Йогурт B': [Candidate('222', 'Йогурт B', '12 ₽')],
                'Йогурт C': [Candidate('555', 'Йогурт C', '15 ₽')],
                'Запасной': [Candidate('333', 'Запасной', '13 ₽')],
            }[query]
        store.search = search
        basket = Basket.model_validate({'id': 'test', 'name': 'Тест', 'category': 'Завтрак', 'groups': [
            {'name': 'Йогурты', 'pick': 2, 'strategy': 'random', 'options': [
                {'sku': '111', 'name': 'Йогурт A'}, {'sku': '222', 'name': 'Йогурт B'},
                {'sku': '555', 'name': 'Йогурт C'},
            ]},
            {'name': 'Замена', 'options': [{'sku': '444', 'name': 'Нет в наличии'}, {'sku': '333', 'name': 'Запасной'}]},
        ]})
        result = await assemble_cart(store, [], choose, basket=basket)
        self.assertEqual(store.items, {'555': 1, '222': 1, '333': 1})
        shuffle.assert_called_once()
        self.assertIn('Тест', result.message())
        self.assertTrue(result.lines[-1].replacement)

    async def test_report_uses_final_availability_and_total_instead_of_raw_cart(self):
        store = Store()
        product = Product(id='oats', name='Хлопья', purpose='завтрак')

        async def verify_cart():
            return CartSnapshot({'123': 0, '555': 3}, '239,50 ₽', frozenset({'123'}))

        result = await assemble_cart(store, [product], choose, verify_cart=verify_cart)
        self.assertEqual(result.lines[0].confirmed, 0)
        self.assertEqual(result.lines[0].issue, 'Нет в наличии.')
        self.assertIn('239,50 ₽', result.message())

    async def test_exclusions_filter_names_before_model_including_replacements(self):
        store = Store()

        async def search(query):
            if query == 'Мюсли':
                return []
            return [Candidate('555', 'Хлопья с МЁДОМ', '90 ₽'),
                    Candidate('123', 'Хлопья овсяные', '95 ₽')]

        async def choose_remaining(product, candidates):
            self.assertEqual([item.id for item in candidates], ['123'])
            return candidates[0].id

        store.search = search
        products = [
            Product(id='school', name='Мюсли', purpose='в школу', quantity=2,
                    replacements=['oats'], exclude=['медом']),
            Product(id='oats', name='Хлопья', purpose='завтрак'),
        ]
        result = await assemble_cart(store, products, choose_remaining, purpose='в школу')
        self.assertEqual(store.items, {'123': 2, '555': 3})
        self.assertTrue(result.lines[0].replacement)

    async def test_cart_group_uses_linked_replacement_without_adding_its_own_quantity(self):
        store = Store()
        products = [
            Product(id='breakfast', name='Хлопья', purpose='завтрак', quantity=3),
            Product(id='school', name='Мюсли', purpose='в школу', quantity=2, replacements=['breakfast']),
        ]
        result = await assemble_cart(store, products, choose, purpose='в школу')
        self.assertEqual(store.items, {'123': 2, '555': 3})
        self.assertEqual([(line.purpose, line.confirmed) for line in result.lines], [('в школу', 2)])
        self.assertTrue(result.lines[0].replacement)

    async def test_repeat_preserves_existing_items_and_quantities(self):
        store = Store()
        products = [Product(id="oats", name="Хлопья", purpose="завтрак", quantity=2)]
        first = await assemble_cart(store, products, choose)
        second = await assemble_cart(store, products, choose)
        self.assertEqual(store.items, {"123": 2, "555": 3})
        self.assertEqual(store.clicks, 1)
        self.assertEqual(first.lines[0].confirmed, 2)
        self.assertEqual(second.lines[0].confirmed, 2)
        self.assertTrue(first.other_items)

    async def test_uncertain_click_is_read_back_before_another_click(self):
        store = Store()
        store.uncertain = True
        result = await assemble_cart(store, [Product(id="oats", name="Хлопья", purpose="завтрак", quantity=2)], choose)
        self.assertEqual(result.lines[0].confirmed, 2)
        self.assertEqual(store.clicks, 1)

    async def test_last_confirmed_cart_is_retained_if_final_read_fails(self):
        store = Store()
        snapshot = store.snapshot
        reads = 0

        async def read_until_failure():
            nonlocal reads
            reads += 1
            if reads >= 4:
                raise CartError("Сбой чтения")
            return await snapshot()

        store.snapshot = read_until_failure
        product = Product(id="oats", name="Хлопья", purpose="завтрак", quantity=2)
        result = await assemble_cart(store, [product], choose)
        self.assertEqual(store.clicks, 1)
        self.assertEqual(result.lines[0].confirmed, 2)
        self.assertIn("последняя успешная сверка", result.message())

    async def test_unknown_candidate_and_empty_search_are_reported(self):
        async def choose_unknown(product, candidates):
            return "000"

        store = Store()
        products = [Product(id="oats", name="Хлопья", purpose="завтрак"), Product(id="bread", name="Хлеб", purpose="в школу")]
        result = await assemble_cart(store, products, choose_unknown)
        self.assertEqual(store.clicks, 0)
        self.assertEqual([line.confirmed for line in result.lines], [0, 0])
        self.assertTrue(all(line.issue for line in result.lines))

    async def test_replacement_is_reported_and_required_attributes_still_apply(self):
        store = Store()
        product = Product(id="muesli", name="Мюсли", purpose="в школу", quantity=2, replacements=["oats"])
        oats = Product(id="oats", name="Хлопья", purpose="завтрак", required=["400 г"])
        result = await assemble_cart(store, [product, oats], choose)
        self.assertEqual(len(result.lines), 2)
        self.assertTrue(result.lines[0].replacement)
        self.assertEqual(result.lines[0].selected, "Хлопья овсяные 400г")
        self.assertEqual(result.lines[0].confirmed, 2)
        self.assertEqual(result.lines[1].confirmed, 1)
        self.assertEqual(store.items["123"], 3)

        for entries in ([product.model_copy(update={"required": ["500 г"]}), oats],
                        [product, oats.model_copy(update={"required": ["500 г"]})]):
            with self.subTest(entries=entries):
                result = await assemble_cart(store, entries, choose)
                self.assertIsNone(result.lines[0].selected)
        self.assertEqual(store.clicks, 2)

    def test_invalid_replacement_ids_are_rejected_at_config_load(self):
        def entry(id, replacements=()):
            return {"id": id, "name": "Хлопья", "purpose": "завтрак", "replacements": list(replacements)}

        for products in ([entry("a"), entry("a")], [entry("a", ["missing"])], [entry("a", ["a"])]):
            with self.subTest(products=products), self.assertRaisesRegex(ValidationError, "ID"):
                CartSettings.model_validate({"address": "Москва", "products": products})

    async def test_unrelated_cart_item_does_not_hide_matching_candidate(self):
        store = Store()

        async def search(query):
            return [Candidate("555", "Хлопья кукурузные", "80 ₽"), Candidate("123", "Хлопья овсяные", "95 ₽")]

        async def choose_oats(product, candidates):
            self.assertEqual(product.replacements, [])
            return next((item.id for item in candidates if "овсяные" in item.name), None)

        store.search = search
        store.items = {"555": 3}
        product = Product(id="oats", name="Хлопья овсяные", purpose="завтрак")
        result = await assemble_cart(store, [product], choose_oats)
        self.assertEqual(result.lines[0].confirmed, 1)
        self.assertEqual(store.items, {"555": 3, "123": 1})

    def test_observed_product_urls_and_cart_units(self):
        self.assertEqual(product_id("/product/1000456968"), "1000456968")
        self.assertEqual(product_id("https://magnit.ru/product/12345-khleb"), "12345")
        self.assertIsNone(product_id("https://example.org/product/12345"))
        self.assertEqual(cart_quantity("3"), 3)
        self.assertEqual(cart_quantity("3\u00a0шт"), 3)
        self.assertIsNone(cart_quantity("0,5 кг"))

    def test_delivery_requires_exact_configured_address(self):
        self.assertTrue(address_matches("Доставка • Экспресс", "Москва, улица Ленина, 2", "Москва, улица Ленина, 2"))
        self.assertFalse(address_matches("Доставка • Экспресс", "проспект Тул. Ленина, 2", "ул. Ленина, 2"))
        self.assertFalse(address_matches("Самовывоз", "Москва, улица Ленина, 2", "Москва, улица Ленина, 2"))

    async def test_telegram_reports_failure_without_exposing_token(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"ok": False})
        )) as client:
            with self.assertRaises(CartError) as context:
                await notify_telegram(client, "secret-test-token", "123", "Готовая корзина")
            self.assertNotIn("secret-test-token", str(context.exception))
