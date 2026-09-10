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
        # 只读约束：写入口只有单条软删除/恢复；不得有批量删除或 HTTP DELETE
        self.assertNotIn("apiGet('delete", javascript)
        self.assertNotIn("method: 'DELETE'", javascript)
        self.assertIn("apiPost(action === 'restore' ? 'restore' : 'soft-delete'", javascript)

        # 小节：事件子类过滤必须随查询发出（防「下拉失效」回归）
        import re as _re

        collect = _re.search(r"function collectQuery\b[\s\S]*?\n}", javascript)
        self.assertIsNotNone(collect, "collectQuery 未找到")
        self.assertIn("event_type", collect.group(0))

        # 小节：软删除（schema v8）交互——模式切换、三态下拉、times/backup 图标
        self.assertIn("deleted_state", javascript)
        self.assertIn("已删除（软删）", javascript)
        collect_deleted = _re.search(r"function collectQuery\b[\s\S]*?\n}", javascript)
        self.assertIsNotNone(collect_deleted, "collectQuery 未找到")
        self.assertIn("deleted_state", collect_deleted.group(0))
        self.assertIn("filter-deleted", html)
        for option in ('value="all">All<', 'value="visible" selected>未删除<',
                       'value="deleted">已删除<'):
            self.assertIn(option, html, option)
        self.assertIn("id=\"mode-toggle\"", html)
        self.assertIn("删除模式", javascript)
        self.assertIn("TIMES_ICON", javascript)
        self.assertIn("BACKUP_ICON", javascript)
        self.assertIn("chip-deleted", javascript)
        self.assertIn("detail-deleted-time", javascript)
        # 写入口仅对插件实时库开放：前端据此拦截进入删除模式
        self.assertIn("write_enabled", javascript)
        # 工具调用表同样支持单条软删除：同一下拉 + 同一套图标/chip
        self.assertIn("tool-filter-deleted", html)
        collect_tools = _re.search(
            r"function collectToolQuery\b[\s\S]*?\n}", javascript
        )
        self.assertIsNotNone(collect_tools, "collectToolQuery 未找到")
        self.assertIn("deleted_state", collect_tools.group(0))
        self.assertIn("data-mode-target", javascript)
        self.assertIn("applyToolDeletedState", javascript)
        # 切换删除模式必须同时重绘工具列表（否则工具条目没有 times/backup 图标）
        set_mode = _re.search(r"function setDeleteMode\b[\s\S]*?\n}", javascript)
        self.assertIsNotNone(set_mode, "setDeleteMode 未找到")
        self.assertIn("runToolQuery", set_mode.group(0))
        # 工具卡片区分"轮内序号"与"记录 ID"
        self.assertIn("轮内 #${record.call_index} · 记录 #${record.tool_id}", javascript)
        # 展示层不再预截断：UMO/CID/turn_id 全部输出完整值（列表徽章 title 亦为完整值）
        self.assertNotIn("compactId", javascript)
        self.assertIn('>${escapeHtml(record.umo)}</span>', javascript)
        self.assertIn("cid:${escapeHtml(record.conversation_id", javascript)
        self.assertIn('title="轮次 turn_id">turn:${escapeHtml(record.turn_id)}</span>', javascript)
        self.assertIn('title="Conversation ID">cid:${escapeHtml(record.conversation_id)}</span>', javascript)

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
