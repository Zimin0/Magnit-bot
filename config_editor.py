import argparse
import base64
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import ValidationError

from cart_service import SETTINGS_PATH
from magnit_cart import CartError, CartSettings, load_settings, save_settings
from telegram_menu import photo_path


ASSETS = Path(__file__).with_name('config_ui')
MAX_PHOTO = 10_000_000
MAX_REQUEST = 36_000_000


class Conflict(Exception):
    pass


class ConfigEditor:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()

    def revision(self) -> str:
        source = self.path if self.path.exists() else self.path.with_suffix('.json')
        return sha256(source.read_bytes()).hexdigest() if source.is_file() else ''

    def read(self) -> dict:
        with self.lock:
            revision = self.revision()
            settings = load_settings(self.path)
            if revision != self.revision():
                raise Conflict('Конфиг обновился во время чтения. Откройте редактор заново.')
        return {'revision': revision, 'settings': settings.model_dump()}

    def save(self, revision: str, data: dict, uploads: dict) -> dict:
        with self.lock:
            if revision != self.revision():
                raise Conflict('Конфиг уже изменился. Скопируйте нужные правки и обновите страницу перед сохранением.')
            settings = CartSettings.model_validate(data)
            records = [*settings.products, *(option for basket in settings.baskets
                         for group in basket.groups for option in group.options)]
            photos = {}
            for record in records:
                key = record.photo
                if key and key.startswith('pending:'):
                    if key not in photos:
                        raw = base64.b64decode(uploads[key], validate=True)
                        if len(raw) > MAX_PHOTO:
                            raise ValueError('Фото должно занимать не более 10 МБ.')
                        if raw.startswith(b'\x89PNG\r\n\x1a\n'):
                            suffix = '.png'
                        elif raw.startswith(b'\xff\xd8\xff'):
                            suffix = '.jpg'
                        else:
                            raise ValueError('Поддерживаются фотографии JPG и PNG.')
                        photos[key] = (f'photos/{uuid4().hex}{suffix}', raw)
                    record.photo = photos[key][0]
                elif key:
                    photo_path(self.path.parent, key)

            created = []
            try:
                for relative, raw in photos.values():
                    target = photo_path(self.path.parent, relative)
                    target.parent.mkdir(exist_ok=True)
                    with target.open('xb') as image:
                        created.append(target)
                        image.write(raw)
                if revision != self.revision():
                    raise Conflict('Конфиг изменился во время сохранения. Обновите страницу.')
                save_settings(self.path, settings)
            except Exception:
                for target in created:
                    target.unlink(missing_ok=True)
                raise
            return {'revision': self.revision(), 'settings': settings.model_dump()}


def make_server(path: Path, port: int) -> ThreadingHTTPServer:
    editor = ConfigEditor(path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_content(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, code: int, data: dict) -> None:
            self.send_content(code, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

        def local_origin(self) -> str:
            return f'http://127.0.0.1:{self.server.server_port}'

        def do_GET(self) -> None:
            if self.headers.get('Host') != self.local_origin().removeprefix('http://'):
                self.json_response(403, {'error': 'Откройте редактор по локальному адресу 127.0.0.1.'})
                return
            url = urlsplit(self.path)
            static = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                      '/styles.css': ('styles.css', 'text/css')}
            try:
                if url.path in static:
                    name, kind = static[url.path]
                    self.send_content(200, (ASSETS / name).read_bytes(), f'{kind}; charset=utf-8')
                elif url.path == '/api/config':
                    self.json_response(200, editor.read())
                elif url.path == '/api/photo':
                    target = photo_path(path.parent, parse_qs(url.query).get('ref', [''])[0])
                    if not target.is_file() or target.stat().st_size > MAX_PHOTO:
                        raise FileNotFoundError
                    self.send_content(200, target.read_bytes(), 'image/png' if target.suffix.lower() == '.png' else 'image/jpeg')
                else:
                    self.json_response(404, {'error': 'Страница не найдена.'})
            except Conflict as exc:
                self.json_response(409, {'error': str(exc)})
            except CartError as exc:
                self.json_response(422, {'error': str(exc)})
            except (ValueError, OSError):
                self.json_response(404, {'error': 'Файл не найден.'})

        def do_PUT(self) -> None:
            origin = self.local_origin()
            if (self.headers.get('Host') != origin.removeprefix('http://')
                    or self.headers.get('Origin') != origin):
                self.json_response(403, {'error': 'Сохранение доступно из локального редактора.'})
                return
            if self.path != '/api/config':
                self.json_response(404, {'error': 'Страница не найдена.'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_REQUEST:
                    self.json_response(413, {'error': 'Слишком большой запрос. Сохраните фотографии в несколько приёмов.'})
                    return
                data = json.loads(self.rfile.read(size))
                self.json_response(200, editor.save(data['revision'], data['settings'], data.get('uploads', {})))
            except Conflict as exc:
                self.json_response(409, {'error': str(exc)})
            except ValidationError as exc:
                error = exc.errors(include_input=False)[0]
                field = '.'.join(str(part + 1) if isinstance(part, int) else part for part in error['loc'])
                self.json_response(422, {'error': f'{field} — {error["msg"]}' if field else error['msg']})
            except (KeyError, TypeError, ValueError):
                self.json_response(422, {'error': 'Проверьте данные и формат фотографий. Допустимы JPG и PNG до 10 МБ.'})
            except OSError:
                self.json_response(500, {'error': 'Не удалось записать конфиг или фотографию.'})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Локальный редактор корзин «Магнита»')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with make_server(SETTINGS_PATH, args.port) as server:
        print(f'Редактор корзин: http://127.0.0.1:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
