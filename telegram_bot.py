import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from secrets import token_hex
from traceback import extract_tb

import httpx
import yaml
from pydantic import ValidationError

from cart_assistant import CartAssistant
from magnit_cart import (CART_URL, MAGNIT_APP_URL, CartError, CartSettings, CartStopped, Product,
                         check_cart_stop, load_settings, notify_telegram, save_settings)
from telegram_binding import BindingError
from telegram_menu import (BACKEND_CHOICES, BUTTON_ACTIONS, CART_BUTTON, EXIT_QWEN_MODE_BUTTON, MENU,
                           QWEN_MENU, QWEN_MODE_BUTTON, RESTART_BUTTON, SETTINGS_BUTTON, BotMenu, main_menu)


HELP = (
    "Собрать корзину — /cart\n"
    "Открыть список продуктов — /settings\n"
    "Изменить количество — /qty oats 2\n"
    "Посмотреть поля записи — /product oats\n"
    "Добавить или заменить запись — /set и YAML записи\n"
    "Удалить запись — /remove oats\n"
    "Повторить отправку результата — /retry"
)
PRODUCT_EXAMPLE = '/set\nid: apples\nname: яблоки фасованные\npurpose: в школу\nquantity: 2'
CART_LINKS = {'inline_keyboard': [
    [{'text': '📱 Открыть приложение «Магнит»', 'url': MAGNIT_APP_URL}],
    [{'text': '🌐 Открыть корзину на сайте', 'url': CART_URL}],
]}
logger = logging.getLogger(__name__)


async def handle_message(message, chat_id: str, settings_path: Path, reply, build_cart, *,
                         menu: BotMenu | None = None, edit=None, start_cart=None, start_proposal=None,
                         proposal_when_busy=None) -> None:
    chat = message.get('chat', {})
    text = message.get('text')
    if (chat.get('type') != 'private' or str(chat.get('id')) != chat_id or not isinstance(text, str)
            or ('from' in message and str(message['from'].get('id')) != chat_id)):
        return

    text = text.strip()
    if menu is not None and text == QWEN_MODE_BUTTON:
        menu.enable_qwen_mode()
        await reply('Режим общения с Qwen включён. Напишите, какую корзину собрать.', reply_markup=QWEN_MENU)
        return
    if menu is not None and text == EXIT_QWEN_MODE_BUTTON:
        menu.reset()
        await reply('Режим общения с Qwen выключен. Выберите действие.', reply_markup=MENU)
        return
    if text in {'Собрать корзину', CART_BUTTON}:
        text = '/cart'
    parts = text.split(maxsplit=1)
    if not parts:
        return
    command = parts[0].split('@', 1)[0] if parts[0].startswith('/') else ''
    args = parts[1] if len(parts) == 2 else ''

    if command in {'/start', '/help'}:
        if menu is not None:
            menu.reset()
        await reply(HELP if command == '/help' else 'Выберите действие.', reply_markup=MENU)
        return
    if command == '/retry':
        if menu is None or menu.pending_cart_notice is None:
            await reply('Нет результата для повторной отправки.')
        else:
            await send_cart_notice(reply, menu.pending_cart_notice, menu)
        return
    if command == '/cart' and menu is not None:
        try:
            await menu.choose_cart(load_settings(settings_path), reply)
        except CartError as exc:
            await reply(str(exc))
        return

    choice = None
    if not command and menu is not None and menu.step in {'cart-category', 'cart-basket'}:
        back = BUTTON_ACTIONS.get(text, text).casefold() == 'назад'
        try:
            if menu.step == 'cart-category':
                if back:
                    menu.reset()
                    await reply('Выберите действие.', reply_markup=MENU)
                elif category := menu.buttons.get(text):
                    await menu.choose_named_cart(load_settings(settings_path), category, reply)
                else:
                    await menu.choose_cart(load_settings(settings_path), reply)
                return
            if back:
                await menu.choose_cart(load_settings(settings_path), reply)
                return
            choice = menu.cart_choices.get(text)
            if choice is None:
                await menu.choose_named_cart(load_settings(settings_path), menu.cart_category, reply)
                return
        except CartError as exc:
            await reply(str(exc))
            return
        menu.reset()

    if command == '/cart' or choice is not None:
        if start_cart is None:
            await run_cart(choice, reply, build_cart, menu, edit)
        else:
            await start_cart(choice)
        return
    known_menu_text = (text in BUTTON_ACTIONS or text in BACKEND_CHOICES
                      or text in {'Главное меню', 'К списку', 'Продукты', 'Добавить продукт',
                                  'Назад', 'Настройки', 'Отмена', SETTINGS_BUTTON,
                                  f'{SETTINGS_BUTTON} · API', f'{SETTINGS_BUTTON} · Playwright'})
    if (not command and menu is not None and not known_menu_text and start_proposal is not None
            and (menu.qwen_mode or (menu.is_idle() and proposal_when_busy is not None and proposal_when_busy()))):
        await start_proposal(text)
        return
    try:
        if menu is not None and (not command or command == '/settings'):
            await menu.handle(settings_path, 'Продукты' if command else text, reply)
            return
        if command not in {'/settings', '/qty', '/product', '/set', '/remove'}:
            if command:
                await reply(HELP, reply_markup=MENU)
            return
        settings = load_settings(settings_path)
        response = edit_products(settings_path, settings, command, args)
    except ValidationError as exc:
        error = exc.errors(include_input=False)[0]
        field = '.'.join(map(str, error['loc']))
        response = f'Проверьте настройки. {field + " — " if field else ""}{error["msg"]}'
    except (CartError, ValueError) as exc:
        response = str(exc)
    except yaml.YAMLError:
        response = 'Проверьте формат YAML. Отправьте /set без параметров, чтобы увидеть пример.'
    except OSError:
        response = f'Не удалось записать {settings_path.name}. Проверьте доступ к файлу.'
    await reply(response)


