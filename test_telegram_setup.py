import unittest
from unittest.mock import patch

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
