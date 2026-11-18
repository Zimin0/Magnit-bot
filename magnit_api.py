import asyncio
import logging
from decimal import Decimal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from magnit_browser import MagnitStore
from magnit_cart import CART_URL, Candidate, CartError, CartSnapshot, ItemUnavailable


ORIGIN = 'https://magnit.ru'
CART_PATH = '/webgate/v1/carts/lite'
SEARCH_PATH = '/webgate/v2/goods/search'
CONTEXT_HEADERS = {'x-app-version', 'x-client-name', 'x-device-platform', 'x-device-id', 'x-device-tag',
                   'x-magnit-bff', 'x-permissions-jwt', 'x-platform-version', 'x-service', 'x-store-code',
                   'x-address-id', 'x-delivery-type'}
CLI_CONTEXT_HEADERS = {
    'x-device-id': 'MAGNIT_DEVICE_ID',
    'x-device-tag': 'MAGNIT_DEVICE_TAG',
    'x-permissions-jwt': 'MAGNIT_PERMISSIONS_JWT',
    'x-store-code': 'MAGNIT_STORE_CODE',
    'x-address-id': 'MAGNIT_ADDRESS_ID',
    'x-service': 'MAGNIT_SERVICE',
    'x-delivery-type': 'MAGNIT_DELIVERY_TYPE',
}
logger = logging.getLogger(__name__)


class Increment(BaseModel):
    model_config = ConfigDict(strict=True)
    unit: str
    value: int


class CartItem(BaseModel):
    model_config = ConfigDict(strict=True)
    goodId: str = Field(min_length=1)
    offerId: str = Field(min_length=1)
    qnty: int = Field(ge=1)
    increment: Increment | None = None

    def is_package(self) -> bool:
        return self.increment is not None and self.increment.unit == 'apiece' and self.increment.value == 1


class LiteCart(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1)
    formats: list
    items: list[CartItem]
    updatedAt: int | None = None

    def item_quantities(self) -> dict[tuple[str, str], int]:
        return {(item.goodId, item.offerId): item.qnty for item in self.items}


def read_lite_cart(payload, *, for_clear: bool = False) -> LiteCart:
    try:
        cart = LiteCart.model_validate(payload)
    except ValidationError:
        raise CartError('API «Магнита» вернул корзину неизвестного формата.') from None
    if not for_clear and (cart.formats or len({item.goodId for item in cart.items}) != len(cart.items)):
        raise CartError('В корзине есть позиции, для которых API-режим не может безопасно изменить количество.')
    return cart


async def _observe_cart_context(page, address: str) -> tuple[LiteCart, dict[str, str], MagnitStore]:
    from playwright.async_api import Error, TimeoutError

    browser_store = MagnitStore(page, address)
    failure = ''
    for attempt in range(1, 3):
        response_timeout = 12000
        if attempt > 1:
            logger.warning('Повторяю подключение к API «Магнита»: %s', failure)
            await asyncio.sleep(0.5)
            response_timeout = 20000
        step = 'загрузка страницы корзины'
        try:
            async with page.expect_response(
                lambda response: response.url == ORIGIN + CART_PATH
                and response.request.method == 'GET',
                timeout=response_timeout,
            ) as observed:
                await page.goto(CART_URL, wait_until='domcontentloaded')
                step = 'проверка входа и доставки'
                await browser_store.check_account()
                step = 'ожидание ответа корзины'
            response = await observed.value
            if not response.ok:
                failure = f'«Магнит» ответил HTTP {response.status} при чтении корзины.'
                if response.status == 408 or 500 <= response.status <= 599:
                    continue
                raise CartError(failure)
            step = 'чтение ответа корзины'
            original = read_lite_cart(await response.json(), for_clear=True)
            request_headers = {name.lower(): value for name, value in response.request.headers.items()}
            headers = {name: request_headers[name] for name in CONTEXT_HEADERS if name in request_headers}
            return original, headers, browser_store
        except TimeoutError:
            failure = f'Истекло время ожидания ({step}).'
        except Error:
            failure = f'Браузер прервал операцию ({step}).'
        except ValueError:
            failure = 'Ответ корзины не удалось прочитать как JSON.'
    raise CartError(f'Не удалось подключиться к корзине «Магнита» за две попытки. {failure}') from None