async def run_cart(choice, reply, build_cart, menu: BotMenu | None, edit, *, stop=None, stop_data=None) -> None:
    name = f' «{choice.name}»' if choice is not None else ''
    heading = f'Собираю корзину{name}. Пришлю результат сюда.'
    phases = {
        'connecting': 'Подключаюсь к «Магниту»',
        'clearing': 'Очищаю корзину',
        'planning': 'Смотрю, что надо заказать',
        'searching': 'Ищу товары в каталоге',
        'adding': 'Добавляю товары в корзину',
        'verifying': 'Проверяю наличие и итоговую стоимость',
    }
    current_phase = 'connecting'
    search_items = {}
    product_rows = []
    stop_markup = {'inline_keyboard': [[{'text': '🛑 Остановить сборку', 'callback_data': stop_data}]]}
    button = {'reply_markup': stop_markup} if stop_data is not None else {}

    def render(*, error=None):
        keys = list(phases)
        current = keys.index(current_phase)
        rows = [heading, '']
        for index, (key, label) in enumerate(phases.items()):
            icon = '✅' if index < current else ('❌' if error else '🔄') if index == current else '○'
            rows.append(f'{icon} {label}')
            if key == 'searching' and current_phase == key:
                rows.extend(search_items.values())
            if key == 'adding' and current >= keys.index('adding'):
                rows.extend(product_rows)
        if error:
            rows.extend(['', f'❌ {error}'])
        text = '\n'.join(rows)
        return text if len(text) <= 4000 else text[:3999] + '…'

    if edit is not None:
        message_id = await reply(render(), **button)
    else:
        message_id = None
        if choice is None:
            await reply(heading)
        else:
            await reply(heading, reply_markup=MENU)
    editable = edit is not None and type(message_id) is int
    last_text = render()

    async def update():
        nonlocal last_text
        text = render()
        if text != last_text:
            try:
                await edit(message_id, text, **button)
            except CartError:
                logger.warning('Telegram не обновил статус сборки.')
            else:
                last_text = text

    async def stage(phase, product=None, status='pending'):
        nonlocal current_phase
        current_phase = phase
        if product is not None:
            icon, suffix = {'pending': ('🔄', 'ищу'), 'found': ('✅', 'найден'),
                            'unavailable': ('❌', 'не найден в наличии')}[status]
            search_items[product] = f'{icon} {product} — {suffix}'
        await update()

    async def progress(items):
        nonlocal current_phase, product_rows
        current_phase = 'adding'
        icons = {'pending': '🔄', 'added': '✅', 'unavailable': '❌'}
        product_rows = [f'{icons[item.status]} {item.name} — {item.quantity} шт.' for item in items]
        await update()

    options = {'progress': progress, 'stage': stage} if editable else {}
    if stop is not None:
        options['stop'] = stop
    if choice is not None:
        if choice.basket_id is not None:
            options['basket_id'] = choice.basket_id
        else:
            options['purpose'] = choice.purpose
    try:
        result = await build_cart(**options)
        check_cart_stop(stop)
    except CartStopped:
        stopped = f'🛑 Сборка корзины{name} остановлена. Проверьте текущий состав корзины.'
        if editable:
            try:
                await edit(message_id, stopped, reply_markup=CART_LINKS)
            except CartError:
                await reply(stopped, reply_markup=CART_LINKS)
        else:
            await reply(stopped, reply_markup=CART_LINKS)
    except CartError as exc:
        logger.error('Сборка корзины%s остановилась на этапе «%s»: %s', name, phases[current_phase], exc)
        if editable:
            try:
                await edit(message_id, render(error=exc), reply_markup={'inline_keyboard': []})
            except CartError:
                await reply(str(exc))
        else:
            await reply(str(exc))
    else:
        if editable:
            try:
                await edit(message_id, result, reply_markup=CART_LINKS)
            except CartError:
                await send_cart_notice(reply, result, menu)
        else:
            await send_cart_notice(reply, result, menu)
    if edit is not None and menu is not None:
        await reply('Выберите действие.', reply_markup=MENU)


