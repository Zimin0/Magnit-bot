from asyncio import Event
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from random import shuffle
import re
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal, Self

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError


CART_URL = "https://magnit.ru/ecom/cart"
MAGNIT_APP_URL = "https://redirect.appmetrica.yandex.com/serve/1109053982455637484"
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PhotoRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=260)]
Sku = Annotated[str, StringConstraints(pattern=r'^[0-9]+$')]
Purpose = Literal["завтрак", "в школу"]
logger = logging.getLogger(__name__)


class CartError(Exception):
    pass


class CartStopped(Exception):
    pass


def check_cart_stop(stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise CartStopped


class AddUncertain(CartError):
    pass


class ItemUnavailable(CartError):
    pass


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: ShortText
    sku: Sku | None = None
    name: ShortText
    purpose: Purpose | None = None
    quantity: int = Field(default=1, ge=1, le=12)
    required: list[ShortText] = Field(default_factory=list, max_length=5)
    exclude: list[ShortText] = Field(default_factory=list, max_length=5)
    replacements: list[ShortText] = Field(default_factory=list, max_length=3)
    photo: PhotoRef | None = None

    def orderable_identity(self) -> tuple[str, str, str, int] | None:
        return (self.id, self.sku, self.name, self.quantity) if self.sku is not None else None


class BasketOption(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    product_id: ShortText
    quantity: int | None = Field(default=None, ge=1, le=12)


class BasketGroup(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: ShortText
    options: list[BasketOption] = Field(min_length=1, max_length=12)
    pick: int = Field(default=1, ge=1, le=12)
    strategy: Literal['ordered', 'random'] = 'ordered'

    @model_validator(mode='after')
    def validate_pick(self) -> Self:
        if len({item.product_id for item in self.options}) != len(self.options) or self.pick > len(self.options):
            raise PydanticCustomError('basket_config', 'В группе «{name}» проверьте число вариантов и уникальность продуктов.',
                                      {'name': self.name})
        return self


class Basket(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: ShortText
    name: ShortText
    category: ShortText
    groups: list[BasketGroup] = Field(min_length=1, max_length=12)


class CartSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    address: ShortText
    products: list[Product] = Field(max_length=100)
    backend: Literal['playwright', 'api'] = 'playwright'
    baskets: list[Basket] = Field(default_factory=list, max_length=24)
    categories: list[ShortText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        if len({name.casefold() for name in self.categories}) != len(self.categories):
            raise PydanticCustomError('basket_config', 'Названия категорий должны быть уникальными.')
        if len({basket.id for basket in self.baskets}) != len(self.baskets):
            raise PydanticCustomError('basket_config', 'ID именных корзин должны быть уникальными.')
        if len({(basket.category.casefold(), basket.name.casefold()) for basket in self.baskets}) != len(self.baskets):
            raise PydanticCustomError('basket_config', 'Названия корзин внутри категории должны быть уникальными.')
        ids = set()
        products_by_id = {}
        orderable_skus = set()
        for product in self.products:
            if product.id in ids:
                raise PydanticCustomError("product_reference", "ID «{id}» указан дважды.", {"id": product.id})
            ids.add(product.id)
            products_by_id[product.id] = product
            if product.sku is not None:
                if product.sku in orderable_skus:
                    raise PydanticCustomError('product_reference', 'Артикулы любимых продуктов должны быть уникальными.')
                orderable_skus.add(product.sku)
        for product in self.products:
            for replacement_id in product.replacements:
                if replacement_id == product.id:
                    raise PydanticCustomError("product_reference", "ID «{id}» ссылается на себя.", {"id": product.id})
                if replacement_id not in ids:
                    raise PydanticCustomError("product_reference", "ID «{id}» ссылается на отсутствующую запись «{target}».",
                                              {"id": product.id, "target": replacement_id})
        for basket in self.baskets:
            for group in basket.groups:
                for option in group.options:
                    if option.product_id not in ids:
                        raise PydanticCustomError(
                            'product_reference',
                            'Корзина «{basket}» ссылается на отсутствующий продукт «{target}».',
                            {'basket': basket.name, 'target': option.product_id},
                        )
                    if products_by_id[option.product_id].sku is None:
                        raise PydanticCustomError(
                            'product_reference',
                            'Корзина «{basket}» ссылается на продукт «{target}» без артикула.',
                            {'basket': basket.name, 'target': option.product_id},
                        )
        return self


def load_settings(path: Path) -> CartSettings:
    source = path
    if path.suffix in {'.yaml', '.yml'} and not path.exists() and path.with_suffix('.json').is_file():
        source = path.with_suffix('.json')
    try:
        content = source.read_text(encoding='utf-8-sig')
        if source.suffix == '.json':
            return CartSettings.model_validate_json(content)
        return CartSettings.model_validate(yaml.safe_load(content))
    except FileNotFoundError:
        raise CartError(f'Создайте {path.name} из magnit-products.example.yaml и заполните адрес.') from None
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        if error['type'] in {'product_reference', 'basket_config'}:
            raise CartError(f"Ошибка в {source.name}. {error['msg']}") from None
        raise CartError(f'Проверьте адрес и список продуктов в {source.name} по примеру.') from None
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CartError(f'Проверьте адрес и список продуктов в {source.name} по примеру.') from None


def save_settings(path: Path, settings: CartSettings) -> None:
    if path.suffix == '.json':
        text = settings.model_dump_json(exclude_defaults=True, indent=2) + '\n'
    else:
        text = yaml.safe_dump(settings.model_dump(exclude_defaults=True), allow_unicode=True, sort_keys=False)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    price: str
    search_query: str = ""
    stock: int | None = None


@dataclass(frozen=True)
class CartSnapshot:
    quantities: dict[str, int | None]
    total: str | None
    unavailable: frozenset[str] = frozenset()
    minimum_order: str | None = None


@dataclass(frozen=True)
class CartLine:
    purpose: str
    requested: str
    selected: str | None
    target: int
    confirmed: int
    replacement: bool
    issue: str | None = None


@dataclass(frozen=True)
class CartResult:
    lines: list[CartLine]
    total: str | None
    other_items: bool
    warning: str | None = None
    basket_name: str | None = None
    minimum_order: str | None = None

    def message(self) -> str:
        rows = [f'Корзина «{self.basket_name}»' if self.basket_name else 'Корзина «Магнита»']
        if self.warning:
            rows.append(self.warning)
        for line in self.lines:
            name = line.selected or line.requested
            if line.replacement:
                name = f"{line.requested} → {name}"
            icon = '✅' if line.confirmed == line.target else '❌'
            text = f"{icon} {line.purpose.capitalize()} — {name}, {line.confirmed} из {line.target} шт."
            if line.issue:
                text += f" {line.issue}"
            rows.append(text)
        if self.total:
            label = "Вся корзина, включая другие товары" if self.other_items else "Итого по корзине"
            rows.append(f"{label} — {self.total}")
        if self.minimum_order:
            rows.append(f'⚠️ {self.minimum_order}. Добавьте что-нибудь вручную в корзину «Магнита».')
        rows.append("Оформление и оплата в приложении «Магнит».")
        return "\n".join(rows)


def normalized(text: str) -> str:
    return "".join(c for c in text.casefold().replace("ё", "е") if c.isalnum())


def name_matches(name: str, required: list[str], exclude: list[str]) -> bool:
    compact_name = normalized(name)
    if any(not (key := normalized(term)) or key not in compact_name for term in required):
        return False
    words = ' ' + ' '.join(re.findall(r'[^\W_]+', name.casefold().replace('ё', 'е'))) + ' '
    for term in exclude:
        phrase = ' '.join(re.findall(r'[^\W_]+', term.casefold().replace('ё', 'е')))
        if phrase and f' {phrase} ' in words:
            return False
    return True


@dataclass(frozen=True)
class RequestedItem:
    name: str
    purpose: str
    quantity: int


@dataclass(frozen=True)
class CartProgressItem:
    name: str
    quantity: int
    status: Literal['pending', 'added', 'unavailable']


async def select_basket(store, basket: Basket, products: list[Product], snapshot: CartSnapshot,
                        stage=None, stop=None):
    products_by_id = {product.id: product for product in products}
    selections = []
    for group in basket.groups:
        options = list(group.options)
        if group.strategy == 'random':
            shuffle(options)
        selected = 0
        for option in options:
            product = products_by_id[option.product_id]
            if stage:
                await stage('searching', product.name)
            check_cart_stop(stop)
            candidates = await store.search(product.name)
            check_cart_stop(stop)
            candidate = next((item for item in candidates if item.id == product.sku
                              and snapshot.quantities.get(item.id, 0) is not None
                              and (item.stock is None or item.stock > 0)), None)
            if stage:
                await stage('searching', product.name, 'found' if candidate else 'unavailable')
            if candidate is None:
                continue
            requested_option = option if group.strategy == 'random' else group.options[selected]
            requested_product = products_by_id[requested_option.product_id]
            quantity = option.quantity if option.quantity is not None else product.quantity
            replacement = group.strategy == 'ordered' and option.product_id != requested_option.product_id
            selections.append((RequestedItem(requested_product.name, basket.category, quantity), candidate, replacement))
            selected += 1
            if selected == group.pick:
                break
        if selected < group.pick:
            selections.append((RequestedItem(group.name, basket.category, group.pick - selected), None, False))
    return selections


async def select_products(store, products, choose, purpose, snapshot, stage=None, stop=None):
    products_by_id = {product.id: product for product in products}
    selections = []
    for product in products:
        if purpose is not None and product.purpose != purpose:
            continue
        candidate = None
        replacement = False
        alternatives = [products_by_id[replacement_id] for replacement_id in product.replacements]
        for option in [product, *alternatives]:
            if stage:
                await stage('searching', option.name)
            check_cart_stop(stop)
            required = list(dict.fromkeys(product.required + option.required))
            exclude = list(dict.fromkeys(product.exclude + option.exclude))
            candidates = [item for item in await store.search(option.name)
                          if name_matches(item.name, required, exclude)
                          and snapshot.quantities.get(item.id, 0) is not None]
            if not candidates:
                if stage:
                    await stage('searching', option.name, 'unavailable')
                continue
            existing = [item for item in candidates if snapshot.quantities.get(item.id, 0)]
            query_product = product.model_copy(update={"name": option.name, "required": required,
                                                       "exclude": exclude, "replacements": []})
            groups = [existing, candidates] if existing and len(existing) < len(candidates) else [candidates]
            for eligible in groups:
                check_cart_stop(stop)
                selection = await choose(query_product, eligible)
                check_cart_stop(stop)
                candidate = next((item for item in eligible if item.id == selection), None)
                if candidate:
                    break
            if stage:
                await stage('searching', option.name, 'found' if candidate else 'unavailable')
            if candidate:
                replacement = option.id != product.id
                break
        selections.append((product, candidate, replacement))
    return selections


async def assemble_cart(store, products: list[Product], choose, *, purpose: Purpose | None = None,
                        basket: Basket | None = None, verify_cart=None, progress=None, stage=None,
                        stop: Event | None = None) -> CartResult:
    check_cart_stop(stop)
    snapshot = await store.snapshot()
    if stage:
        await stage('searching')
    if basket is None:
        selections = await select_products(store, products, choose, purpose, snapshot, stage, stop)
    else:
        selections = await select_basket(store, basket, products, snapshot, stage, stop)
    check_cart_stop(stop)
    targets: dict[str, int] = {}
    for product, candidate, _ in selections:
        if candidate:
            targets[candidate.id] = targets.get(candidate.id, 0) + product.quantity

    progress_items = []
    if stage:
        await stage('adding')
    if progress is not None:
        progress_items = [CartProgressItem(candidate.name if candidate else product.name, product.quantity,
                                           'pending' if candidate else 'unavailable')
                          for product, candidate, _ in selections]
        await progress(tuple(progress_items))

    warning = None
    unavailable = set()
    try:
        for _, candidate, _ in selections:
            if not candidate or candidate.id not in targets:
                continue
            target = targets.pop(candidate.id)
            check_cart_stop(stop)
            snapshot = await store.snapshot()
            for _ in range(target):
                previous = snapshot.quantities.get(candidate.id, 0)
                if previous is None or previous >= target:
                    break
                check_cart_stop(stop)
                try:
                    await store.add_one(candidate, previous)
                except ItemUnavailable:
                    unavailable.add(candidate.id)
                    break
                except AddUncertain:
                    pass
                snapshot = await store.snapshot()
                confirmed = snapshot.quantities.get(candidate.id, 0)
                if confirmed is None or confirmed <= previous:
                    break
            if progress is not None:
                available = snapshot.quantities.get(candidate.id) or 0
                for index, (product, selected, _) in enumerate(selections):
                    if selected and selected.id == candidate.id:
                        status = 'added' if available >= product.quantity else 'unavailable'
                        progress_items[index] = replace(progress_items[index], status=status)
                        available = max(0, available - product.quantity)
                await progress(tuple(progress_items))
    except CartError as exc:
        logger.warning('Сборка прервалась при добавлении товаров: %s', exc)
        warning = f"Сборка прервалась. {exc}"

    if stage:
        await stage('verifying')
    check_cart_stop(stop)
    try:
        snapshot = await (verify_cart or store.snapshot)()
        availability_verified = True
    except CartError as exc:
        logger.warning('Ошибка итоговой сверки корзины: %s', exc)
        detail = ('Итоговая проверка наличия и суммы не удалась.' if verify_cart
                  else 'Ниже последняя успешная сверка. Проверьте текущую корзину в приложении.')
        warning = f'{warning or ""} {detail} {exc}'.strip()
        availability_verified = verify_cart is None
    check_cart_stop(stop)
    unavailable.update(snapshot.unavailable)

    remaining = snapshot.quantities.copy()
    lines = []
    for product, candidate, replacement in selections:
        available = (remaining.get(candidate.id) or 0) if candidate else 0
        confirmed = min(product.quantity, available) if availability_verified else 0
        if candidate and available:
            remaining[candidate.id] = available - confirmed
        issue = None
        if not candidate:
            issue = "Подходящий товар в наличии не найден."
        elif not availability_verified:
            issue = 'Наличие не подтверждено.'
        elif confirmed < product.quantity and candidate.id in unavailable:
            issue = 'Нет в наличии.' if not confirmed else 'В наличии меньше нужного количества.'
        elif confirmed < product.quantity:
            issue = "Магазин не подтвердил всё количество."
        lines.append(CartLine(product.purpose, product.name, candidate.name if candidate else None,
                              product.quantity, confirmed, replacement, issue))
    total = snapshot.total if availability_verified else None
    return CartResult(lines, total, any(quantity != 0 for quantity in remaining.values()), warning,
                      basket.name if basket else None, snapshot.minimum_order if availability_verified else None)


async def notify_telegram(client: httpx.AsyncClient, token: str, chat_id: str, message: str, *, reply_markup=None) -> int | None:
    if not token or not chat_id.isascii() or not chat_id.isdecimal() or int(chat_id) <= 0:
        raise CartError("Задайте токен бота и числовой ID личного Telegram-чата.")
    try:
        payload = {"chat_id": chat_id, "text": message}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                     json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("ok") is True:
            result = data.get('result')
            return result.get('message_id') if isinstance(result, dict) else None
    except httpx.HTTPStatusError as exc:
        logger.warning('Telegram не отправил сообщение (sendMessage, HTTP %s).', exc.response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning('Telegram не отправил сообщение (sendMessage, %s).', type(exc).__name__)
    raise CartError("Telegram не подтвердил отправку. Проверьте подключение, настройки бота и нажмите «Повторить отправку».")