async def capture_cli_context(page, address: str) -> dict[str, str]:
    """Capture only the live request values required by magnit-cli."""
    _, headers, _ = await _observe_cart_context(page, address)
    if any(not headers.get(header) for header in CLI_CONTEXT_HEADERS):
        raise CartError('Не удалось получить полный контекст текущей корзины для Magnit CLI.')
    return {environment_name: headers[header]
            for header, environment_name in CLI_CONTEXT_HEADERS.items()}


class MagnitApiStore:
    def __init__(self, page, headers: dict[str, str], browser_store: MagnitStore | None = None):
        self.page = page
        self.headers = headers
        self.browser_store = browser_store
        self.last_cart: LiteCart | None = None
        self.catalog_context: dict[str, str] | None = None

    @classmethod
    async def connect(cls, page, address: str):
        original, headers, browser_store = await _observe_cart_context(page, address)
        if headers.get('x-service') != 'express' or not headers.get('x-store-code'):
            raise CartError('API-режим пока поддерживает экспресс-доставку с выбранным магазином.')
        store = cls(page, headers, browser_store)
        await store.load_catalog_context()
        direct = await store.read_cart(for_clear=True)
        if direct.id != original.id or direct.formats != original.formats or direct.items != original.items:
            raise CartError('API вернул другую корзину. Откройте настройки и выберите Playwright.')
        store.last_cart = direct
        return store

    async def load_catalog_context(self) -> None:
        from playwright.async_api import Error

        # Cart headers can refer to another store. Use the delivery catalog
        # selected by the site's own search request, independently of cart headers.
        try:
            async with self.page.expect_response(
                lambda response: response.url == ORIGIN + SEARCH_PATH
                and response.request.method == 'POST', timeout=12000,
            ) as observed:
                await self.page.goto(ORIGIN + '/search?' + urlencode({'term': 'молоко'}),
                                     wait_until='domcontentloaded')
                await self.browser_store.check_account()
            response = await observed.value
            body = response.request.post_data_json
            if (not response.ok or not isinstance(body, dict)
                    or not isinstance(body.get('storeCode'), str) or not body['storeCode']
                    or body.get('storeType') != 'express' or body.get('catalogType') != '2'):
                raise CartError('Не удалось подтвердить магазин доставки для поиска API.')
            self.catalog_context = {key: body[key] for key in ('storeCode', 'storeType', 'catalogType')}
        except (Error, ValueError) as exc:
            logger.warning('Не удалось прочитать контекст каталога (%s).', type(exc).__name__)
            raise CartError('Не удалось получить актуальный магазин доставки для поиска API.') from None

    async def _request(self, method: str, path: str, data=None):
        from playwright.async_api import Error

        try:
            result = await self.page.evaluate('''async ({origin, method, path, data, headers}) => {
                if (location.origin !== origin) return {status: 0};
                const response = await fetch(origin + path, {
                    method, headers: {...headers, ...(data ? {'content-type': 'application/json'} : {})},
                    body: data ? JSON.stringify(data) : undefined,
                    credentials: 'same-origin', redirect: 'error', cache: 'no-store',
                    signal: AbortSignal.timeout(10000),
                });
                const payload = response.status === 201 && method === 'PUT'
                    ? await response.json().catch(() => null)
                    : response.ok ? await response.json() : null;
                return {status: response.status, payload};
            }''', {'origin': ORIGIN, 'method': method, 'path': path, 'data': data, 'headers': self.headers})
        except (Error, ValueError) as exc:
            logger.warning('Не удалось выполнить запрос к «Магниту»: %s %s (%s).', method, path, type(exc).__name__)
            raise CartError('API «Магнита» не подтвердил запрос. Сверьте корзину перед повтором.') from None
        if result['status'] == 201 and method == 'PUT' and path == CART_PATH:
            logger.warning('API «Магнита»: PUT %s, HTTP 201, JSON получен=%s. Проверяю состав корзины.',
                           path, result['payload'] is not None)
        elif result['status'] != 200:
            logger.warning('API «Магнита»: %s %s, HTTP %s.', method, path, result['status'])
            raise CartError(f'API «Магнита» отклонил запрос (HTTP {result["status"]}). Можно выбрать Playwright в настройках.')
        return result['payload']

    async def read_cart(self, *, for_clear: bool = False) -> LiteCart:
        return read_lite_cart(await self._request('GET', CART_PATH), for_clear=for_clear)

    async def clear_cart(self) -> None:
        if self.browser_store is not None:
            await self.browser_store.check_account()
        before = await self.read_cart(for_clear=True)
        if before.items or before.formats:
            try:
                await self._request('DELETE', CART_PATH)
            except CartError:
                pass
            after = await self.read_cart(for_clear=True)
            if after.id != before.id or after.items or after.formats:
                raise CartError('«Магнит» не подтвердил очистку корзины. Наполнение остановлено.')
            self.last_cart = after
        else:
            self.last_cart = before

    async def snapshot(self) -> CartSnapshot:
        cart = await self.read_cart()
        if self.last_cart is not None and cart.id != self.last_cart.id:
            raise CartError('Идентификатор корзины изменился. Сборка остановлена.')
        self.last_cart = cart
        return CartSnapshot({item.goodId: item.qnty if item.is_package() else None for item in cart.items}, None)

    async def final_snapshot(self) -> CartSnapshot:
        snapshot = await self.snapshot()
        return await self.browser_store.final_snapshot(snapshot)

    async def search(self, query: str) -> list[Candidate]:
        if self.catalog_context is None:
            raise CartError('Магазин доставки для поиска API не определён.')
        payload = await self._request('POST', SEARCH_PATH, {
            'term': query, 'includeAdultGoods': False, 'pagination': {'offset': 0, 'limit': 36},
            'sort': {'order': 'desc', 'type': 'popularity'},
            **self.catalog_context,
        })
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise CartError('API «Магнита» вернул результаты поиска неизвестного формата.')
        candidates = []
        for item in payload['items']:
            if not isinstance(item, dict):
                continue
            sku, name, price = item.get('id'), item.get('name'), item.get('price')
            stock = item.get('quantity')
            order, weighted = item.get('orderProperties'), item.get('weighted')
            if (isinstance(sku, str) and sku.isascii() and sku.isdecimal() and sku == item.get('productId')
                    and isinstance(name, str) and type(price) is int and price >= 0
                    and type(stock) is int and stock > 0
                    and item.get('targetCart') == 'express' and not item.get('pickupOnly') and not item.get('isForAdults')
                    and isinstance(weighted, dict) and weighted.get('isWeighted') is False
                    and isinstance(order, dict)
                    and order.get('minOrderQuantity') == order.get('orderStepQuantity') == 1):
                candidates.append(Candidate(sku, name, f'{Decimal(price) / 100:.2f} ₽', query, stock))
        return candidates

    async def add_one(self, candidate: Candidate, previous: int) -> None:
        if self.browser_store is not None:
            await self.browser_store.check_account()
        if candidate.stock is not None:
            available = await self.search(candidate.search_query)
            if not any(item.id == candidate.id and item.stock > previous for item in available):
                raise ItemUnavailable('Количество в наличии изменилось.')
        current = await self.read_cart()
        last = self.last_cart
        items = {item.goodId: item for item in current.items}
        if last is None:
            raise CartError('Нет подтверждённого состояния корзины перед добавлением товара.')
        same_id = current.id == last.id
        same_items = items == {item.goodId: item for item in last.items}
        # Compare the contents that the next write would replace, not the server timestamp.
        if not same_id or not same_items:
            logger.warning('Корзина изменилась перед добавлением: идентификатор изменился=%s, состав изменился=%s.',
                           not same_id, not same_items)
            raise CartError('Корзина изменилась во время сборки. Проверьте её и запустите сборку снова.')
        item = items.get(candidate.id)
        if item is None and previous == 0:
            items[candidate.id] = CartItem(goodId=candidate.id, offerId=candidate.id, qnty=1)
        elif item is not None and item.is_package() and item.qnty == previous:
            items[candidate.id] = item.model_copy(update={'qnty': previous + 1})
        else:
            raise CartError('Не удалось подтвердить количество товара перед изменением корзины.')
        payload = await self._request('PUT', CART_PATH, {'items': [
            item.model_dump(include={'goodId', 'offerId', 'qnty'}) for item in items.values()
        ]})
        try:
            updated = read_lite_cart(payload)
        except CartError:
            logger.warning('Ответ на добавление SKU %s не содержит подтверждённый состав. Читаю корзину повторно.', candidate.id)
            updated = await self.read_cart()
        expected = {(item.goodId, item.offerId): item.qnty for item in items.values()}
        if updated.id != current.id or updated.item_quantities() != expected:
            observed = sum(item.qnty for item in updated.items if item.goodId == candidate.id)
            logger.warning('Добавление SKU %s не подтверждено: ожидалось %s шт., получено %s шт., '
                           'идентификатор корзины изменился=%s.', candidate.id, previous + 1, observed,
                           updated.id != current.id)
            raise CartError('API вернул другой состав корзины. Проверьте корзину в «Магните».')
        self.last_cart = updated
