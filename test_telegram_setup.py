import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_setup
from magnit_cart import CartError
from telegram_binding import TelegramBinding


CLI_CONTEXT = {
    'MAGNIT_DEVICE_ID': 'device-id',
    'MAGNIT_DEVICE_TAG': 'device-tag',
    'MAGNIT_PERMISSIONS_JWT': 'permissions-jwt',
    'MAGNIT_STORE_CODE': 'store-code',
    'MAGNIT_ADDRESS_ID': 'address-id',
    'MAGNIT_SERVICE': 'express',
    'MAGNIT_DELIVERY_TYPE': 'delivery',
}


class TelegramSetupTests(unittest.TestCase):
    def test_saved_binding_skips_token_and_pairing_prompts(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        with patch.object(telegram_setup, 'load_binding', return_value=binding), \
                patch.object(telegram_setup, 'getpass') as getpass, \
                patch('builtins.input') as prompt, \
                patch.object(telegram_setup, 'run_bot') as run:
            telegram_setup.main()
        getpass.assert_not_called()
        prompt.assert_not_called()
        run.assert_awaited_once_with(binding)


class TelegramStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_bot_wires_one_cli_and_assistant_into_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        page = object()
        events = []
        profile = AsyncMock()

        async def enter_profile():
            events.append('open')
            return page

        async def exit_profile(*_args):
            events.append('close')
            return False

        async def capture_context(*_args):
            events.append('capture')
            return CLI_CONTEXT

        async def start_polling(*_args, **_kwargs):
            events.append('poll')

        profile.__aenter__.side_effect = enter_profile
        profile.__aexit__.side_effect = exit_profile
        with TemporaryDirectory() as directory:
            (Path(directory) / 'magnit.py').write_text('# synthetic entrypoint', encoding='utf-8')
            with patch.object(telegram_setup, 'MagnitCli') as cli_type, \
                    patch.object(telegram_setup, 'load_settings',
                                 return_value=SimpleNamespace(address='Test address')) as settings, \
                    patch.object(telegram_setup, 'open_profile', return_value=profile) as open_profile, \
                    patch.object(telegram_setup, 'capture_cli_context', new_callable=AsyncMock,
                                 side_effect=capture_context) as capture, \
                    patch.object(telegram_setup, 'CartAssistant') as assistant_type, \
                    patch.object(telegram_setup, 'CartService') as service_type, \
                    patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock,
                                 side_effect=start_polling) as poll:
                cli_instance = cli_type.return_value
                cli_instance.cli_dir = Path(directory)

                def create_cli(**_kwargs):
                    events.append('cli')
                    return cli_instance

                cli_type.side_effect = create_cli
                await telegram_setup.run_bot(binding)
            settings.assert_called_once_with(telegram_setup.SETTINGS_PATH)
            open_profile.assert_called_once_with(headless=True)
            capture.assert_awaited_once_with(page, 'Test address')
            profile.__aexit__.assert_awaited_once()
            cli_type.assert_called_once_with(context=CLI_CONTEXT)
            self.assertEqual(events, ['open', 'capture', 'close', 'cli', 'poll'])
            assistant_type.assert_called_once()
            self.assertIs(assistant_type.call_args.args[1], cli_type.return_value)
            self.assertIs(assistant_type.call_args.args[0], service_type.call_args.args[0])
            self.assertIs(poll.await_args.kwargs['assistant'], assistant_type.return_value)
            self.assertIs(poll.await_args.args[4], service_type.return_value.build)

    async def test_missing_cli_configuration_fails_safely_before_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        profile = AsyncMock()
        profile.__aenter__.return_value = object()
        profile.__aexit__.return_value = False
        with patch.dict('os.environ', {'MAGNIT_CLI_DIR': ''}), \
                patch.object(telegram_setup, 'load_settings',
                             return_value=SimpleNamespace(address='Test address')), \
                patch.object(telegram_setup, 'open_profile', return_value=profile), \
                patch.object(telegram_setup, 'capture_cli_context', new_callable=AsyncMock,
                             return_value=CLI_CONTEXT), \
                patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
            with self.assertRaises(SystemExit) as raised:
                await telegram_setup.run_bot(binding)
        self.assertIn('MAGNIT_CLI_DIR', str(raised.exception))
        self.assertIn('magnit.py', str(raised.exception))
        poll.assert_not_awaited()

    async def test_missing_cli_entrypoint_fails_safely_before_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        profile = AsyncMock()
        profile.__aenter__.return_value = object()
        profile.__aexit__.return_value = False
        with TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'MAGNIT_CLI_DIR': directory}), \
                patch.object(telegram_setup, 'load_settings',
                             return_value=SimpleNamespace(address='Test address')), \
                patch.object(telegram_setup, 'open_profile', return_value=profile), \
                patch.object(telegram_setup, 'capture_cli_context', new_callable=AsyncMock,
                             return_value=CLI_CONTEXT), \
                patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
            with self.assertRaises(SystemExit) as raised:
                await telegram_setup.run_bot(binding)
        message = str(raised.exception)
        self.assertIn('MAGNIT_CLI_DIR', message)
        self.assertIn('magnit.py', message)
        self.assertNotIn(directory, message)
        poll.assert_not_awaited()

    async def test_context_capture_failure_is_safe_and_stops_before_cli_and_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        profile = AsyncMock()
        profile.__aenter__.return_value = object()
        profile.__aexit__.return_value = False
        with patch.object(telegram_setup, 'load_settings',
                          return_value=SimpleNamespace(address='Test address')), \
                patch.object(telegram_setup, 'open_profile', return_value=profile), \
                patch.object(telegram_setup, 'capture_cli_context', new_callable=AsyncMock,
                             side_effect=CartError('private-session-value')), \
                patch.object(telegram_setup, 'MagnitCli') as cli_type, \
                patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
            with self.assertRaises(SystemExit) as raised:
                await telegram_setup.run_bot(binding)

        message = str(raised.exception)
        self.assertIn('private-session-value', message)
        profile.__aexit__.assert_awaited_once()
        cli_type.assert_not_called()
        poll.assert_not_awaited()

