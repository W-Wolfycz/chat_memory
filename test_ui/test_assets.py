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
        for option in ('value="all">all<', 'value="visible" selected>未删除<',
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
        # 下拉统一口径：首项是全集（all / group + private / user + assistant）
        self.assertIn('<option value="">all</option>', html)
        self.assertIn('<option value="">group + private</option>', html)
        self.assertIn('<option value="">user + assistant</option>', html)
        self.assertIn('<option value="1">仅看配对</option>', html)
        # 工具页不再有 turn_id 精确搜索
        self.assertNotIn("tool-filter-turn", html)
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
        # 工具卡片只留"第几步"与可读会话；技术值（轮次/UMO/CID/记录 ID）收进详情
        self.assertIn("function toolStepLabel", javascript)
        self.assertIn("轮 ${short} · 第 ${index} 次调用", javascript)
        self.assertIn("function toolTurnShort", javascript)
        self.assertNotIn("turn_no", javascript, "轮次编号不再另算")
        self.assertIn("function toolCallerAvatar", javascript)
        self.assertIn("['BOT', record.platform_id || '（未知）']", javascript)
        self.assertIn("data-tool-detail-id", javascript)
        self.assertIn("function openToolDetail", javascript)
        self.assertNotIn("轮内 #", javascript, "卡片上不再出现轮内编号")
        self.assertNotIn("记录 #${record.tool_id}", javascript, "卡片上不再出现记录 ID")
        # 技术值仍完整输出，只是位置在详情字段表里
        self.assertNotIn("compactId", javascript)
        self.assertIn("['轮次 turn_id', record.turn_id || '（无）']", javascript)
        self.assertIn("['会话 UMO', record.umo]", javascript)
        self.assertIn("['Conversation ID', record.conversation_id]", javascript)

        # 小节：查询台 about 版本必须来自 metadata.yaml（版本单源），不得在代码里写死
        import re as _re

        meta = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version_match = _re.search(r"^version:\s*(\S+)\s*$", meta, _re.M)
        self.assertIsNotNone(version_match, "metadata.yaml 缺少 version")
        web_api = (PLUGIN_ROOT / "ui" / "web_api.py").read_text(encoding="utf-8")
        self.assertIn('"version": self.plugin_version', web_api, "about 版本必须由 main 从 metadata 传入")
        self.assertNotRegex(
            web_api, r'"version":\s*"\d', "about 版本号不得写死（升版后会静默显示旧版本）"
        )

        # 小节：配置 schema 为合法 JSON
        import json

        schema = PLUGIN_ROOT / "_conf_schema.json"
        self.assertTrue(schema.exists())
        json.loads(schema.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
