from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from magnit_cart import CartSettings, Product, Purpose, load_settings, save_settings


def keyboard(*rows) -> dict:
    return {'keyboard': [list(row) for row in rows], 'resize_keyboard': True, 'is_persistent': True}


CART_BUTTON = '🛒 Собрать корзину'
PRODUCTS_BUTTON = '📋 Продукты'
ADD_BUTTON = '➕ Добавить продукт'
SETTINGS_BUTTON = '⚙️ Настройки'
RESTART_BUTTON = '🔄 Перезапустить бота'
QWEN_MODE_BUTTON = '🤖 Общение с Qwen'
EXIT_QWEN_MODE_BUTTON = '⬅️ Выйти из общения с Qwen'
BACKEND_CHOICES = {'🌐 Playwright': 'playwright', '🧪 API': 'api'}
BACK = ('⬅️ Назад',)
PURPOSES = ('🍳 Завтрак', '🎒 В школу')
STANDARD_CATEGORIES = {'завтрак': PURPOSES[0], 'в школу': PURPOSES[1]}
STANDARD_BASKETS = {'завтрак': 'Обычный завтрак', 'в школу': 'Обычная корзина в школу'}
QUANTITIES = ('1️⃣', '2️⃣', '3️⃣')
CLEAR = ('🧹 Очистить',)
CONFIRM_DELETE = '✅ Подтвердить удаление'
MENU = keyboard((CART_BUTTON, PRODUCTS_BUTTON), (QWEN_MODE_BUTTON,), (SETTINGS_BUTTON,), (RESTART_BUTTON,))


def main_menu(backend: str) -> dict:
    mode = 'API' if backend == 'api' else 'Playwright'
    return keyboard((CART_BUTTON, PRODUCTS_BUTTON), (QWEN_MODE_BUTTON,),
                    (f'{SETTINGS_BUTTON} · {mode}',), (RESTART_BUTTON,))


QWEN_MENU = keyboard((EXIT_QWEN_MODE_BUTTON,))


CARD_MENU = keyboard((BACK[0], '📷 Фото'), ('✏️ Название', '📦 Количество'),
                     ('🧺 Корзина', '🔎 Ключевые слова'), ('🔁 Замены', '🚫 Исключения'), ('🗑️ Удалить',))
BUTTON_ACTIONS = {
    PRODUCTS_BUTTON: 'Продукты', ADD_BUTTON: 'Добавить продукт', BACK[0]: 'Назад',
    PURPOSES[0]: 'Завтрак', PURPOSES[1]: 'В школу', CLEAR[0]: 'Очистить',
    CONFIRM_DELETE: 'Подтвердить удаление', '🗑️ Удалить': 'Удалить продукт',
    '✏️ Название': 'Название', '📦 Количество': 'Количество', '🧺 Корзина': 'Назначение',
    '🔎 Ключевые слова': 'Условия', '🚫 Исключения': 'Исключения', '🔁 Замены': 'Замены', '📷 Фото': 'Фото',
    **dict(zip(QUANTITIES, ('1', '2', '3'))),
}
EDIT_FIELDS = {
    'Количество': ('quantity', 'Введите количество упаковок от 1 до 12.'),
    'Название': ('name', 'Введите название продукта.'),
    'Назначение': ('purpose', 'Выберите назначение.'),
    'Замены': ('replacements', 'Введите ID замен через запятую в нужном порядке.'),
    'Фото': ('photo', 'Положите JPG или PNG в папку photos на ПК и введите имя файла.'),
    'Условия': ('required', 'Введите обязательные слова или фразы через запятую.'),
    'Исключения': ('exclude', 'Введите слова или фразы, которых не должно быть в названии, через запятую.'),
}


def photo_path(root: Path, reference: str) -> Path:
    folder = (root / 'photos').resolve()
    supplied = Path(reference.replace('\\', '/'))
    if supplied.anchor or '..' in supplied.parts or ':' in reference:
        raise ValueError('Укажите JPG или PNG из папки photos.')
    candidate = root / supplied if supplied.parts and supplied.parts[0].casefold() == 'photos' else folder / supplied
    candidate = candidate.resolve()
    if not candidate.is_relative_to(folder) or candidate.suffix.lower() not in {'.jpg', '.jpeg', '.png'}:
        raise ValueError('Укажите JPG или PNG из папки photos.')
    return candidate


@dataclass(frozen=True)
class CartChoice:
    name: str
    purpose: Purpose | None = None
    basket_id: str | None = None


