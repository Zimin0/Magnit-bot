import argparse
import asyncio
import logging
import os
import re
import secrets
import sys
import warnings
from getpass import GetPassWarning, getpass
from logging.handlers import RotatingFileHandler

import httpx

from cart_service import CartService, SETTINGS_PATH
from cart_assistant import CartAssistant
from magnit_api import capture_cli_context
from magnit_browser import open_profile
from magnit_cart import CartError, load_settings
from magnit_cli import MagnitCli, MagnitCliError
from telegram_binding import BindingError, TelegramBinding, load_binding, save_binding
from telegram_bot import poll_telegram


LOG_PATH = SETTINGS_PATH.with_name('magnit-bot.log')
logger = logging.getLogger('telegram_setup')


def configure_logging() -> None:
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
    app_modules = {'telegram_setup', 'telegram_bot', 'magnit_api', 'magnit_cart', 'cart_service'}
    handlers = [file_handler, logging.StreamHandler()]
    for handler in handlers:
        handler.addFilter(lambda record: record.name in app_modules)
    logging.basicConfig(level=logging.WARNING, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')


def telegram_request(client: httpx.Client, method: str, **parameters):
    try:
        response = client.post(method, json=parameters)
        response.raise_for_status()
        data = response.json()
        if data.get('ok') is True:
            return data['result']
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 404}:
            raise SystemExit('Telegram не принял токен. Проверьте новый токен в BotFather.') from None
        if exc.response.status_code == 409:
            raise SystemExit('Telegram не может выдать сообщения. Остановите другую программу, которая читает этого бота.') from None
    except (httpx.RequestError, ValueError, KeyError):
        pass
    raise SystemExit('Не удалось получить ответ от Telegram. Проверьте подключение и повторите запуск.') from None


def pair_bot() -> TelegramBinding:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', GetPassWarning)
            token = getpass('Новый токен бота из BotFather: ').strip()
    except GetPassWarning:
        raise SystemExit('Запустите команду в обычном окне PowerShell для скрытого ввода токена.') from None
    if not re.fullmatch(r'[0-9]+:[a-zA-Z0-9_-]+', token):
        raise SystemExit('Вставьте новый токен целиком в ответ на приглашение.')

    with httpx.Client(base_url=f'https://api.telegram.org/bot{token}/', timeout=15) as client:
        bot = telegram_request(client, 'getMe')
        code = f'magnit-{secrets.token_hex(6)}'
        print(f'Откройте https://t.me/{bot["username"]}, нажмите «Старт» и отправьте сообщение {code}')
        offset = 0
        while True:
            input('Отправили сообщение? Нажмите Enter, чтобы проверить. ')
            updates = telegram_request(client, 'getUpdates', offset=offset, allowed_updates=['message'])
            for update in updates:
                offset = update['update_id'] + 1
                message = update.get('message', {})
                chat = message.get('chat', {})
                if chat.get('type') == 'private' and message.get('text') == code:
                    chat_id = str(chat['id'])
                    break
            else:
                print('Сообщение пока не найдено. Проверьте код и чат с ботом.')
                continue
            break

    return TelegramBinding(token=token, chat_id=chat_id, offset=offset)


async def run_bot(binding: TelegramBinding) -> bool | None:
    def save_offset(offset: int) -> None:
        save_binding(binding.model_copy(update={'offset': offset}))

    try:
        settings = load_settings(SETTINGS_PATH)
        async with open_profile(headless=True) as page:
            cli_context = await capture_cli_context(page, settings.address)
    except CartError as exc:
        raise SystemExit(
            f'Не удалось получить сессию «Магнита»: {exc}'
        ) from None
    try:
        cli = MagnitCli(context=cli_context)
        if not (cli.cli_dir / 'magnit.py').is_file():
            raise MagnitCliError('Magnit CLI entrypoint is unavailable.')
    except (MagnitCliError, OSError, ValueError):
        raise SystemExit('Проверьте MAGNIT_CLI_DIR: укажите папку Magnit CLI с файлом magnit.py. '
                         'Запустите бота из текущего venv командой python -m telegram_setup.') from None
    async with httpx.AsyncClient(base_url='http://127.0.0.1:11434',
                                 timeout=httpx.Timeout(180, connect=5), trust_env=False) as ollama:
        cart = CartService(ollama, SETTINGS_PATH)
        assistant = CartAssistant(ollama, cli)
        return await poll_telegram(binding.token, binding.chat_id, binding.offset, SETTINGS_PATH,
                                   cart.build, assistant=assistant, save_offset=save_offset)


def main(*, rebind: bool = False) -> None:
    binding = None if rebind else load_binding()
    if binding is None:
        binding = pair_bot()
        save_binding(binding)
    print('Telegram подключён. Отправьте боту /start.')
    if asyncio.run(run_bot(binding)) is True:
        try:
            os.execv(sys.executable, [sys.executable, '-m', 'telegram_setup'])
        except OSError as exc:
            raise SystemExit(f'Не удалось перезапустить бота ({type(exc).__name__}). Запустите его вручную.') from None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rebind', action='store_true', help='Заново подключить бота или сменить токен')
    try:
        configure_logging()
        main(rebind=parser.parse_args().rebind)
    except (BindingError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
            raise
        logger.error('Не удалось запустить бота: %s', exc)
        raise SystemExit(str(exc)) from None
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('Работа остановлена.') from None