async def send_cart_notice(reply, result: str, menu: BotMenu | None) -> None:
    if menu is not None:
        menu.pending_cart_notice = result
    try:
        await reply(result, reply_markup=CART_LINKS)
    except CartError:
        await reply('Корзина собрана. Чтобы получить результат повторно, отправьте /retry.')
    else:
        if menu is not None:
            menu.pending_cart_notice = None


def edit_products(path: Path, settings: CartSettings, command: str, args: str) -> str:
    products = {product.id: product for product in settings.products}
    if command == '/settings':
        rows = [f'{product.id} — {product.name}, {product.quantity} шт. ({product.purpose})'
                for product in products.values()]
        return '\n'.join([*rows, '', HELP, '', PRODUCT_EXAMPLE])
    if command == '/set':
        if not args:
            return PRODUCT_EXAMPLE
        product = Product.model_validate(yaml.safe_load(args))
        products[product.id] = product
        response = f'Сохранил {product.id} — {product.name}, {product.quantity} шт.'
    elif command == '/qty':
        product_id, _, quantity = args.rpartition(' ')
        if not product_id or not quantity.isascii() or not quantity.isdecimal() or len(quantity) > 2:
            raise ValueError('Укажите ID и количество от 1 до 12. Например, /qty oats 2')
        product = products.get(product_id)
        if product is None:
            raise ValueError('Такого ID нет. Откройте /settings.')
        product = Product.model_validate({**product.model_dump(), 'quantity': int(quantity)})
        products[product_id] = product
        response = f'Сохранил {product.name}, {product.quantity} шт.'
    else:
        product = products.get(args)
        if product is None:
            raise ValueError('Такого ID нет. Откройте /settings.')
        if command == '/product':
            return '/set\n' + yaml.safe_dump(product.model_dump(exclude_defaults=True), allow_unicode=True, sort_keys=False)
        del products[args]
        response = f'Удалил {product.name} из списка.'

    updated = CartSettings.model_validate({**settings.model_dump(), 'products': list(products.values())})
    save_settings(path, updated)
    return response


