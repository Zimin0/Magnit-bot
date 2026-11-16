import ctypes
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, ConfigDict, Field, ValidationError


BINDING_PATH = Path(__file__).resolve().with_name('telegram-binding.dpapi')


class BindingError(Exception):
    pass


class TelegramBinding(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    token: str = Field(pattern=r'^[0-9]+:[a-zA-Z0-9_-]+$', repr=False)
    chat_id: str = Field(pattern=r'^[1-9][0-9]*$')
    offset: int = Field(ge=0)


class DataBlob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def crypt_binding(data: bytes, *, decrypt: bool) -> bytes:
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    transform = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    transform.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    transform.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), buffer)
    result = DataBlob()
    if not transform(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def load_binding(path: Path = BINDING_PATH) -> TelegramBinding | None:
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise BindingError('Не удалось прочитать файл привязки Telegram.') from None
    try:
        return TelegramBinding.model_validate_json(crypt_binding(encrypted, decrypt=True))
    except (OSError, ValidationError):
        raise BindingError('Не удалось открыть сохранённую привязку. Запустите telegram_setup с --rebind.') from None


def save_binding(binding: TelegramBinding, path: Path = BINDING_PATH) -> None:
    temporary = None
    try:
        encrypted = crypt_binding(binding.model_dump_json().encode('utf-8'), decrypt=False)
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(encrypted)
        temporary.replace(path)
    except OSError:
        raise BindingError('Не удалось сохранить зашифрованную привязку Telegram.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
