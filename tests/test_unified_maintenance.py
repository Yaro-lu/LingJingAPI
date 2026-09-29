"""Exercise maintenance widgets and callbacks without creating desktop windows."""
import unittest
from pathlib import Path
from unittest import mock

from app.gui import dashboard_pages, main_gateway
from app.gui.dashboard_pages import StaticDashboardPages
from app.gui.main_gateway import C, F, GatewayApp


class Widget:
    def __init__(self, parent=None, **options):
        self.options = options
        self.children = []
        self.bindings = {}
        if parent is not None:
            parent.children.append(self)

    def pack(self, **options):
        self.pack_options = options

    pack_configure = pack

    def configure(self, **options):
        self.options.update(options)

    config = configure

    def bind(self, event, callback, **options):
        self.bindings[event] = callback

    def columnconfigure(self, *args, **kwargs):
        pass

    def winfo_children(self):
        return self.children

    def pack_propagate(self, flag):
        self.propagate = flag

    def grid_propagate(self, flag):
        pass

    def create_window(self, *args, **kwargs):
        return 1

    def yview(self):
        return (0, 1)

    def set(self, *args):
        pass

    def yview_scroll(self, *args):
        self.last_scroll = args

    def itemconfigure(self, *args, **kwargs):
        pass

    def bbox(self, *args):
        return (0, 0, 800, 1000)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class UnifiedMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.app = mock.Mock()
        self.app._model_status = {"A": "完整", "B": "缺失", "C": "完整", "missing": {"B": ["b.safetensors"]}}
        self.app._environment_status = {}
        self.app._workflow_model_key.side_effect = lambda item: item.get("model_group", "")
        self.app._workflow_model_available.return_value = True
        self.app._storage_directory.return_value = Path("isolated")
        self.app._card.side_effect = lambda parent: Widget(parent)
        self.app._button.side_effect = lambda parent, text, command, variant, width: Widget(parent, text=text, command=command)
        self.pages = StaticDashboardPages(self.app, C, F)
        self.pages._badge = lambda parent, text, tone: Widget(parent, text=text)
        self.pages._workflow_type_label = lambda item: ("", "图片", "primary")
        self.records = [
            {"id": "ready", "name": "已就绪的工作流", "model_group": "A", "enabled": True, "dependency_status": "ready"},
            {"id": "disabled", "name": "停用的工作流", "model_group": "B", "enabled": False},
            {"id": "custom", "name": "自定义节点工作流", "missing_nodes": ["CustomNode"], "dependency_status": "ready"},
        ]
        self.pages._workflow_records = lambda: self.records
        self.requirements = {key: {"title": title} for key, title in [("A", "关联模型A"), ("B", "关联模型B"), ("C", "独立模型C")]}
        self.app._vertical_scrollbar.side_effect = lambda parent, command: Widget(parent)
        self.frames = mock.patch.multiple(dashboard_pages.tk, Frame=Widget, Label=Widget, Canvas=Widget)
        self.frames.start()
        self.addCleanup(self.frames.stop)
        patcher = mock.patch.object(dashboard_pages, "MODEL_REQUIREMENTS", self.requirements)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def actions(root, text):
        return [w for w in root.walk() if w.options.get("text") == text and "command" in w.options]

    def test_pending_workflow_status_precedes_disabled_and_missing_file(self):
        pages = object.__new__(StaticDashboardPages)
        item = {"api_mapping_status": "pending_conversion", "enabled": False, "workflow_json": ""}
        state, tone, detail, key = pages._workflow_state(item)
        self.assertEqual((state, tone), ("待修复", "neutral"))
        app = object.__new__(GatewayApp)
        self.assertFalse(app._workflow_model_available(item))
        self.assertEqual(app._workflow_status_text(item, False), "待修复")

    def test_all_workflow_actions_keep_their_target_and_disabled_model_download(self):
        root = Widget()
        self.pages._build_workflow_models(root)
        for widget in self.actions(root, "配置 / 详情"):
            widget.options["command"]()
        self.assertEqual(self.app._show_workflow_schema.call_args_list, [mock.call(item) for item in self.records])
        self.actions(root, "启用")[0].options["command"]()
        self.app._set_workflow_enabled.assert_called_with("disabled", True)
        for widget in self.actions(root, "停用"):
            widget.options["command"]()
        self.assertEqual(self.app._set_workflow_enabled.call_args_list[-2:], [mock.call("ready", False), mock.call("custom", False)])
        self.assertEqual(len(self.actions(root, "设为默认")), 1)
        self.actions(root, "设为默认")[0].options["command"]()
        self.app._set_default_workflow.assert_called_once_with("ready")
        for widget in self.actions(root, "模型文件"):
            widget.options["command"]()
        self.assertEqual(self.app._show_workflow_model_help.call_args_list, [mock.call(item) for item in self.records])
        texts = [w.options.get("text") for w in root.walk()]
        self.assertTrue(any("模型缺少 1 个文件" in str(text) for text in texts))
        self.assertTrue(any("缺少 1 个节点" in str(text) for text in texts))
        self.assertIn("独立模型C", texts)
        self.assertNotIn("关联模型A", texts)
        self.assertNotIn("关联模型B", texts)

    def test_outer_wheel_binding_preserves_the_nested_list(self):
        root = Widget()
        self.pages._build_workflow_models(root)
        child = self.actions(root, "模型文件")[0]
        outer = Widget()
        self.pages._bind_mousewheel_tree(root, outer)
        event = mock.Mock(num=None, delta=-120)
        self.assertEqual(child.bindings["<MouseWheel>"](event), "break")
        self.assertEqual(self.pages._workflow_list_canvas.last_scroll, (1, "units"))
        self.assertFalse(hasattr(outer, "last_scroll"))

    def test_empty_catalog_keeps_every_model_and_import_entry(self):
        self.records.clear()
        root = Widget()
        self.pages._build_workflow_models(root)
        texts = [w.options.get("text") for w in root.walk()]
        for item in self.requirements.values():
            self.assertIn(item["title"], texts)
        for label, callback in [("＋ 添加工作流", self.app._show_workflow_upload_dialog), ("添加教程", self.app._show_workflow_tutorial), ("导入已有模型", self.app._import_models), ("重新检查", self.app._start_background_model_recheck)]:
            self.actions(root, label)[0].options["command"]()
            callback.assert_called_once_with()

    def test_merged_page_keeps_environment_and_comfyui_actions(self):
        root = Widget()
        root._scroll_canvas = mock.Mock()
        self.pages._scrollable_body = lambda page: root
        self.pages._runtime_status = lambda: ("ready", [])
        self.pages._metric = mock.Mock()
        self.pages._card = lambda parent, height=None: Widget(parent)
        self.pages._build_resources(root)
        for label, callback in [("检查环境", self.app._start_background_runtime_recheck), ("一键配置", self.app._show_quick_repair_dialog), ("打开目录", self.app._open_runtime_dir), ("修复运行环境", self.app._show_runtime_maintenance), ("一键更新 ComfyUI", self.app._start_comfyui_update)]:
            callback.reset_mock()
            self.actions(root, label)[0].options["command"]()
            callback.assert_called_once_with()
        self.assertEqual(len(self.actions(root, "配置 / 详情")), len(self.records))
        for action in self.actions(root, "修改"):
            action.options["command"]()
        self.assertEqual(self.app._choose_storage_directory.call_args_list,
                         [mock.call("models"), mock.call("workflows"), mock.call("outputs")])

    def test_missing_runtime_keeps_both_install_sources(self):
        root = Widget()
        root._scroll_canvas = mock.Mock()
        self.pages._scrollable_body = lambda page: root
        self.pages._runtime_status = lambda: ("missing", ["python/python.exe"])
        self.pages._metric = mock.Mock()
        self.pages._card = lambda parent, height=None: Widget(parent)
        self.pages._build_resources(root)
        for label, callback in [("一键修复", self.app._show_quick_repair_dialog), ("本地安装包", self.app._select_runtime), ("更多方式", self.app._show_runtime_maintenance)]:
            self.actions(root, label)[0].options["command"]()
            callback.assert_called_once_with()

    def test_runtime_deep_link_scrolls_to_the_lower_maintenance_section(self):
        card = mock.Mock()
        card.winfo_rooty.return_value = 1300
        canvas = mock.Mock()
        canvas.winfo_rooty.return_value = 100
        canvas.canvasy.return_value = 200
        canvas.bbox.return_value = (0, 0, 800, 2000)
        page = mock.Mock(_scroll_canvas=canvas)
        self.pages._pages["resources"] = page
        self.pages._resource_targets["runtime"] = card
        self.pages._set_card_outline = mock.Mock()
        self.pages.focus_runtime_maintenance()
        canvas.yview_moveto.assert_called_once_with(1364 / 2000)

    def test_state_refresh_preserves_scroll_position(self):
        page = Widget()
        old_canvas = mock.Mock()
        old_canvas.yview.return_value = (0.45, 0.65)
        page._scroll_canvas = old_canvas
        new_canvas = mock.Mock()
        new_canvas.winfo_exists.return_value = True
        self.pages._pages["resources"] = page
        self.pages._build_resources = lambda target: setattr(target, "_scroll_canvas", new_canvas)
        self.pages._runtime_status = lambda: ("ready", [])
        self.app.after_idle.side_effect = lambda callback: callback()
        self.pages.refresh({"workflows": self.records})
        new_canvas.yview_moveto.assert_called_once_with(0.45)

    def test_legacy_navigation_resolves_to_the_single_maintenance_page(self):
        app = object.__new__(GatewayApp)
        app._pages = {"overview": mock.Mock(), "resources": mock.Mock()}
        app._current_page_id = "overview"
        app._nav_buttons = {"resources": mock.Mock()}
        app._page_title_label = mock.Mock()
        app._page_subtitle_label = mock.Mock()
        GatewayApp._show_page(app, "workflows")
        self.assertEqual(app._current_page_id, "resources")
        app._pages["resources"].pack.assert_called_once_with(fill="both", expand=True)
        self.assertNotIn("workflows", StaticDashboardPages.PAGE_BUILDERS)


if __name__ == "__main__":
    unittest.main()