@dataclass
class BotMenu:
    selected: str | None = None
    step: str | None = None
    draft: dict = field(default_factory=dict)
    buttons: dict[str, str] = field(default_factory=dict)
    in_list: bool = False
    add_from_list: bool = False
    pending_cart_notice: str | None = None
    cart_category: str | None = None
    cart_choices: dict[str, CartChoice] = field(default_factory=dict)
    qwen_mode: bool = False

    def is_idle(self) -> bool:
        return not (self.selected or self.step or self.in_list or self.add_from_list
                    or self.draft or self.buttons or self.cart_category or self.cart_choices)

    def reset(self) -> None:
        self.selected = self.step = None
        self.draft.clear()
        self.buttons.clear()
        self.in_list = self.add_from_list = False
        self.cart_category = None
        self.cart_choices.clear()
        self.qwen_mode = False

    def enable_qwen_mode(self) -> None:
        self.reset()
        self.qwen_mode = True

    async def choose_cart(self, settings: CartSettings, reply) -> None:
        self.reset()
        self.step = 'cart-category'
        labels = {product.purpose: STANDARD_CATEGORIES[product.purpose] for product in settings.products}
        for basket in settings.baskets:
            category = basket.category.casefold()
            labels.setdefault(category, STANDARD_CATEGORIES.get(category, f'🗂️ {basket.category}'))
        order = dict.fromkeys([*(name.casefold() for name in settings.categories), *labels])
        self.buttons = {labels[category]: category for category in order if category in labels}
        if not self.buttons:
            self.reset()
            await reply('Список корзин пуст. Добавьте продукт или корзину в настройках.', reply_markup=MENU)
            return
        category_buttons = list(self.buttons)
        rows = [tuple(category_buttons[i:i + 2]) for i in range(0, len(category_buttons), 2)]
        await reply('Выберите категорию.', reply_markup=keyboard(*rows, BACK))

    async def choose_named_cart(self, settings: CartSettings, category: str, reply) -> None:
        self.reset()
        self.step = 'cart-basket'
        self.cart_category = category
        choices = [CartChoice(basket.name, basket_id=basket.id)
                   for basket in settings.baskets if basket.category.casefold() == category]
        for product in settings.products:
            if product.purpose == category:
                choices.append(CartChoice(STANDARD_BASKETS[product.purpose], purpose=product.purpose))
                break
        self.cart_choices = {f'🧺 {choice.name}': choice for choice in choices}
        await reply('Выберите корзину.', reply_markup=keyboard(BACK, *((label,) for label in self.cart_choices)))

    async def show_products(self, settings: CartSettings, reply) -> None:
        self.reset()
        self.in_list = True
        self.buttons = {f'📦 {index}. {product.name[:52]}': product.id
                        for index, product in enumerate(settings.products, 1)}
        rows = [(label,) for label in self.buttons]
        text = 'Выберите продукт.' if rows else 'Список продуктов пуст.'
        await reply(text, reply_markup=keyboard(*rows, (ADD_BUTTON,), BACK))

    async def show_product(self, path: Path, product: Product, reply) -> None:
        self.selected = product.id
        self.step = None
        self.draft.clear()
        self.buttons.clear()
        self.in_list = self.add_from_list = False
        keywords = f'«{", ".join(product.required)}»' if product.required else '—'
        text = '\n'.join([
            f'🏷️ Название: «{product.name}»',
            f'📦 Количество, которое будет добавлено в один заказ: {product.quantity} шт.',
            f'🧺 Корзина: «{product.purpose}»',
            f'🔎 Ключевые слова поиска: {keywords}',
        ])
        if product.exclude:
            text += f'\n🚫 Слова-исключения: «{", ".join(product.exclude)}»'
        if product.photo:
            try:
                photo = photo_path(path.parent, product.photo)
                if photo.is_file() and 0 < photo.stat().st_size <= 10_000_000:
                    caption = text if len(text) <= 1024 else product.name
                    await reply(caption, photo=photo, reply_markup=CARD_MENU)
                    if caption != text:
                        await reply(text, reply_markup=CARD_MENU)
                    return
            except (OSError, ValueError):
                pass
            text += '\nПроверьте, что фото находится в папке photos и занимает до 10 МБ.'
        await reply(text, reply_markup=CARD_MENU)

    async def handle(self, path: Path, text: str, reply) -> None:
        text = BUTTON_ACTIONS.get(text, text)
        if text == 'Главное меню' or (text == 'Назад' and (self.in_list or self.step == 'backend')):
            self.reset()
            await reply('Выберите действие.', reply_markup=MENU)
            return
        settings = load_settings(path)
        if text in {SETTINGS_BUTTON, f'{SETTINGS_BUTTON} · API', f'{SETTINGS_BUTTON} · Playwright'} or self.step == 'backend':
            if text in BACKEND_CHOICES and self.step == 'backend':
                settings = CartSettings.model_validate({**settings.model_dump(), 'backend': BACKEND_CHOICES[text]})
                save_settings(path, settings)
                self.reset()
                await reply(f'Режим сборки — {text}.', reply_markup=main_menu(settings.backend))
                return
            self.reset()
            self.step = 'backend'
            current = '🧪 API' if settings.backend == 'api' else '🌐 Playwright'
            await reply(f'Режим сборки — {current}.',
                        reply_markup=keyboard(tuple(BACKEND_CHOICES), BACK))
            return
        if text in {'Продукты', 'Настройки', 'К списку'}:
            await self.show_products(settings, reply)
            return
        if text == 'Добавить продукт':
            from_list = self.in_list
            self.reset()
            self.add_from_list = from_list
            self.step = 'add-name'
            await self.prompt_add_step(reply)
            return
        products = {product.id: product for product in settings.products}
        product = products.get(self.buttons.get(text) or self.selected)
        if text == 'Назад':
            if self.step == 'add-quantity':
                self.step = 'add-purpose'
                await self.prompt_add_step(reply)
            elif self.step == 'add-purpose':
                self.step = 'add-name'
                await self.prompt_add_step(reply)
            elif self.step == 'add-name' and not self.add_from_list:
                self.reset()
                await reply('Выберите действие.', reply_markup=MENU)
            elif self.step and product is not None:
                await self.show_product(path, product, reply)
            else:
                await self.show_products(settings, reply)
            return
        if text == 'Отмена':
            if product is None:
                await self.show_products(settings, reply)
            else:
                await self.show_product(path, product, reply)
            return
        if text in self.buttons:
            if product is None:
                await self.show_products(settings, reply)
            else:
                await self.show_product(path, product, reply)
            return
        if self.step and self.step.startswith('add-'):
            await self.add_product(path, settings, text, reply)
            return
        if product is None:
            await self.show_products(settings, reply)
            return
        if self.step == 'delete':
            if text != 'Подтвердить удаление':
                await reply('Подтвердите удаление или нажмите «Назад».')
                return
            del products[product.id]
            updated = CartSettings.model_validate({**settings.model_dump(), 'products': list(products.values())})
            save_settings(path, updated)
            await self.show_products(updated, reply)
            return
        if self.step:
            updated = product.model_dump()
            updated[self.step] = self.field_value(path, self.step, text)
            await self.save_product(path, settings, Product.model_validate(updated), reply)
            return
        if text == 'Удалить продукт':
            self.step = 'delete'
            await reply(f'Удалить «{product.name}» из списка?', reply_markup=keyboard((CONFIRM_DELETE,), BACK))
        elif text in EDIT_FIELDS:
            self.step, prompt = EDIT_FIELDS[text]
            if self.step == 'purpose':
                await reply(prompt, reply_markup=keyboard(PURPOSES, BACK))
            elif self.step in {'photo', 'required', 'exclude', 'replacements'}:
                if self.step == 'replacements':
                    prompt += '\n' + '\n'.join(f'{item.id} — {item.name}' for item in settings.products if item.id != product.id)
                await reply(prompt, reply_markup=keyboard(CLEAR, BACK))
            else:
                await reply(prompt, reply_markup=keyboard(BACK))
        else:
            await self.show_product(path, product, reply)

    async def add_product(self, path: Path, settings: CartSettings, text: str, reply) -> None:
        if self.step == 'add-name':
            if not 1 <= len(text) <= 100:
                raise ValueError('Введите название длиной от 1 до 100 символов.')
            self.draft.setdefault('id', f'p-{uuid4().hex[:8]}')
            self.draft['name'] = text
            self.step = 'add-purpose'
            await self.prompt_add_step(reply)
        elif self.step == 'add-purpose':
            self.draft['purpose'] = self.field_value(path, 'purpose', text)
            self.step = 'add-quantity'
            await self.prompt_add_step(reply)
        else:
            product = Product.model_validate({**self.draft, 'quantity': self.field_value(path, 'quantity', text)})
            await self.save_product(path, settings, product, reply)

    async def prompt_add_step(self, reply) -> None:
        if self.step == 'add-name':
            await reply('Введите название нового продукта.', reply_markup=keyboard(BACK))
        elif self.step == 'add-purpose':
            await reply('Выберите корзину.', reply_markup=keyboard(PURPOSES, BACK))
        else:
            await reply('Сколько упаковок добавить в список?', reply_markup=keyboard(QUANTITIES, BACK))

    @staticmethod
    def field_value(path: Path, name: str, text: str):
        if name == 'quantity':
            if not text.isascii() or not text.isdecimal() or len(text) > 2 or not 1 <= int(text) <= 12:
                raise ValueError('Введите число от 1 до 12.')
            return int(text)
        if name == 'purpose':
            if text.casefold() not in {'завтрак', 'в школу'}:
                raise ValueError('Выберите «Завтрак» или «В школу».')
            return text.casefold()
        if name in {'required', 'exclude', 'replacements'}:
            return [] if text == 'Очистить' else [value.strip() for value in text.split(',')]
        if name == 'photo':
            return None if text == 'Очистить' else photo_path(path.parent, text).relative_to(path.parent.resolve()).as_posix()
        return text

    async def save_product(self, path: Path, settings: CartSettings, product: Product, reply) -> None:
        products = {item.id: item for item in settings.products}
        products[product.id] = product
        updated = CartSettings.model_validate({**settings.model_dump(), 'products': list(products.values())})
        save_settings(path, updated)
        await self.show_product(path, product, reply)

