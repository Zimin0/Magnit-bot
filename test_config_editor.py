from pathlib import Path
import unittest


class ConfigEditorUiTests(unittest.TestCase):
    def test_favorite_product_editor_requires_sku_and_marks_legacy_entries(self):
        root = Path(__file__).parent / 'config_ui'
        html = (root / 'index.html').read_text(encoding='utf-8')
        script = (root / 'app.js').read_text(encoding='utf-8')

        self.assertIn('Любимые продукты', html)
        self.assertIn("textField('sku', 'Артикул «Магнита»')", script)
        self.assertIn('Артикул должен состоять из цифр.', script)
        self.assertIn('Недоступен для Qwen — укажите артикул', script)


if __name__ == '__main__':
    unittest.main()
