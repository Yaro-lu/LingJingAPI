"""Verify fallback actions without opening desktop windows or a browser."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock


class RuntimeFallbackLinksTests(unittest.TestCase):
    def test_browser_links_and_extraction_codes(self):
        source = Path(__file__).resolve().parents[1] / "app/gui/main_gateway.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GatewayApp")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_show_runtime_download_fallback")
        browser = MagicMock()
        labels = []
        def label(*args, **kwargs):
            labels.append(kwargs.get("text", ""))
            return MagicMock()
        env = dict(tk=SimpleNamespace(Toplevel=MagicMock(), Label=label, Frame=MagicMock(),
                                     StringVar=MagicMock(), Entry=MagicMock()),
                   C={k: k for k in ("bg", "text", "text2", "card", "error", "entry", "warn")},
                   F={k: k for k in ("title", "normal", "small")}, webbrowser=browser,
                   PROJECT_HOMEPAGE_URL="https://github.com/Yaro-lu/LingJingAPI",
                   RUNTIME_PACKAGE_NAME="runtime.7z")
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), env)
        app = MagicMock()
        buttons = []
        def button(parent, text, command, *args, **kwargs):
            buttons.append((text, command))
            return MagicMock()
        app._button.side_effect = button
        env[method.name](app, "all routes failed")
        actions = dict(buttons)
        actions["打开 123 云盘"]()
        actions["打开 百度网盘"]()
        self.assertEqual(browser.open.call_args_list[0].args[0],
                         "https://1817692300.share.123pan.cn/123pan/lZtTjv-jOG4h?pwd=KkJt#")
        self.assertEqual(browser.open.call_args_list[1].args[0],
                         "https://pan.baidu.com/s/1Tt5khCdRs13nymP9hHCn7Q")
        for text, command in buttons:
            if text == "复制提取码":
                command()
        self.assertEqual([c.args[0] for c in app._copy.call_args_list], ["KkJt", "wdmt"])
        self.assertIn("提取码：KkJt", labels)
        self.assertIn("提取码：wdmt", labels)
        self.assertTrue(any("all routes failed" in text for text in labels))
        actions["选择本地环境包"]()
        app._select_runtime.assert_called_once()


if __name__ == "__main__":
    unittest.main()