async def poll_telegram(token: str, chat_id: str, offset: int, settings_path: Path, build_cart, *,
                        assistant: CartAssistant, save_offset) -> bool | None:
    menu = BotMenu()
    cart_task = None
    stop = None
    stop_data = None
    pending_proposal = None
    empty_buttons = {'inline_keyboard': []}

    def busy():
        return cart_task is not None and not cart_task.done()

    def busy_text():
        if stop is not None:
            return 'Сборка уже идёт. Остановить её можно кнопкой под сообщением о ходе сборки.'
        return 'Сборка уже идёт. Дождитесь завершения проверки корзины.'

    async with httpx.AsyncClient(timeout=40) as client, AsyncExitStack() as cleanup:
        async def reply(text: str, *, reply_markup=None, photo: Path | None = None):
            if reply_markup is MENU:
                try:
                    reply_markup = main_menu(load_settings(settings_path).backend)
                except CartError:
                    pass
            if photo is not None:
                try:
                    with photo.open('rb') as image:
                        response = await client.post(f'https://api.telegram.org/bot{token}/sendPhoto',
                            data={'chat_id': chat_id, 'caption': text, 'reply_markup': json.dumps(reply_markup)},
                            files={'photo': (photo.name, image)})
                    response.raise_for_status()
                    if response.json().get('ok') is True:
                        return
                except (OSError, httpx.HTTPError, ValueError):
                    pass
                text += '\nTelegram не смог показать фото.'
            return await notify_telegram(client, token, chat_id, text, reply_markup=reply_markup)

        async def edit(message_id: int, text: str, *, reply_markup=None) -> None:
            payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            try:
                response = await client.post(f'https://api.telegram.org/bot{token}/editMessageText', json=payload, timeout=10)
                response.raise_for_status()
                if response.json().get('ok') is True:
                    return
            except httpx.HTTPStatusError as exc:
                logger.warning('Telegram не обновил сообщение (editMessageText, HTTP %s).', exc.response.status_code)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning('Telegram не обновил сообщение (editMessageText, %s).', type(exc).__name__)
            raise CartError('Telegram не подтвердил обновление сообщения.')

        async def status(message_id, text, *, reply_markup=empty_buttons):
            try:
                await edit(message_id, text, reply_markup=reply_markup)
            except CartError:
                await reply(text, reply_markup=reply_markup)

        async def propose(text):
            nonlocal pending_proposal
            if busy():
                await reply(busy_text())
                return
            pending_proposal = None
            message_id = await reply('Подбираю подходящую корзину…')
            if type(message_id) is not int:
                raise CartError('Telegram не подтвердил сообщение с предложением корзины.')
            try:
                proposal = await assistant.propose(text, load_settings(settings_path))
                if proposal.basket_id is None:
                    await status(message_id, proposal.message)
                    return
                basket = next((basket for basket in load_settings(settings_path).baskets
                               if basket.id == proposal.basket_id), None)
                if basket is None:
                    raise CartError('Эта корзина больше не существует. Запросите новое предложение.')
                nonce = token_hex(16)
                markup = {'inline_keyboard': [[
                    {'text': 'Собрать корзину', 'callback_data': 'cart-confirm:' + nonce},
                    {'text': 'Отмена', 'callback_data': 'cart-cancel:' + nonce},
                ]]}
                # Only the exact, successfully edited message can authorize execution.
                await edit(message_id, f'{proposal.message}\nКорзина: «{basket.name}».', reply_markup=markup)
                pending_proposal = (nonce, basket.id, message_id)
            except CartError as exc:
                await status(message_id, str(exc))

        async def execute_proposal(basket, message_id):
            phases = {'reading': 'Читаю текущую корзину', 'replacing': 'Заменяю корзину',
                      'verifying': 'Проверяю состав', 'checkout': 'Проверяю наличие и сумму'}
            current_phase = 'reading'

            def render(error: str | None = None) -> str:
                phase_names = list(phases)
                current = phase_names.index(current_phase)
                rows = [f'Собираю корзину «{basket.name}».', '']
                for index, label in enumerate(phases.values()):
                    icon = '✅' if index < current else ('❌' if error else '🔄') if index == current else '○'
                    rows.append(f'{icon} {label}')
                if error:
                    rows.extend(['', f'❌ {error}'])
                return '\n'.join(rows)

            async def progress(phase):
                nonlocal current_phase
                current_phase = phase
                await status(message_id, render())

            try:
                result = await assistant.execute(basket, progress)
            except CartError as exc:
                await status(message_id, render(str(exc)))
            except Exception as exc:
                logger.error('Помощник завершил сборку с ошибкой (%s).', type(exc).__name__)
                await status(message_id, render('Не удалось подтвердить сборку. Проверьте корзину в приложении.'))
            else:
                rows = [f'Корзина «{result.basket_name}» собрана и проверена.']
                if result.total is not None:
                    rows.append(f'Итого: {result.total // 100},{result.total % 100:02d} ₽')
                unavailable = ', '.join(result.unavailable)
                if len(unavailable) > 2000:
                    unavailable = unavailable[:1999] + '…'
                rows.append(f'Недоступно SKU: {len(result.unavailable)}' + (f' — {unavailable}' if unavailable else ''))
                await status(message_id, '\n'.join(rows), reply_markup=CART_LINKS)

        async def answer_callback(callback, text):
            try:
                await client.post(f'https://api.telegram.org/bot{token}/answerCallbackQuery', json={
                    'callback_query_id': callback['id'], 'text': text,
                }, timeout=5)
            except httpx.RequestError:
                logger.warning('Telegram не подтвердил нажатие кнопки.')

        async def start_cart(choice):
            nonlocal cart_task, stop, stop_data
            if busy():
                await reply(busy_text())
                return
            stop = asyncio.Event()
            stop_data = 'cart-stop:' + token_hex(8)

            async def build_in_background():
                try:
                    await run_cart(choice, reply, build_cart, menu, edit, stop=stop, stop_data=stop_data)
                except Exception as exc:
                    frames = extract_tb(exc.__traceback__)
                    location = ' → '.join(f'{Path(frame.filename).name}:{frame.lineno}' for frame in frames)
                    logger.error('Фоновая сборка завершилась ошибкой (%s). Место: %s', type(exc).__name__, location)

            cart_task = asyncio.create_task(build_in_background())

        async def close_cart_task():
            if cart_task is not None:
                cart_task.cancel()
                await asyncio.gather(cart_task, return_exceptions=True)

        cleanup.push_async_callback(close_cart_task)

        while True:
            try:
                response = await client.post(f'https://api.telegram.org/bot{token}/getUpdates', json={
                    'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query'],
                })
                response.raise_for_status()
                updates = response.json()['result']
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 404, 409}:
                    logger.error('Telegram остановил приём команд, HTTP %s. Проверьте токен и другие запуски бота.', exc.response.status_code)
                    return
                logger.warning('Telegram не вернул команды, HTTP %s.', exc.response.status_code)
                await asyncio.sleep(5)
                continue
            except (httpx.RequestError, ValueError, KeyError) as exc:
                logger.warning('Не удалось прочитать команды Telegram (%s).', type(exc).__name__)
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = max(offset, update['update_id'] + 1)
                try:
                    save_offset(offset)
                except BindingError:
                    logger.error('Не удалось сохранить позицию Telegram. Приём команд остановлен.')
                    return
                try:
                    if callback := update.get('callback_query'):
                        message = callback.get('message', {})
                        chat = message.get('chat', {})
                        owner = (chat.get('type') == 'private' and str(chat.get('id')) == chat_id
                                 and str(callback.get('from', {}).get('id')) == chat_id)
                        data = callback.get('data', '')
                        action, _, nonce = data.partition(':') if isinstance(data, str) else ('', '', '')
                        if action in {'cart-confirm', 'cart-cancel'}:
                            valid = (owner and pending_proposal is not None
                                     and nonce == pending_proposal[0]
                                     and type(message.get('message_id')) is int
                                     and message['message_id'] == pending_proposal[2])
                            if not valid:
                                await answer_callback(callback, 'Это предложение устарело. Запросите новое.')
                            elif busy() and action == 'cart-confirm':
                                pending_proposal = None
                                await answer_callback(callback, busy_text())
                            else:
                                _, basket_id, message_id = pending_proposal
                                pending_proposal = None
                                await answer_callback(callback, 'Собираю корзину…' if action == 'cart-confirm' else 'Отменено.')
                                if action == 'cart-cancel':
                                    await status(message_id, 'Предложение отменено.')
                                    continue
                                try:
                                    basket = next((basket for basket in load_settings(settings_path).baskets
                                                   if basket.id == basket_id), None)
                                    if basket is None:
                                        raise CartError('Эта корзина больше не существует. Запросите новое предложение.')
                                except CartError as exc:
                                    await status(message_id, str(exc))
                                    continue
                                stop = stop_data = None
                                cart_task = asyncio.create_task(execute_proposal(basket, message_id))
                        elif owner:
                            active = busy() and stop is not None and data == stop_data
                            if active:
                                stop.set()
                            await answer_callback(callback, 'Останавливаю сборку…' if active else 'Эта сборка уже завершилась.')
                        else:
                            await answer_callback(callback, 'Это предложение устарело. Запросите новое.')
                        continue
                    message = update.get('message', {})
                    chat = message.get('chat', {})
                    if (message.get('text') == RESTART_BUTTON and chat.get('type') == 'private'
                            and str(chat.get('id')) == chat_id
                            and ('from' not in message or str(message['from'].get('id')) == chat_id)):
                        if busy():
                            await reply('Дождитесь завершения сборки, затем перезапустите бота.')
                            continue
                        await reply('🔄 Перезапускаю бота…', reply_markup=MENU)
                        return True
                    if (chat.get('type') == 'private' and str(chat.get('id')) == chat_id
                            and ('from' not in message or str(message['from'].get('id')) == chat_id)
                            and isinstance(message.get('text'), str)):
                        normalized = message['text'].strip()
                        command = normalized.split(maxsplit=1)[0].split('@', 1)[0] if normalized else ''
                        if command in {'/start', '/help'} or normalized in {QWEN_MODE_BUTTON, EXIT_QWEN_MODE_BUTTON}:
                            pending_proposal = None
                    await handle_message(message, chat_id, settings_path, reply, build_cart,
                                         menu=menu, edit=edit, start_cart=start_cart, start_proposal=propose,
                                         proposal_when_busy=busy)
                except CartError:
                    logger.warning('Telegram не подтвердил ответ на команду.')

