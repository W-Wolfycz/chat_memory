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

        # 小节：事件子类过滤必须随查询发出（防「下拉失效」回归）
        import re as _re

        collect = _re.search(r"function collectQuery\b[\s\S]*?\n}", javascript)
        self.assertIsNotNone(collect, "collectQuery 未找到")
        self.assertIn("event_type", collect.group(0))

        # 小节：查询台 about 版本与 metadata.yaml 一致（防双处漂移）
        import re as _re

        meta = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version_match = _re.search(r"^version:\s*(\S+)\s*$", meta, _re.M)
        self.assertIsNotNone(version_match, "metadata.yaml 缺少 version")
        version = version_match.group(1)
        web_api = (PLUGIN_ROOT / "ui" / "web_api.py").read_text(encoding="utf-8")
        self.assertIn(f'"version": "{version}"', web_api, "about 版本与 metadata 不一致")

        # 小节：配置 schema 为合法 JSON
        import json

        schema = PLUGIN_ROOT / "_conf_schema.json"
        self.assertTrue(schema.exists())
        json.loads(schema.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
