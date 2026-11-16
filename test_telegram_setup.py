import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import telegram_setup
from telegram_binding import TelegramBinding


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
        with TemporaryDirectory() as directory:
            (Path(directory) / 'magnit.py').write_text('# synthetic entrypoint', encoding='utf-8')
            with patch.object(telegram_setup, 'MagnitCli') as cli_type, \
                    patch.object(telegram_setup, 'CartAssistant') as assistant_type, \
                    patch.object(telegram_setup, 'CartService') as service_type, \
                    patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
                cli_type.return_value.cli_dir = Path(directory)
                await telegram_setup.run_bot(binding)
            cli_type.assert_called_once_with()
            assistant_type.assert_called_once()
            self.assertIs(assistant_type.call_args.args[1], cli_type.return_value)
            self.assertIs(assistant_type.call_args.args[0], service_type.call_args.args[0])
            self.assertIs(poll.await_args.kwargs['assistant'], assistant_type.return_value)
            self.assertIs(poll.await_args.args[4], service_type.return_value.build)

    async def test_missing_cli_configuration_fails_safely_before_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        with patch.dict('os.environ', {'MAGNIT_CLI_DIR': ''}), \
                patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
            with self.assertRaises(SystemExit) as raised:
                await telegram_setup.run_bot(binding)
        self.assertIn('MAGNIT_CLI_DIR', str(raised.exception))
        self.assertIn('magnit.py', str(raised.exception))
        poll.assert_not_awaited()

    async def test_missing_cli_entrypoint_fails_safely_before_polling(self):
        binding = TelegramBinding(token='123:synthetic', chat_id='456', offset=789)
        with TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'MAGNIT_CLI_DIR': directory}), \
                patch.object(telegram_setup, 'poll_telegram', new_callable=AsyncMock) as poll:
            with self.assertRaises(SystemExit) as raised:
                await telegram_setup.run_bot(binding)
        message = str(raised.exception)
        self.assertIn('MAGNIT_CLI_DIR', message)
        self.assertIn('magnit.py', message)
        self.assertNotIn(directory, message)
        poll.assert_not_awaited()

