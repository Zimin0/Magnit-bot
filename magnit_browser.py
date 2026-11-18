import asyncio
import os
import re
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from magnit_cart import AddUncertain, Candidate, CART_URL, CartError, CartSnapshot, normalized
from magnit_checkout import CHECKOUT_PREVIEW, checkout_snapshot


ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / 'magnit-chrome-profile'
HOME = 'https://magnit.ru/'
ROWS = '.cart-product-list-item-wrap'
COUNTER = '.pl-stepper__counter'
PLUS = 'button[data-test-id="v-stepper-plus-button"]'
ADD = 'button[data-test-id="v-product-preview-basket-button"]'
CARD = 'a[href^="/product/"][title]'
PRICE = '.unit-catalog-product-preview-prices__regular'
MONEY = re.compile(r'\d[\d\s]*(?:[,.]\d{1,2})?\s*₽')
MINIMUM_ORDER = re.compile(r'Заказ\s+от\s+' + MONEY.pattern + r'\s*[—–-]\s*не\s+хватает\s+ещ[её]\s+' + MONEY.pattern, re.I)
EMPTY_CART = re.compile(r'корзина (?:пока )?пуст[ао]|в корзине (?:пока )?(?:пусто|ничего нет)|добавьте товары в корзину', re.I)
NO_RESULTS = re.compile(r'ничего не нашли|ничего не найдено|товары не найдены|найдено 0 товаров', re.I)


def product_id(url: str | None) -> str | None:
    parsed = urlsplit(url or '')
    if parsed.netloc and parsed.netloc != 'magnit.ru':
        return None
    match = re.fullmatch(r'/product/(\d+)(?:-[^/]+)?/?', parsed.path)
    return match[1] if match else None


def cart_quantity(text: str) -> int | None:
    match = re.fullmatch(r'\s*(\d+)\s*(?:шт\.?)?\s*', text)
    return int(match[1]) if match else None


def minimum_order_notice(text: str) -> str | None:
    match = MINIMUM_ORDER.search(text)
    return ' '.join(match[0].split()) if match else None


def address_matches(mode: str, address: str, configured: str) -> bool:
    if not re.match(r'^\s*Доставка\b', mode, re.I):
        return False
    actual = re.findall(r'[^\W_]+', address.casefold().replace('ё', 'е'))
    expected = re.findall(r'[^\W_]+', configured.casefold().replace('ё', 'е'))
    return bool(expected) and actual == expected


@asynccontextmanager
async def open_profile(*, headless: bool = False):
    try:
        from playwright.async_api import Error, async_playwright
    except ImportError:
        raise CartError('Установите Playwright по инструкции в README.md.') from None

    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                PROFILE, channel='chrome', headless=headless, viewport={'width': 1100, 'height': 780},
            )
            try:
                page = await context.new_page() if headless or not context.pages else context.pages[0]
                page.set_default_timeout(12000 if headless else 6500)
                yield page
            finally:
                try:
                    await context.close()
                except Error:
                    pass
    except Error:
        raise CartError('Браузер «Магнита» не ответил. Проверьте корзину на телефоне, доступность сайта и закройте окно настройки профиля перед повтором.') from None


