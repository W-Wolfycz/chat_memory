from pathlib import Path
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class AssetsTestCase(unittest.TestCase):
    def test_required_plugin_files_exist(self):
        for relative in (
            "metadata.yaml",
            "main.py",
            "ui/repository.py",
            "ui/web_api.py",
            "requirements.txt",
            "pages/explorer/index.html",
            "pages/explorer/styles.css",
            "pages/explorer/app.js",
        ):
            path = PLUGIN_ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertGreater(path.stat().st_size, 0, relative)

    def test_page_uses_bridge_and_read_only_copy(self):
        html = (PLUGIN_ROOT / "pages/explorer/index.html").read_text(encoding="utf-8")
        javascript = (PLUGIN_ROOT / "pages/explorer/app.js").read_text(encoding="utf-8")
        self.assertIn("只读模式", html)
        self.assertIn("AstrBotPluginPage", javascript)
        self.assertIn("apiPost('query'", javascript)
        self.assertIn("Bridge 模式下这里收到的就是业务 payload", javascript)
        self.assertIn("数据源设置", html)
        self.assertIn("apiPost('settings'", javascript)
        self.assertNotIn("delete", javascript.lower())

    def test_astrbot_config_schema_is_valid(self):
        """并入后沿用 chat_memory 自身的配置 schema，必须是合法 JSON。"""
        import json

        schema = PLUGIN_ROOT / "_conf_schema.json"
        self.assertTrue(schema.exists())
        json.loads(schema.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
