import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
import asyncio

from magnit_api import MagnitApiStore
from magnit_cart import Candidate, CartError, CartSnapshot
from magnit_checkout import checkout_snapshot


def cart(*rows):
    return {'id': 'test-cart', 'formats': [], 'items': [
        {'goodId': sku, 'offerId': sku, 'qnty': count,
         'increment': {'unit': 'apiece', 'value': 1}} for sku, count in rows
    ]}


class MagnitApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_positions_can_be_cleared_but_not_edited(self):
        store = MagnitApiStore(None, {})
        duplicates = cart(('123', 1), ('123', 2))
        store._request = AsyncMock(side_effect=[duplicates, duplicates, cart(), cart()])
        with self.assertRaises(CartError):
            await store.snapshot()
        await store.clear_cart()
        self.assertEqual(store.last_cart.items, [])
        self.assertEqual([call.args[0] for call in store._request.await_args_list], ['GET', 'GET', 'DELETE', 'GET'])

    async def test_http_201_without_body_confirms_addition_by_reading_cart(self):
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            {'status': 200, 'payload': cart()}, {'status': 200, 'payload': cart()},
            {'status': 201, 'payload': None}, {'status': 200, 'payload': cart(('123', 1))},
        ]))
        store = MagnitApiStore(page, {})
        await store.snapshot()
        await store.add_one(Candidate('123', 'Yogurt', '69'), 0)
        self.assertEqual(store.last_cart.item_quantities(), {('123', '123'): 1})
        self.assertEqual([call.args[1]['method'] for call in page.evaluate.await_args_list],
                         ['GET', 'GET', 'PUT', 'GET'])

    async def test_connect_retries_temporary_cart_http_error_before_using_context(self):
        headers = {'x-service': 'express', 'x-store-code': 'fixture'}
        response = SimpleNamespace(ok=True, status=200, request=SimpleNamespace(headers=headers),
                                   json=AsyncMock(return_value=cart()))
        observations = []
        for result in (SimpleNamespace(ok=False, status=503), response):
            observed = asyncio.get_running_loop().create_future()
            observed.set_result(result)
            context = AsyncMock()
            context.__aenter__.return_value = SimpleNamespace(value=observed)
            observations.append(context)
        page = SimpleNamespace(expect_response=MagicMock(side_effect=observations), goto=AsyncMock())
        with patch('magnit_api.MagnitStore', return_value=SimpleNamespace(check_account=AsyncMock())), \
                patch.object(MagnitApiStore, 'load_catalog_context', new_callable=AsyncMock), \
                patch.object(MagnitApiStore, '_request', new_callable=AsyncMock, return_value=cart()) as request:
            store = await MagnitApiStore.connect(page, 'Test address')
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(store.last_cart.id, 'test-cart')
        request.assert_awaited_once_with('GET', '/webgate/v1/carts/lite')

    async def test_search_uses_native_catalog_store_instead_of_cart_header(self):
        response = SimpleNamespace(ok=True, request=SimpleNamespace(post_data_json={
            'storeCode': 'delivery-store', 'storeType': 'express', 'catalogType': '2',
        }))
        observed = asyncio.get_running_loop().create_future()
        observed.set_result(response)
        context = AsyncMock()
        context.__aenter__.return_value = SimpleNamespace(value=observed)
        page = SimpleNamespace(expect_response=MagicMock(return_value=context), goto=AsyncMock())
        browser = SimpleNamespace(check_account=AsyncMock())
        store = MagnitApiStore(page, {'x-store-code': 'different-cart-store'}, browser)
        await store.load_catalog_context()
        store._request = AsyncMock(return_value={'items': []})
        await store.search('Вологодское Лето молоко топленое')
        body = store._request.await_args.args[2]
        self.assertEqual(body['storeCode'], 'delivery-store')
        self.assertEqual(body['storeType'], 'express')
        self.assertEqual(body['catalogType'], '2')
        self.assertEqual(store.headers['x-store-code'], 'different-cart-store')

    async def test_clear_cart_checks_server_is_empty_before_returning(self):
        store = MagnitApiStore(None, {})
        store._request = AsyncMock(side_effect=[cart(('123', 2)), cart(), cart()])
        await store.clear_cart()
        self.assertEqual([(call.args[0], call.args[1]) for call in store._request.await_args_list], [
            ('GET', '/webgate/v1/carts/lite'), ('DELETE', '/webgate/v1/carts/lite'),
            ('GET', '/webgate/v1/carts/lite'),
        ])
        self.assertEqual(store._request.await_args_list[1].args, ('DELETE', '/webgate/v1/carts/lite'))
        self.assertEqual(store.last_cart.items, [])

    async def test_clear_cart_fails_if_server_still_has_items(self):
        store = MagnitApiStore(None, {})
        store._request = AsyncMock(side_effect=[cart(('123', 2)), cart(), cart(('123', 2))])
        with self.assertRaises(CartError):
            await store.clear_cart()

    def test_checkout_confirms_stock_and_final_price(self):
        result = checkout_snapshot(CartSnapshot({'123': 2, '456': 1, '789': 1}, None), {
            'summary': {'totalFinalPrice': 23950},
            'suborders': [{'items': [
                {'goodId': '123', 'offerId': '123', 'qnty': 2, 'maxQnty': 1, 'isAvailable': True},
                {'goodId': '456', 'offerId': '456', 'qnty': 1, 'maxQnty': 4, 'isAvailable': True},
            ]}],
            'unavailableItems': [{'goodId': '789'}],
        })
        self.assertEqual(result.quantities, {'123': 1, '456': 1, '789': 0})
        self.assertEqual(result.total, '239,50 ₽')
        self.assertEqual(result.unavailable, frozenset({'123', '789'}))

    async def test_search_skips_zero_stock_and_rechecks_before_write(self):
        store = MagnitApiStore(None, {'x-store-code': 'fixture'})
        store.catalog_context = {'storeCode': 'fixture', 'storeType': 'express', 'catalogType': '2'}
        item = {'id': '123', 'productId': '123', 'name': 'Yogurt', 'price': 9500, 'targetCart': 'express',
                'weighted': {'isWeighted': False}, 'orderProperties': {'minOrderQuantity': 1, 'orderStepQuantity': 1}}
        store._request = AsyncMock(side_effect=[{'items': [{**item, 'quantity': 0}, {**item, 'quantity': 2}]},
                                                {'items': [{**item, 'quantity': 0}]}])
        candidates = await store.search('Yogurt')
        self.assertEqual(len(candidates), 1)
        with self.assertRaises(CartError):
            await store.add_one(candidates[0], 0)
        self.assertEqual([call.args[0] for call in store._request.await_args_list], ['POST', 'POST'])

    async def test_api_appends_new_item_without_rewriting_other_offer_or_weight(self):
        store = MagnitApiStore(None, {})
        before = cart(('456', 500))
        before['items'][0].update(offerId='another-offer', increment={'unit': 'gram', 'value': 100})
        after = {**before, 'items': [*before['items'], *cart(('123', 1))['items']]}
        store._request = AsyncMock(side_effect=[before, before, after])
        snapshot = await store.snapshot()
        self.assertIsNone(snapshot.quantities['456'])
        await store.add_one(Candidate('123', 'Oats', '95'), 0)
        store._request.assert_awaited_with('PUT', '/webgate/v1/carts/lite', {'items': [
            {'goodId': '456', 'offerId': 'another-offer', 'qnty': 500},
            {'goodId': '123', 'offerId': '123', 'qnty': 1},
        ]})

    async def test_api_updates_full_list_and_preserves_other_rows(self):
        store = MagnitApiStore(None, {})
        before = cart(('123', 1), ('456', 3))
        after = cart(('123', 2), ('456', 3))
        store._request = AsyncMock(side_effect=[before, before, after])
        await store.snapshot()
        await store.add_one(Candidate('123', 'Oats', '95'), 1)
        store._request.assert_awaited_with('PUT', '/webgate/v1/carts/lite', {'items': [
            {'goodId': '123', 'offerId': '123', 'qnty': 2},
            {'goodId': '456', 'offerId': '456', 'qnty': 3},
        ]})

    async def test_api_does_not_write_if_cart_changed_since_snapshot(self):
        store = MagnitApiStore(None, {})
        store._request = AsyncMock(side_effect=[cart(('123', 1)), cart(('123', 1), ('456', 3))])
        await store.snapshot()
        with self.assertRaises(CartError):
            await store.add_one(Candidate('123', 'Oats', '95'), 1)
        self.assertEqual([call.args[0] for call in store._request.await_args_list], ['GET', 'GET'])

    async def test_api_adds_to_empty_cart_when_only_timestamp_changes(self):
        store = MagnitApiStore(None, {})
        store._request = AsyncMock(side_effect=[{**cart(), 'updatedAt': 1000},
                                                {**cart(), 'updatedAt': 1001}, cart(('123', 1))])
        await store.snapshot()
        await store.add_one(Candidate('123', 'Sausages', '215'), 0)
        store._request.assert_awaited_with('PUT', '/webgate/v1/carts/lite', {
            'items': [{'goodId': '123', 'offerId': '123', 'qnty': 1}],
        })
