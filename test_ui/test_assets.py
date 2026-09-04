from pathlib import Path
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class AssetsTestCase(unittest.TestCase):
    def test_plugin_assets(self):
        # 小节：必需文件存在且非空
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

        # 小节：页面用 Bridge 且只读（无删除能力）
        html = (PLUGIN_ROOT / "pages/explorer/index.html").read_text(encoding="utf-8")
        javascript = (PLUGIN_ROOT / "pages/explorer/app.js").read_text(encoding="utf-8")
        self.assertIn("只读模式", html)
        self.assertIn("AstrBotPluginPage", javascript)
        self.assertIn("apiPost('query'", javascript)
        self.assertIn("Bridge 模式下这里收到的就是业务 payload", javascript)
        self.assertIn("数据源设置", html)
        self.assertIn("export-button", html)
        self.assertIn("apiPost('settings'", javascript)
        self.assertNotIn("delete", javascript.lower())

        # 小节：配置 schema 为合法 JSON
        import json

        schema = PLUGIN_ROOT / "_conf_schema.json"
        self.assertTrue(schema.exists())
        json.loads(schema.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
