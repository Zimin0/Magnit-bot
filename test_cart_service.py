from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cart_service import CartService
from magnit_cart import CartError


@asynccontextmanager
async def profile():
    yield object()


class CartServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_precedes_assembly_and_failed_clear_stops_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.yaml'
            path.write_text('''address: Test address
backend: api
products:
  - {id: oats, name: Oats, purpose: завтрак}
''', encoding='utf-8')
            store = Mock()
            store.clear_cart = AsyncMock()
            result = Mock()
            result.message.return_value = 'Готово'

            async def assemble(*args, **kwargs):
                store.clear_cart.assert_awaited_once()
                return result

            with patch('cart_service.open_profile', side_effect=lambda **kwargs: profile()), \
                    patch('cart_service.MagnitApiStore.connect', return_value=store), \
                    patch('cart_service.assemble_cart', side_effect=assemble) as build:
                service = CartService(AsyncMock(), path)
                self.assertEqual(await service.build(purpose='завтрак'), 'Готово')
                build.reset_mock()
                store.clear_cart.side_effect = CartError('Очистка не подтверждена')
                with self.assertRaises(CartError):
                    await service.build(purpose='завтрак')
                build.assert_not_called()
