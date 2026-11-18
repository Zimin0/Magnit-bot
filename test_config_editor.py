from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from config_editor import ConfigEditor
from magnit_cart import CartSettings, save_settings


class ConfigEditorUiTests(unittest.TestCase):
    def test_editor_saves_baskets_that_reference_catalog_products(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / 'settings.yaml'
            settings = CartSettings.model_validate({
                'address': 'Address',
                'products': [{'id': 'yogurt', 'sku': '111', 'name': 'Йогурт'}],
                'baskets': [{
                    'id': 'breakfast', 'name': 'Завтрак', 'category': 'Утро',
                    'groups': [{'name': 'Йогурты', 'options': [{'product_id': 'yogurt'}]}],
                }],
            })
            save_settings(path, settings)
            editor = ConfigEditor(path)

            saved = editor.save(editor.revision(), settings.model_dump(), {})

            self.assertEqual(saved['settings']['baskets'][0]['groups'][0]['options'],
                             [{'product_id': 'yogurt', 'quantity': None}])

    def test_editor_uses_one_product_catalog_and_basket_product_references(self):
        root = Path(__file__).parent / 'config_ui'
        html = (root / 'index.html').read_text(encoding='utf-8')
        script = (root / 'app.js').read_text(encoding='utf-8')

        self.assertIn('Любимые продукты', html)
        self.assertIn("textField('sku', 'Артикул «Магнита»')", script)
        self.assertIn('Артикул должен состоять из цифр.', script)
        self.assertIn("selectField('product_id', 'Любимый продукт'", script)
        self.assertNotIn("const purposes = ['завтрак', 'в школу'];", script)
        self.assertNotIn("purpose === 'завтрак' ? 'Завтрак' : 'В школу'", script)


if __name__ == '__main__':
    unittest.main()