class MagnitStore:
    def __init__(self, page, address: str):
        self.page = page
        self.address = address

    async def check_account(self) -> None:
        from playwright.async_api import TimeoutError

        auth = self.page.locator('.pl-auth-button:visible').first
        try:
            await auth.wait_for(state='visible')
        except TimeoutError:
            raise CartError('Не удалось проверить вход на сайте «Магнита». Откройте локальный профиль и проверьте вход.') from None
        auth_text = (await auth.inner_text()).strip()
        if normalized(auth_text) not in {'профиль', 'моякарта'}:
            raise CartError(
                f'В профиле Магнит-бота сайт показывает «{auth_text[:40]}», а не профиль аккаунта. '
                'Войдите в открывшемся окне Chrome именно для этого профиля.'
            )
        selector = self.page.locator('button.pl-shop-select-bar:visible').first
        await selector.wait_for(state='visible')
        title = selector.locator('.pl-shop-select-bar__info--title')
        address = selector.locator('.pl-shop-select-bar__info--address')
        if not await title.count() or not await address.count() or not address_matches(
            await title.inner_text(), await address.inner_text(), self.address
        ):
            raise CartError('Выберите «Доставка» и нажмите «Подтвердить». Город, улица и дом в шапке сайта должны совпадать с адресом в magnit-products.yaml.')

    async def snapshot(self) -> CartSnapshot:
        from playwright.async_api import Error

        try:
            return await self._snapshot()
        except Error:
            raise CartError('Не удалось прочитать корзину «Магнита». Проверьте её в приложении перед повтором.') from None

    async def clear_cart(self) -> None:
        from playwright.async_api import Error

        before = await self.snapshot()
        if not before.quantities:
            return
        try:
            await self.page.get_by_role('button', name='Очистить корзину', exact=True).click()
            dialog = self.page.get_by_role('dialog').filter(visible=True)
            if await dialog.count():
                await dialog.get_by_role('button', name=re.compile(r'^(?:Очистить|Удалить|Да[, ]+удалить)', re.I)).click()
            await self.page.get_by_text(EMPTY_CART).filter(visible=True).first.wait_for(state='visible', timeout=8000)
        except Error:
            # A click may have reached the site even if its response was interrupted.
            pass
        after = await self.snapshot()
        if after.quantities:
            raise CartError('«Магнит» не подтвердил очистку корзины. Наполнение остановлено.')

    async def final_snapshot(self, snapshot: CartSnapshot | None = None) -> CartSnapshot:
        from playwright.async_api import Error

        preview = asyncio.get_running_loop().create_future()

        def observe(response):
            if (not preview.done() and response.url == HOME.rstrip('/') + CHECKOUT_PREVIEW
                    and response.request.method == 'GET' and response.ok):
                preview.set_result(response)

        self.page.on('response', observe)
        try:
            if snapshot is not None and not snapshot.quantities:
                return CartSnapshot({}, '0,00 ₽')
            if snapshot is None:
                snapshot = await self.snapshot()
            else:
                await self.page.goto(CART_URL, wait_until='domcontentloaded')
                await self.check_account()
            if not snapshot.quantities:
                return CartSnapshot({}, '0,00 ₽')
            response = await asyncio.wait_for(preview, timeout=10)
            verified = checkout_snapshot(snapshot, await response.json())
            notice = self.page.get_by_text(MINIMUM_ORDER).filter(visible=True).first
            text = await notice.inner_text() if await notice.count() else await self.page.locator('body').inner_text()
            return replace(verified, total=await self.visible_total() or snapshot.total or verified.total,
                           minimum_order=minimum_order_notice(text))
        except (TimeoutError, Error, ValueError):
            raise CartError('Страница «Магнита» не вернула итоговый расчёт корзины.') from None
        finally:
            self.page.remove_listener('response', observe)

    async def _snapshot(self) -> CartSnapshot:
        from playwright.async_api import TimeoutError

        await self.page.goto(CART_URL, wait_until='domcontentloaded')
        await self.check_account()
        rows = self.page.locator(ROWS + ':visible')
        empty = self.page.get_by_text(EMPTY_CART).filter(visible=True).first
        try:
            await rows.first.or_(empty).first.wait_for(state='visible')
        except TimeoutError:
            raise CartError('Не удалось прочитать корзину «Магнита». Проверьте её в приложении перед повтором.') from None
        quantities = {}
        for row in await rows.all():
            link = row.locator('a[href^="/product/"]').first
            sku = product_id(await link.get_attribute('href')) if await link.count() else None
            if not sku or sku in quantities:
                raise CartError('Не удалось однозначно прочитать товары корзины. Проверьте корзину в приложении.')
            counter = row.locator(COUNTER).first
            quantities[sku] = cart_quantity(await counter.inner_text()) if await counter.count() else None
        return CartSnapshot(quantities, await self.visible_total())

    async def visible_total(self) -> str | None:
        sidebar = self.page.locator('.ecom-sidebar-total:visible').first
        if await sidebar.count():
            match = re.search(r'Итого\s*(' + MONEY.pattern + ')', await sidebar.inner_text(), re.I)
            if match:
                return ' '.join(match[1].split())
        return None

    async def open_search(self, query: str) -> None:
        from playwright.async_api import TimeoutError

        url = HOME + 'search?' + urlencode({'term': query})
        await self.page.goto(url, wait_until='domcontentloaded')
        await self.check_account()
        heading = self.page.get_by_role('heading', name=re.compile(r'найдено.*по запросу', re.I)).first
        empty = self.page.get_by_text(NO_RESULTS).filter(visible=True).first
        try:
            await heading.or_(empty).first.wait_for(state='visible')
            await self.page.locator(CARD + ':visible').first.or_(empty).first.wait_for(state='visible')
        except TimeoutError:
            raise CartError('Не удалось прочитать результаты поиска «Магнита». Проверьте сайт перед повтором.') from None

    async def search(self, query: str) -> list[Candidate]:
        await self.open_search(query)
        candidates = []
        for card in (await self.page.locator(CARD + ':visible').all())[:12]:
            sku = product_id(await card.get_attribute('href'))
            name = await card.get_attribute('title')
            price = card.locator(PRICE).first
            add = card.locator(ADD).first
            in_cart = await card.locator(PLUS).count() > 0
            can_add = await add.count() > 0 and await add.is_enabled()
            if sku and name and await price.count() and (in_cart or can_add):
                candidates.append(Candidate(sku, name, ' '.join((await price.inner_text()).split()), query))
        return candidates

    async def add_one(self, candidate: Candidate, previous: int) -> None:
        from playwright.async_api import Error, expect

        try:
            await self._add_one(candidate, previous, expect)
        except (Error, AssertionError):
            raise AddUncertain from None

    async def _add_one(self, candidate: Candidate, previous: int, expect) -> None:
        if previous:
            link = self.page.locator(f'a[href="/product/{candidate.id}"], a[href^="/product/{candidate.id}-"]')
            row = self.page.locator(ROWS).filter(has=link, visible=True)
            button = row.locator(PLUS)
            counter = row.locator(COUNTER)
        else:
            await self.open_search(candidate.search_query)
            card = self.page.locator(f'{CARD}[href="/product/{candidate.id}"], {CARD}[href^="/product/{candidate.id}-"]').filter(visible=True)
            button = card.locator(ADD)
            counter = card.locator(COUNTER)
        await button.click(timeout=4500)
        await expect(counter).to_have_text(re.compile(rf'^\s*{previous + 1}\s*(?:шт\.?)?\s*$'), timeout=4500)


def setup() -> None:
    for variable in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        directory = os.environ.get(variable)
        if directory:
            chrome = Path(directory) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'
            if chrome.is_file():
                break
    else:
        raise CartError('Не удалось найти установленный Google Chrome.')

    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f'Открываю отдельный профиль Магнит-бота: {PROFILE}')
    print('Проверьте, что в адресной строке открыт magnit.ru. Войдите, выберите доставку и нужный адрес.')
    print('Не закрывайте это окно PowerShell, пока не закончите вход и не закроете окно Chrome.')
    try:
        completed = subprocess.run([
            str(chrome), f'--user-data-dir={PROFILE}', '--profile-directory=Default',
            '--no-first-run', '--new-window', HOME,
        ], check=False)
    except (OSError, subprocess.CalledProcessError):
        raise CartError('Не удалось открыть Chrome для входа в «Магнит».') from None
    if completed.returncode != 0:
        raise CartError(f'Chrome завершился с кодом {completed.returncode}; проверьте, что профиль не занят.')


if __name__ == '__main__':
    try:
        setup()
    except CartError as exc:
        raise SystemExit(str(exc)) from None
