import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx

from magnit_cart import CartError, CartProgressItem, check_cart_stop, load_settings
from cart_assistant import BasketProposal, CartExecution
from telegram_bot import CART_LINKS, BotMenu, handle_message, poll_telegram
from telegram_menu import QWEN_MODE_BUTTON, EXIT_QWEN_MODE_BUTTON, RESTART_BUTTON


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
                                          assistant=self.assistant(), save_offset=offsets.append)
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
                                                assistant=self.assistant(), save_offset=lambda offset: None), timeout=3)
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
  - {id: school, sku: '555', name: Apple, purpose: в школу}
  - {id: yogurt-product, sku: '123', name: Yogurt}
baskets:
  - id: yogurt
    name: Завтрак-Йогурт
    category: Завтрак
    groups:
      - name: Йогурт
        options: [{product_id: yogurt-product}]
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

    def assistant(self, basket_id='yogurt'):
        return SimpleNamespace(
            propose=AsyncMock(return_value=BasketProposal(
                kind='basket' if basket_id is not None else 'none', basket_id=basket_id,
                message='Подойдёт эта корзина.')),
            execute=AsyncMock(return_value=CartExecution('yogurt', 'Завтрак-Йогурт', {'123': 1}, 25409, ['456'])),
        )

    def message(self, text, *, owner=123, kind='private', sender=123):
        return {'message': {'chat': {'id': owner, 'type': kind}, 'from': {'id': sender}, 'text': text}}

    def proposal_callback(self, requests, *, action=0, data=None, message_id=71, sender=123, kind='private'):
        if data is None:
            markup = next(payload['reply_markup'] for method, payload in reversed(requests)
                          if method == 'editMessageText' and payload.get('reply_markup', {}).get('inline_keyboard'))
            data = markup['inline_keyboard'][0][action]['callback_data']
        return {'callback_query': {'id': f'query-{len(requests)}', 'from': {'id': sender}, 'data': data,
                                  'message': {'chat': {'id': 123, 'type': kind}, 'message_id': message_id}}}

    async def run_assistant_flow(self, assistant, batches, *, path=None, build=None, fail_edit=False, qwen_mode=True):
        requests = []
        if qwen_mode:
            batches = [[self.message(QWEN_MODE_BUTTON)], *batches]
        pending = iter(batches)
        counter = 0

        async def telegram(request):
            nonlocal counter
            method = request.url.path.rsplit('/', 1)[-1]
            payload = json.loads(request.content)
            if method == 'getUpdates':
                await asyncio.sleep(0)
                batch = next(pending, None)
                if batch is None:
                    return httpx.Response(409)
                if callable(batch):
                    batch = batch(requests)
                    if asyncio.iscoroutine(batch):
                        batch = await batch
                updates = []
                for update in batch:
                    updates.append({'update_id': counter, **update})
                    counter += 1
                return httpx.Response(200, json={'result': updates})
            requests.append((method, payload))
            if method == 'editMessageText' and fail_edit and 'собрана и проверена' in payload['text']:
                return httpx.Response(400)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
        with patch('telegram_bot.httpx.AsyncClient', return_value=client):
            await asyncio.wait_for(poll_telegram('test-token', '123', 0, path or self.basket_config(),
                                                build or AsyncMock(), assistant=assistant,
                                                save_offset=lambda offset: None), timeout=3)
        return requests

    async def test_free_text_proposes_one_editable_message_without_execution(self):
        assistant = self.assistant()
        requests = await self.run_assistant_flow(assistant, [[self.message('Собери завтрак')]])
        sends = [payload for method, payload in requests if method == 'sendMessage']
        edits = [payload for method, payload in requests if method == 'editMessageText']
        self.assertEqual(len(sends), 2)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['message_id'], 71)
        self.assertIn('Завтрак-Йогурт', edits[0]['text'])
        self.assertIn('Подойдёт эта корзина.', edits[0]['text'])
        buttons = edits[0]['reply_markup']['inline_keyboard'][0]
        self.assertEqual([button['text'] for button in buttons], ['Собрать корзину', 'Отмена'])
        confirm, cancel = [button['callback_data'] for button in buttons]
        self.assertNotIn('yogurt', confirm)
        self.assertEqual(confirm.split(':', 1)[1], cancel.split(':', 1)[1])
        self.assertGreaterEqual(len(confirm.split(':', 1)[1]), 32)
        self.assertLessEqual(len(confirm.encode()), 64)
        self.assertEqual(assistant.propose.await_args.args[0], 'Собери завтрак')
        self.assertEqual(assistant.propose.await_args.args[1].baskets[0].id, 'yogurt')
        assistant.execute.assert_not_awaited()

    async def test_free_text_requires_explicit_qwen_mode(self):
        assistant = self.assistant()
        await self.run_assistant_flow(assistant, [[self.message('Собери завтрак')]], qwen_mode=False)
        assistant.propose.assert_not_awaited()

    async def test_qwen_mode_button_routes_free_text_and_exit_disables_it(self):
        assistant = self.assistant()
        requests = await self.run_assistant_flow(assistant, [[self.message(QWEN_MODE_BUTTON)],
                                                              [self.message('Собери завтрак')]], qwen_mode=False)
        assistant.propose.assert_awaited_once()
        enabled = next(payload for method, payload in requests
                       if method == 'sendMessage' and 'Режим общения с Qwen включён' in payload['text'])
        labels = [label for row in enabled['reply_markup']['keyboard'] for label in row]
        self.assertIn(EXIT_QWEN_MODE_BUTTON, labels)

        assistant = self.assistant()
        await self.run_assistant_flow(assistant, [[self.message(QWEN_MODE_BUTTON)],
                                                  [self.message(EXIT_QWEN_MODE_BUTTON)],
                                                  [self.message('Собери завтрак')]], qwen_mode=False)
        assistant.propose.assert_not_awaited()

    async def test_no_match_edits_status_without_buttons(self):
        assistant = self.assistant(None)
        requests = await self.run_assistant_flow(assistant, [[self.message('Ужин')]])
        edit = [payload for method, payload in requests if method == 'editMessageText'][-1]
        self.assertEqual(edit['text'], 'Подойдёт эта корзина.')
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.assertEqual(sum(method == 'sendMessage' for method, _ in requests), 2)
        assistant.execute.assert_not_awaited()

    async def test_confirmation_executes_exact_basket_once_and_edits_all_stages_and_result(self):
        assistant = self.assistant()

        async def execute(basket, products, progress):
            self.assertEqual({product.id for product in products}, {'school', 'yogurt-product'})
            for phase in ('reading', 'replacing', 'verifying', 'checkout'):
                await progress(phase)
            return CartExecution(basket.id, basket.name, {'123': 1}, 25409, ['456'])

        assistant.execute.side_effect = execute
        callback = None

        def confirm(requests):
            nonlocal callback
            callback = self.proposal_callback(requests)
            return [callback, callback]

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')], confirm,
                                                          lambda _: [callback]])
        assistant.execute.assert_awaited_once()
        self.assertEqual(assistant.execute.await_args.args[0].id, 'yogurt')
        edits = [payload for method, payload in requests if method == 'editMessageText']
        self.assertTrue(all(payload['message_id'] == 71 for payload in edits))
        for label in ('✅ Читаю текущую корзину', '✅ Заменяю корзину',
                      '✅ Проверяю состав', '🔄 Проверяю наличие и сумму'):
            self.assertTrue(any(label in payload['text'] for payload in edits))
        final = edits[-1]
        self.assertIn('Завтрак-Йогурт', final['text'])
        self.assertIn('254,09 ₽', final['text'])
        self.assertIn('Недоступно SKU: 1', final['text'])
        self.assertIn('456', final['text'])
        self.assertEqual(final['reply_markup'], CART_LINKS)
        answer_index = next(i for i, (method, _) in enumerate(requests) if method == 'answerCallbackQuery')
        progress_index = next(i for i, (_, payload) in enumerate(requests) if 'Читаю текущую корзину' in payload.get('text', ''))
        self.assertLess(answer_index, progress_index)

    async def test_favorite_product_proposal_confirms_and_replaces_with_requested_quantity(self):
        assistant = self.assistant()
        assistant.propose.return_value = BasketProposal.model_validate({
            'kind': 'products', 'basket_id': None,
            'items': [{'product_id': 'school', 'quantity': 2}], 'message': 'Два любимых яблока.',
        })
        assistant.execute_products = AsyncMock(return_value=CartExecution(
            'products', 'Любимые продукты', {'555': 2}, 10000, []))

        requests = await self.run_assistant_flow(assistant, [[self.message('Закажи два яблока')],
            lambda log: [self.proposal_callback(log)]])

        assistant.execute_products.assert_awaited_once()
        selections = assistant.execute_products.await_args.args[0]
        self.assertEqual([(product.id, quantity) for product, quantity in selections], [('school', 2)])
        proposal = next(payload for method, payload in requests
                        if method == 'editMessageText' and payload.get('reply_markup', {}).get('inline_keyboard'))
        self.assertIn('Apple — 2 шт.', proposal['text'])

    async def test_wrong_nonce_message_and_owner_callbacks_cannot_execute(self):
        assistant = self.assistant()
        requests = await self.run_assistant_flow(assistant, [
            [self.message('Завтрак')],
            lambda log: [self.proposal_callback(log, data='cart-confirm:wrong'),
                         self.proposal_callback(log, message_id=72),
                         self.proposal_callback(log, sender=999),
                         self.proposal_callback(log, kind='group')],
        ])
        assistant.execute.assert_not_awaited()
        answers = [payload for method, payload in requests if method == 'answerCallbackQuery']
        self.assertEqual(len(answers), 4)
        self.assertTrue(all('устарел' in payload['text'] for payload in answers))

    async def test_cancel_start_and_new_proposal_invalidate_previous_nonce(self):
        for invalidation in ('cancel', '/start', '/start payload', '/start@synthetic_bot payload', 'Новый завтрак'):
            with self.subTest(invalidation=invalidation):
                assistant = self.assistant()
                old = None

                def invalidate(log):
                    nonlocal old
                    old = self.proposal_callback(log)
                    return [self.proposal_callback(log, action=1)] if invalidation == 'cancel' else [self.message(invalidation)]

                await self.run_assistant_flow(assistant, [[self.message('Завтрак')], invalidate,
                                                          lambda _: [old]])
                assistant.execute.assert_not_awaited()

    async def test_non_owner_and_non_private_messages_never_propose(self):
        assistant = self.assistant()
        requests = await self.run_assistant_flow(assistant, [[
            self.message('Завтрак', owner=999), self.message('Завтрак', kind='group'),
            self.message('Завтрак', sender=999),
        ]])
        assistant.propose.assert_not_awaited()
        self.assertEqual(assistant.propose.await_count, 0)

    async def test_active_menu_text_and_commands_never_reach_qwen(self):
        for sequence in (('/settings', 'неизвестный продукт'),
                         ('/cart', 'неизвестная категория'),
                         ('/cart', '🍳 Завтрак', 'неизвестная корзина'),
                         ('⚙️ Настройки', 'неизвестный режим'),
                         ('➕ Добавить продукт', 'Яблоки'),
                         ('/settings', '📦 1. Apple', '✏️ Название', 'Новое яблоко'),
                         ('/unknown',)):
            with self.subTest(sequence=sequence):
                assistant = self.assistant()
                await self.run_assistant_flow(assistant, [[self.message(text) for text in sequence]])
                assistant.propose.assert_not_awaited()

    async def test_deleted_basket_and_proposal_errors_leave_no_confirmation(self):
        path = self.basket_config()
        assistant = self.assistant()

        def delete_and_confirm(log):
            path.write_text('address: Delivery address\nproducts: []\nbaskets: []\n', encoding='utf-8')
            return [self.proposal_callback(log)]

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')], delete_and_confirm], path=path)
        assistant.execute.assert_not_awaited()
        final = [payload for method, payload in requests if method == 'editMessageText'][-1]
        self.assertIn('корзин', final['text'].lower())
        self.assertEqual(final['reply_markup'], {'inline_keyboard': []})
        assistant.propose.side_effect = CartError('Безопасная ошибка Qwen.')
        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')]])
        final = [payload for method, payload in requests if method == 'editMessageText'][-1]
        self.assertEqual(final['text'], 'Безопасная ошибка Qwen.')
        self.assertEqual(final['reply_markup'], {'inline_keyboard': []})

    async def test_failed_confirmation_invalidates_before_execution_and_removes_buttons(self):
        assistant = self.assistant()
        assistant.execute.side_effect = CartError('Безопасная ошибка CLI.')
        old = None

        def confirm(log):
            nonlocal old
            old = self.proposal_callback(log)
            return [old]

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')], confirm, lambda _: [old]])
        assistant.execute.assert_awaited_once()
        final = [payload for method, payload in requests if method == 'editMessageText'][-1]
        self.assertTrue(final['text'].endswith('CLI.'))
        self.assertIn('❌', final['text'])
        self.assertEqual(final['reply_markup'], {'inline_keyboard': []})

    async def test_failed_final_edit_falls_back_without_executing_again(self):
        assistant = self.assistant()
        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')],
                                                           lambda log: [self.proposal_callback(log)]], fail_edit=True)
        assistant.execute.assert_awaited_once()
        final = [payload for method, payload in requests if method == 'sendMessage'][-1]
        self.assertIn('254,09 ₽', final['text'])
        self.assertEqual(final['reply_markup'], CART_LINKS)

    async def test_busy_confirmation_cannot_be_replayed_after_manual_build_finishes(self):
        assistant = self.assistant()
        started, release = asyncio.Event(), asyncio.Event()

        async def build(**kwargs):
            started.set()
            await release.wait()
            return 'Готовая корзина'

        old = None

        def start(log):
            nonlocal old
            old = self.proposal_callback(log)
            return [self.message(text) for text in ('/cart', '🍳 Завтрак', '🧺 Завтрак-Йогурт')]

        async def while_busy(log):
            await started.wait()
            return [self.message('Ещё завтрак'), old]

        def finish(_):
            release.set()
            return []

        def replay_after_completion(log):
            self.assertTrue(any(method == 'editMessageText' and payload.get('text') == 'Готовая корзина'
                                for method, payload in log))
            return [old]

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')], start, while_busy,
                                                          finish, replay_after_completion], build=build)
        assistant.propose.assert_awaited_once()
        assistant.execute.assert_not_awaited()
        self.assertGreaterEqual(sum('уже идёт' in payload.get('text', '') for _, payload in requests), 2)
        answers = [payload['text'] for method, payload in requests if method == 'answerCallbackQuery']
        self.assertIn('уже идёт', answers[0])
        self.assertIn('устарел', answers[-1])

    async def test_cancel_during_manual_build_invalidates_pending_proposal(self):
        assistant = self.assistant()
        started, release = asyncio.Event(), asyncio.Event()
        old = cancel = None

        async def build(**kwargs):
            started.set()
            await release.wait()
            return 'Готовая корзина'

        def start(log):
            nonlocal old, cancel
            old = self.proposal_callback(log)
            cancel = self.proposal_callback(log, action=1)
            return [self.message(text) for text in ('/cart', '🍳 Завтрак', '🧺 Завтрак-Йогурт')]

        async def cancel_while_busy(_):
            await started.wait()
            return [cancel]

        def finish(_):
            release.set()
            return []

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')], start,
                                                          cancel_while_busy, finish, lambda _: [old]], build=build)
        assistant.execute.assert_not_awaited()
        answers = [payload['text'] for method, payload in requests if method == 'answerCallbackQuery']
        self.assertEqual(answers[0], 'Отменено.')
        self.assertIn('устарел', answers[-1])

    async def test_assistant_execution_blocks_manual_build_proposals_and_stop(self):
        assistant = self.assistant()
        started, release = asyncio.Event(), asyncio.Event()
        build = AsyncMock()

        async def execute(basket, products, progress):
            started.set()
            await release.wait()
            return CartExecution(basket.id, basket.name, {'123': 1}, None, [])

        assistant.execute.side_effect = execute

        async def while_busy(log):
            await started.wait()
            return [self.message('Ещё завтрак'), *[self.message(text) for text in
                    ('/cart', '🍳 Завтрак', '🧺 Завтрак-Йогурт')],
                    self.proposal_callback(log, data='cart-stop:old')]

        def finish(_):
            release.set()
            return []

        requests = await self.run_assistant_flow(assistant, [[self.message('Завтрак')],
            lambda log: [self.proposal_callback(log)], while_busy, finish], build=build)
        assistant.execute.assert_awaited_once()
        assistant.propose.assert_awaited_once()
        build.assert_not_awaited()
        self.assertTrue(any('уже идёт' in payload.get('text', '') for _, payload in requests))
        final = [payload for method, payload in requests if method == 'editMessageText'][-1]
        self.assertNotIn('Итого', final['text'])
        self.assertIn('Недоступно SKU: 0', final['text'])
        self.assertEqual(final['reply_markup'], CART_LINKS)

