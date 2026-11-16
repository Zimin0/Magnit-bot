import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from magnit_cart import CartError, CartProgressItem, check_cart_stop, load_settings
from telegram_bot import BotMenu, handle_message, poll_telegram
from telegram_menu import RESTART_BUTTON


class TelegramBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_private_owner_can_restart_and_offset_is_saved_first(self):
        offsets = []
        replies = []

        async def telegram(request):
            method = request.url.path.rsplit('/', 1)[-1]
            if method == 'getUpdates':
                self.assertEqual(json.loads(request.content)['offset'], 0)
                return httpx.Response(200, json={'result': [
                    {'update_id': update_id, 'message': {'chat': {'id': owner, 'type': 'private'},
                                                         'text': RESTART_BUTTON}}
                    for update_id, owner in ((10, 999), (11, 123))
                ]})
            replies.append(json.loads(request.content))
            self.assertEqual(offsets, [11, 12])
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
        with patch('telegram_bot.httpx.AsyncClient', return_value=client):
            restart = await poll_telegram('test-token', '123', 0, self.basket_config(), AsyncMock(),
                                          save_offset=offsets.append)
        self.assertIs(restart, True)
        self.assertEqual(len(replies), 1)
        self.assertIn([RESTART_BUTTON], replies[0]['reply_markup']['keyboard'])

    async def test_stop_button_is_received_while_cart_is_running(self):
        chat = {'id': 123, 'type': 'private'}
        started = asyncio.Event()
        stopped = asyncio.Event()
        requests = []
        button_data = None

        async def build(*, basket_id, progress, stage, stop):
            self.assertEqual(basket_id, 'yogurt')
            await stage('searching', 'Йогурт')
            started.set()
            await stop.wait()
            check_cart_stop(stop)

        async def telegram(request):
            nonlocal button_data
            method = request.url.path.rsplit('/', 1)[-1]
            data = json.loads(request.content)
            requests.append((method, data))
            if method == 'getUpdates':
                if data['offset'] == 0:
                    return httpx.Response(200, json={'result': [
                        {'update_id': i, 'message': {'chat': chat, 'text': text}}
                        for i, text in enumerate(('/cart', '🍳 Завтрак', '🧺 Завтрак-Йогурт'))
                    ]})
                if data['offset'] == 3:
                    await started.wait()
                    return httpx.Response(200, json={'result': [{'update_id': 3, 'callback_query': {
                        'id': 'stop-query', 'from': {'id': 123}, 'data': button_data,
                        'message': {'chat': chat, 'message_id': 71},
                    }}]})
                await stopped.wait()
                return httpx.Response(409)
            if method == 'sendMessage' and 'Собираю корзину' in data['text']:
                button_data = data['reply_markup']['inline_keyboard'][0][0]['callback_data']
            if method == 'editMessageText' and 'остановлена' in data['text']:
                self.assertEqual(data['message_id'], 71)
                self.assertTrue(all('url' in row[0] for row in data['reply_markup']['inline_keyboard']))
                stopped.set()
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
        with patch('telegram_bot.httpx.AsyncClient', return_value=client):
            await asyncio.wait_for(poll_telegram('test-token', '123', 0, self.basket_config(), build,
                                                save_offset=lambda offset: None), timeout=3)
        self.assertTrue(stopped.is_set())
        self.assertTrue(any(method == 'answerCallbackQuery' and data['callback_query_id'] == 'stop-query'
                            for method, data in requests))

    async def test_updates_one_progress_message_then_shows_result_links(self):
        path = self.basket_config()
        reply, edit, menu = AsyncMock(return_value=71), AsyncMock(), BotMenu()
        chat = {'id': 123, 'type': 'private'}

        async def build(*, basket_id, progress, stage):
            self.assertEqual(basket_id, 'yogurt')
            await stage('clearing')
            await stage('planning')
            await stage('searching', 'Йогурт')
            await stage('searching', 'Йогурт', 'found')
            await stage('adding')
            await progress([CartProgressItem('Йогурт', 1, 'pending')])
            await progress([CartProgressItem('Йогурт', 1, 'added')])
            return 'Итого по корзине — 254 ₽\nДобавьте что-нибудь вручную.'

        for text in ('/cart', '🍳 Завтрак', '🧺 Завтрак-Йогурт'):
            await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu, edit=edit)
        initial, pending, finished = edit.await_args_list[-3:]
        self.assertIn('Очищаю корзину', edit.await_args_list[0].args[1])
        self.assertIn('✅ Очищаю корзину', edit.await_args_list[1].args[1])
        self.assertIn('🔄 Йогурт', edit.await_args_list[2].args[1])
        self.assertIn('✅ Йогурт', edit.await_args_list[3].args[1])
        self.assertIn('🔄 Йогурт', initial.args[1])
        self.assertIn('✅ Йогурт', pending.args[1])
        self.assertTrue(all(call.args[0] == 71 for call in edit.await_args_list))
        self.assertIn('254 ₽', finished.args[1])
        self.assertEqual(len(finished.kwargs['reply_markup']['inline_keyboard']), 2)
        self.assertEqual(reply.await_args_list[-2].kwargs, {})
        self.assertIn('○ Очищаю корзину\n○ Смотрю, что надо заказать\n○ Ищу товары в каталоге',
                      reply.await_args_list[-2].args[0])
        self.assertIn('keyboard', reply.await_args.kwargs['reply_markup'])

    def basket_config(self) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'products.yaml'
        path.write_text('''address: Delivery address
products:
  - {id: school, name: Apple, purpose: в школу}
baskets:
  - id: yogurt
    name: Завтрак-Йогурт
    category: Завтрак
    groups:
      - name: Йогурт
        options: [{sku: '123', name: Yogurt}]
''', encoding='utf-8')
        return path

    async def test_backend_switch_persists_after_product_edit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'products.yaml'
            path.write_text('address: Delivery address\nproducts: []\n', encoding='utf-8')
            reply, build, menu = AsyncMock(), AsyncMock(), BotMenu()
            chat = {'id': 123, 'type': 'private'}
            for text in ('⚙️ Настройки', '🧪 API', '⬅️ Назад', '➕ Добавить продукт', 'Яблоки', '🎒 В школу', '2️⃣'):
                await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)
            settings = load_settings(path)
            self.assertEqual(settings.backend, 'api')
            self.assertEqual(settings.products[0].quantity, 2)
            for text in ('/start', '⚙️ Настройки · API', '🌐 Playwright'):
                await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)
            self.assertEqual(load_settings(path).backend, 'playwright')

    async def test_cart_button_requires_group_selection_and_supports_back(self):
        reply = AsyncMock()
        build = AsyncMock(return_value='Готовая корзина')
        menu = BotMenu()
        chat = {'id': 123, 'type': 'private'}
        path = self.basket_config()

        async def send(text):
            await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)

        await send('🛒 Собрать корзину')
        build.assert_not_awaited()
        self.assertEqual(set(reply.call_args.kwargs['reply_markup']['keyboard'][0]), {'🍳 Завтрак', '🎒 В школу'})
        await send('⬅️ Назад')
        build.assert_not_awaited()
        await send('🛒 Собрать корзину')
        await send('🍳 Завтрак')
        self.assertIn(['🧺 Завтрак-Йогурт'], reply.call_args.kwargs['reply_markup']['keyboard'])
        build.assert_not_awaited()
        await send('⬅️ Назад')
        await send('🎒 В школу')
        build.assert_not_awaited()
        await send('🧺 Обычная корзина в школу')
        build.assert_awaited_once_with(purpose='в школу')
        await send('/cart')
        await send('🍳 Завтрак')
        await send('🧺 Завтрак-Йогурт')
        build.assert_awaited_with(basket_id='yogurt')

    async def test_retry_sends_both_links_without_rebuilding_cart(self):
        reply = AsyncMock(side_effect=[None, None, None, CartError('Сбой'), None, None])
        build = AsyncMock(return_value='Готовая корзина')
        menu = BotMenu()
        chat = {'id': 123, 'type': 'private'}
        path = self.basket_config()
        for text in ('/cart', '🎒 В школу', '🧺 Обычная корзина в школу', '/retry'):
            await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)
        build.assert_awaited_once_with(purpose='в школу')
        self.assertIsNone(menu.pending_cart_notice)
        self.assertEqual(reply.call_args.args[0], 'Готовая корзина')
        buttons = reply.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual([row[0]['url'] for row in buttons], [
            'https://redirect.appmetrica.yandex.com/serve/1109053982455637484',
            'https://magnit.ru/ecom/cart',
        ])

    async def test_back_returns_to_previous_step_and_card_has_agreed_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'products.yaml'
            path.write_text('address: Delivery address\nproducts: []\n', encoding='utf-8')
            reply, build = AsyncMock(), AsyncMock()
            menu = BotMenu()
            chat = {'id': 123, 'type': 'private'}

            async def send(text):
                await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)
                return [label for row in reply.call_args.kwargs['reply_markup']['keyboard'] for label in row]

            self.assertNotIn('⬅️ Назад', await send('/start'))
            for text in ('➕ Добавить продукт', 'творог мягкий', '🍳 Завтрак'):
                self.assertIn('⬅️ Назад', await send(text))
            self.assertIn('🍳 Завтрак', await send('⬅️ Назад'))
            await send('🍳 Завтрак')
            await send('2️⃣')
            await send('🔎 Ключевые слова')
            await send('мягкий')
            self.assertEqual(reply.call_args.args[0],
                             '🏷️ Название: «творог мягкий»\n'
                             '📦 Количество, которое будет добавлено в один заказ: 2 шт.\n'
                             '🧺 Корзина: «завтрак»\n🔎 Ключевые слова поиска: «мягкий»')
            self.assertEqual(reply.call_args.kwargs['reply_markup']['keyboard'][0][0], '⬅️ Назад')
            await send('🚫 Исключения')
            await send('клубника, с сахаром')
            self.assertEqual(load_settings(path).products[0].exclude, ['клубника', 'с сахаром'])
            self.assertIn('🚫 Слова-исключения: «клубника, с сахаром»', reply.call_args.args[0])
            await send('📦 Количество')
            self.assertIn('🔎 Ключевые слова', await send('⬅️ Назад'))
            self.assertIn('⬅️ Назад', await send('⬅️ Назад'))
            self.assertNotIn('⬅️ Назад', await send('⬅️ Назад'))

    async def test_button_flow_adds_product_and_opens_yaml_photo(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'products.yaml'
            path.write_text('address: Delivery address\nproducts: []\n', encoding='utf-8')
            reply = AsyncMock()
            build = AsyncMock()
            menu = BotMenu()
            chat = {'id': 123, 'type': 'private'}

            async def send(text):
                await handle_message({'chat': chat, 'text': text}, '123', path, reply, build, menu=menu)

            for text in ('Добавить продукт', 'Яблоки', 'В школу', '2'):
                await send(text)
            product, = load_settings(path).products
            self.assertEqual((product.name, product.purpose, product.quantity), ('Яблоки', 'в школу', 2))

            photo = path.parent / 'photos' / 'apples.jpg'
            photo.parent.mkdir()
            photo.write_bytes(b'fixture photo')
            await send('Фото')
            await send('photos/apples.jpg')
            self.assertEqual(load_settings(path).products[0].photo, 'photos/apples.jpg')
            self.assertEqual(reply.call_args.kwargs['photo'], photo)

    async def test_only_paired_private_chat_can_build_or_change_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'products.json'
            reply = AsyncMock()
            build = AsyncMock()
            for chat in ({'id': 99, 'type': 'private'}, {'id': 123, 'type': 'group'}):
                for command in ('/cart', '/qty oats 5'):
                    await handle_message({'chat': chat, 'text': command}, '123', path, reply, build)
            build.assert_not_called()
            reply.assert_not_called()
            self.assertFalse(path.exists())

    async def test_owner_can_set_quantity_and_build(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'products.json'
            path.write_text(json.dumps({'address': 'Delivery address', 'products': [
                {'id': 'oats', 'name': 'Oats', 'purpose': 'завтрак', 'quantity': 1},
            ]}), encoding='utf-8')
            reply = AsyncMock()
            build = AsyncMock(return_value='Готовая корзина')
            chat = {'id': 123, 'type': 'private'}
            await handle_message({'chat': chat, 'text': '/qty oats 5'}, '123', path, reply, build)
            config = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(config['products'][0]['quantity'], 5)
            self.assertEqual(config['address'], 'Delivery address')
            await handle_message({'chat': chat, 'text': '/qty oats 99'}, '123', path, reply, build)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), config)
            await handle_message({'chat': chat, 'text': '/cart'}, '123', path, reply, build)
            build.assert_awaited_once()
