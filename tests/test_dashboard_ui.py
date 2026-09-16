import json
import hashlib
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from app.config import Config
from app.gui import main_gateway
from app.gui.dashboard_pages import StaticDashboardPages
from app.gui.main_gateway import C, GatewayApp


PAGE_IDS = (
    "overview",
    "resources",
    "settings",
)


class ManualAfterScheduler:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = []
        self._next_id = 0

    def after(self, delay, callback):
        if delay <= 0:
            callback()
            return None
        self._next_id += 1
        callback_id = f"after-{self._next_id}"
        self.callbacks[callback_id] = callback
        return callback_id

    def after_cancel(self, callback_id):
        self.cancelled.append(callback_id)
        self.callbacks.pop(callback_id, None)

    def pop_next(self):
        callback_id = next(iter(self.callbacks))
        return callback_id, self.callbacks.pop(callback_id)


class FakeVariable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self, **options):
        self.options = dict(options)
        self.started = False

    def config(self, **options):
        self.options.update(options)

    configure = config

    def cget(self, name):
        return self.options.get(name)

    def start(self, _interval=None):
        self.started = True

    def stop(self):
        self.started = False


class DashboardShellTests(unittest.TestCase):
    def test_release_version_comparison_uses_numeric_semver(self):
        cases = (
            ("1.0.2", "v1.0.3", True),
            ("1.9.9", "v1.10.0", True),
            ("1.0.2", "v1.0.2", False),
            ("1.0.2", "v1.0.1", False),
            ("dev", "v1.0.3", False),
            ("1.0.2", "nightly", False),
        )
        for current, latest, expected in cases:
            with self.subTest(current=current, latest=latest):
                self.assertEqual(
                    main_gateway._is_newer_release(current, latest),
                    expected,
                )

    def test_latest_release_check_uses_the_renamed_repository(self):
        response = mock.Mock()
        response.read.return_value = json.dumps(
            {
                "tag_name": "v1.0.3",
                "draft": False,
                "prerelease": False,
                "html_url": "https://attacker.invalid/not-used",
            }
        ).encode("utf-8")
        urlopen = mock.Mock(return_value=response)

        latest = main_gateway._fetch_latest_release_version(urlopen=urlopen)

        self.assertEqual(latest, "1.0.3")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            main_gateway.PROJECT_LATEST_RELEASE_API_URL,
            "https://api.github.com/repos/Yaro-lu/LingJingAPI/releases/latest",
        )
        self.assertEqual(request.full_url, main_gateway.PROJECT_LATEST_RELEASE_API_URL)
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertIn("LingJing-Desktop/", request.get_header("User-agent"))
        self.assertLessEqual(urlopen.call_args.kwargs["timeout"], 8)
        response.close.assert_called_once_with()

    def test_copy_local_api_uses_the_full_unshortened_url(self):
        app = object.__new__(GatewayApp)
        app._local_url = "http://127.0.0.1:19001"
        app._copy = mock.Mock()

        app._copy_local_url()

        app._copy.assert_called_once_with("http://127.0.0.1:19001")

    def test_release_worker_only_applies_updates_through_tk_after(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = mock.Mock()
        app._apply_available_release = mock.Mock()

        with mock.patch.object(
            main_gateway,
            "_fetch_latest_release_version",
            return_value="1.0.4",
        ):
            app._release_update_worker()

        app._apply_available_release.assert_not_called()
        callback = app.after.call_args.args[1]
        callback()
        app._apply_available_release.assert_called_once_with("1.0.4")

    def test_release_worker_is_silent_without_a_new_stable_version(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = mock.Mock()
        app._apply_available_release = mock.Mock()

        for latest in ("", "1.0.1", main_gateway.APP_VERSION):
            with (
                self.subTest(latest=latest),
                mock.patch.object(
                    main_gateway,
                    "_fetch_latest_release_version",
                    return_value=latest,
                ),
            ):
                app._release_update_worker()

        app.after.assert_not_called()
        app._apply_available_release.assert_not_called()

    def test_open_output_rejects_absolute_and_traversal_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "client"
            outside = root / "outside"
            outside.mkdir()
            (outside / "result.png").write_bytes(b"outside")
            app = object.__new__(GatewayApp)

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway.os, "startfile") as startfile,
            ):
                app._open_outputs_for_items(
                    [
                        {
                            "filename": "result.png",
                            "subfolder": str(outside),
                            "task_id": "..\\outside",
                        }
                    ]
                )

            startfile.assert_called_once_with(str(base / "outputs"))

    def test_open_output_accepts_a_real_file_below_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            expected = base / "outputs" / "任务结果" / "result.png"
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"image")
            app = object.__new__(GatewayApp)

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway.os, "startfile") as startfile,
            ):
                app._open_outputs_for_items(
                    [{"filename": "result.png", "subfolder": "任务结果"}]
                )

            startfile.assert_called_once_with(str(expected.resolve()))

    @staticmethod
    def _find_by_text(root, text):
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                if str(widget.cget("text")) == text:
                    return widget
            except Exception:
                pass
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                pass
        return None

    @staticmethod
    def _all_text(root):
        values = []
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                value = str(widget.cget("text"))
                if value:
                    values.append(value)
            except Exception:
                pass
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                pass
        return values

    @staticmethod
    def _walk_widgets(root):
        yield root
        try:
            children = root.winfo_children()
        except Exception:
            children = []
        for child in children:
            yield from DashboardShellTests._walk_widgets(child)

    @staticmethod
    def _fake_runtime_dialog():
        popup = mock.Mock()
        popup.winfo_exists.return_value = True
        return {
            "popup": popup,
            "progress": FakeWidget(mode="determinate"),
            "progress_var": FakeVariable(0),
            "stage_var": FakeVariable(""),
            "stage_label": FakeWidget(fg=C["text2"]),
            "detail_var": FakeVariable(""),
            "detail_label": FakeWidget(fg=C["muted"]),
        }

    def test_dashboard_shell_builds_and_switches_all_pages(self):
        """The static shell must not require backend threads to render."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()

                self.assertEqual(app.title(), "灵境造片厂")
                self.assertIsNotNone(self._find_by_text(app._sidebar, "灵境造片厂"))
                self.assertEqual(tuple(app._pages), PAGE_IDS)
                self.assertEqual(tuple(app._nav_buttons), PAGE_IDS)
                self.assertGreaterEqual(
                    len(app._wf_rows),
                    6,
                    "Bundled workflows must remain visible before the API starts",
                )
                overview_text = set(self._all_text(app._wf_frame))
                for label in ("文/图生图", "文生图", "首尾帧", "文本模型"):
                    self.assertTrue(
                        any(text.endswith(label) for text in overview_text),
                        label,
                    )
                self.assertTrue(
                    any(text.startswith("缺少 ") and text.endswith(" 个模型") for text in overview_text)
                )
                self.assertEqual(
                    tuple(app._light_groups),
                    ("server", "env", "comfyui", "tunnel", "api"),
                    "The approved runtime-status card must keep its component order",
                )

                info_texts = set(self._all_text(app._info_frame))
                self.assertTrue({"公网 URL", "本地 API", "API Key"} <= info_texts)
                self.assertEqual(app._local_url_label.cget("text"), main_gateway.API_BASE)

                app._show_page("overview")
                app.geometry("1090x700")
                app.update()
                stack_top = app._url_stack.winfo_rooty()
                stack_bottom = stack_top + app._url_stack.winfo_height()
                key_top = app._api_key_card.winfo_rooty()
                key_bottom = key_top + app._api_key_card.winfo_height()
                self.assertLessEqual(abs(stack_top - key_top), 1)
                self.assertLessEqual(abs(stack_bottom - key_bottom), 1)
                public_top = app._public_url_card.winfo_rooty()
                public_bottom = public_top + app._public_url_card.winfo_height()
                local_top = app._local_url_card.winfo_rooty()
                local_bottom = local_top + app._local_url_card.winfo_height()
                self.assertLessEqual(abs(public_top - stack_top), 1)
                self.assertLessEqual(abs(local_bottom - stack_bottom), 1)
                self.assertGreater(local_top, public_bottom)
                self.assertLessEqual(
                    abs(
                        app._public_url_card.winfo_height()
                        + (local_top - public_bottom)
                        + app._local_url_card.winfo_height()
                        - app._api_key_card.winfo_height()
                    ),
                    1,
                )
                self.assertLess(
                    app._public_url_card.winfo_height(),
                    app._api_key_card.winfo_height(),
                )
                self.assertGreaterEqual(
                    app._local_url_card.winfo_rooty(),
                    app._public_url_card.winfo_rooty()
                    + app._public_url_card.winfo_height(),
                )

                for page_id in PAGE_IDS:
                    app._show_page(page_id)
                    app.update_idletasks()
                    self.assertEqual(app._current_page_id, page_id)
                    self.assertTrue(app._pages[page_id].winfo_ismapped())

                app.geometry("1090x700")
                app.update()
                self.assertGreaterEqual(app._page_host.winfo_width(), 870)
                self.assertGreaterEqual(app._page_host.winfo_height(), 560)
                for page in app._pages.values():
                    self.assertLessEqual(page.winfo_reqwidth(), app._page_host.winfo_width())
                    self.assertLessEqual(page.winfo_reqheight(), app._page_host.winfo_height())

                app._show_page("resources")
                app.update()
                open_buttons = []
                for widget in self._walk_widgets(app._pages["resources"]):
                    try:
                        if str(widget.cget("text")) == "打开" and callable(getattr(widget, "invoke", None)):
                            open_buttons.append(widget)
                    except Exception:
                        pass
                self.assertEqual(len(open_buttons), 3)
                self.assertTrue(all(button.winfo_width() > 1 for button in open_buttons))
                page_left = app._pages["resources"].winfo_rootx()
                page_right = page_left + app._pages["resources"].winfo_width()
                self.assertTrue(
                    all(
                        page_left <= button.winfo_rootx()
                        and button.winfo_rootx() + button.winfo_width() <= page_right
                        for button in open_buttons
                    )
                )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_console_runtime_action_opens_environment_maintenance(self):
        """The approved console card deep-links without starting an install."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                install = self._find_by_text(app._overview_page, "安装运行环境")
                self.assertIsNotNone(install)
                app._install_runtime_from_mirror = mock.Mock()

                install.invoke()
                app.update_idletasks()

                self.assertEqual(app._current_page_id, "resources")
                self.assertTrue(app._pages["resources"].winfo_ismapped())
                app._install_runtime_from_mirror.assert_not_called()
                card = app._dashboard_pages._resource_targets.get("runtime")
                self.assertIsNotNone(card)
                if hasattr(card, "_outline"):
                    self.assertEqual(card._outline, C["primary"])
                else:
                    self.assertEqual(card.cget("border_color"), C["primary"])
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_resources_page_exposes_environment_and_model_maintenance(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app._show_page("resources")
                app.update_idletasks()
                texts = set(self._all_text(app._pages["resources"]))

                self.assertIn("运行环境维护", texts)
                self.assertTrue({"检查环境", "一键修复"} & texts)
                self.assertTrue({"修复 / 更新", "本地安装包"} & texts)
                self.assertIn("工作流与模型", texts)
                self.assertIn("导入已有模型", texts)
                self.assertIn("重新检查", texts)
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_resources_page_exposes_update_and_settings_has_no_copy_key(self):
        update_comfyui = mock.Mock()
        repair_runtime = mock.Mock()
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
            mock.patch.object(
                GatewayApp,
                "_start_comfyui_update",
                update_comfyui,
            ),
            mock.patch.object(
                GatewayApp,
                "_show_runtime_maintenance",
                repair_runtime,
            ),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app._show_page("settings")
                app.update_idletasks()
                settings = app._pages["settings"]
                settings_texts = set(self._all_text(settings))
                self.assertNotIn("复制密钥", settings_texts)
                self.assertNotIn("组件更新", settings_texts)
                app._show_page("resources")
                app.update_idletasks()
                resources = app._pages["resources"]
                texts = set(self._all_text(resources))

                self.assertIn("组件更新", texts)
                self.assertIn("一键更新 ComfyUI", texts)
                self.assertIn("修复运行环境", texts)
                self.assertIn(
                    "只更新 ComfyUI 核心，不影响模型、自定义节点和用户数据。",
                    texts,
                )
                settings_buttons = [
                    widget
                    for widget in self._walk_widgets(settings)
                    if callable(getattr(widget, "invoke", None))
                    and str(widget.cget("text")) == "设置"
                ]
                self.assertEqual(len(settings_buttons), 4)

                self._find_by_text(resources, "一键更新 ComfyUI").invoke()
                self._find_by_text(resources, "修复运行环境").invoke()
                update_comfyui.assert_called_once_with()
                repair_runtime.assert_called_once_with()
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_available_release_shows_green_dot_and_fixed_release_target(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                self.assertFalse(app._release_update_dot.winfo_ismapped())

                app._apply_available_release("1.0.4")
                app.update_idletasks()

                self.assertTrue(app._release_update_dot.winfo_ismapped())
                self.assertEqual(
                    app._version_label.cget("text"),
                    f"Desktop  {main_gateway.APP_VERSION}",
                )
                self.assertEqual(
                    app._release_update_dot.itemcget(
                        app._release_update_dot_item,
                        "fill",
                    ),
                    C["success"],
                )
                self.assertIn("1.0.4", app._release_update_message())
                self.assertTrue(app._version_row.bind("<Enter>"))
                self.assertTrue(app._version_row.bind("<Leave>"))
                self.assertTrue(app._version_row.bind("<Button-1>"))
                with mock.patch.object(main_gateway.webbrowser, "open") as open_browser:
                    app._open_latest_release()
                self.assertEqual(
                    main_gateway.PROJECT_RELEASES_URL,
                    "https://github.com/Yaro-lu/LingJingAPI/releases/latest",
                )
                open_browser.assert_called_once_with(main_gateway.PROJECT_RELEASES_URL)
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_storage_directory_picker_saves_mapping_for_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "客户端"
            target = Path(tmp) / "外部模型"
            target.mkdir(parents=True)
            app = object.__new__(GatewayApp)
            app._footer_label = mock.Mock()
            app._dashboard_pages = mock.Mock()

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(
                    main_gateway.filedialog,
                    "askdirectory",
                    return_value=str(target),
                ),
                mock.patch.object(main_gateway.messagebox, "showinfo") as showinfo,
            ):
                app._choose_storage_directory("models")

            loaded = Config(base)
            self.assertEqual(loaded.models_dir, target.resolve())
            app._dashboard_pages.update_storage_path.assert_called_once_with(
                "models",
                target.resolve(),
            )
            self.assertIn("重新打开灵境造片厂后生效", showinfo.call_args.args[1])

    def test_comfyui_model_path_file_uses_the_configured_chinese_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "客户端"
            target = Path(tmp) / "外部资源" / "模型"
            target.mkdir(parents=True)
            Config(base).set_directory_mapping("models", target)

            with mock.patch.object(main_gateway, "BASE_DIR", base):
                main_gateway._ensure_extra_model_paths()

            content = (
                base / "runtime" / "ComfyUI" / "extra_model_paths.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn(target.resolve().as_posix(), content)

    def test_overview_workflow_catalog_has_a_reachable_vertical_scrollbar(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.geometry("1090x700")
                app._last_health = {
                    "workflows": [
                        {
                            "id": f"scroll_fixture_{index}",
                            "name": f"滚动测试工作流 {index}",
                            "enabled": True,
                            "available": True,
                            "output_type": "video",
                            "dependency_status": "ready",
                            "nodes_verified": True,
                        }
                        for index in range(18)
                    ]
                }
                app._update_workflow_display(app._last_health)
                app.update()

                canvas = app._workflow_canvas
                scrollbar = app._workflow_scrollbar
                bbox = canvas.bbox("all")
                self.assertTrue(scrollbar.winfo_ismapped())
                self.assertIsInstance(scrollbar, main_gateway.SlimRoundedScrollbar)
                self.assertLessEqual(int(scrollbar.cget("width")), 6)
                self.assertEqual(scrollbar.bar_width, 4)
                self.assertTrue(scrollbar.find_withtag("thumb"))
                self.assertTrue(str(canvas.cget("yscrollcommand")))
                self.assertIsNotNone(bbox)
                self.assertGreater(bbox[3] - bbox[1], canvas.winfo_height())

                before = canvas.yview()
                result = app._on_workflow_mousewheel(
                    mock.Mock(delta=-120, num=None)
                )
                app.update_idletasks()
                self.assertEqual(result, "break")
                self.assertGreater(canvas.yview()[0], before[0])

                canvas.yview_moveto(0)
                row_child = app._workflow_scroll_content.winfo_children()[0]
                self.assertTrue(str(row_child.bind("<MouseWheel>")))
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_user_facing_font_tokens_and_footer_remain_legible(self):
        for key in ("normal", "small", "tiny", "mono", "url"):
            self.assertGreaterEqual(
                abs(int(main_gateway.F[key][1])),
                9,
                f"{key} must not render below 9 pt",
            )
        self.assertGreaterEqual(main_gateway.LAYOUT["footer_h"], 30)

        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update_idletasks()
                footer_font = tkfont.Font(font=app._footer_label.cget("font"))
                loading_font = tkfont.Font(font=app._loading_text.cget("font"))
                self.assertIn("YaHei", str(footer_font.actual("family")))
                self.assertGreaterEqual(abs(int(footer_font.actual("size"))), 9)
                self.assertGreaterEqual(abs(int(loading_font.actual("size"))), 9)
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_scrollable_resource_and_settings_pages_use_brand_scrollbars_and_wheel(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.geometry("1090x700")
                for page_id, nested_text in (
                    ("resources", "工作流与模型"),
                    ("settings", "文件位置"),
                ):
                    app._show_page(page_id)
                    app.update()
                    page = app._pages[page_id]
                    widgets = list(self._walk_widgets(page))
                    self.assertFalse(
                        any(isinstance(widget, tk.Scrollbar) for widget in widgets),
                        f"{page_id} must not use the system-grey scrollbar",
                    )
                    scrollbars = [
                        widget
                        for widget in widgets
                        if isinstance(widget, main_gateway.SlimRoundedScrollbar)
                    ]
                    self.assertEqual(len(scrollbars), 1)
                    self.assertEqual(scrollbars[0].bar_width, 4)
                    canvas = next(
                        widget
                        for widget in widgets
                        if isinstance(widget, tk.Canvas)
                        and not isinstance(widget, main_gateway.SlimRoundedScrollbar)
                        and str(widget.cget("yscrollcommand"))
                    )
                    canvas.yview_moveto(0)
                    app.update_idletasks()
                    nested = self._find_by_text(page, nested_text)
                    self.assertIsNotNone(nested)
                    self.assertTrue(str(nested.bind("<MouseWheel>")))
                    self.assertTrue(str(nested.bind("<Button-4>")))
                    self.assertTrue(str(nested.bind("<Button-5>")))
                    before = canvas.yview()
                    nested.event_generate("<MouseWheel>", delta=-120)
                    app.update_idletasks()
                    self.assertGreater(
                        canvas.yview()[0],
                        before[0],
                        f"{page_id}: bbox={canvas.bbox('all')} viewport={canvas.winfo_height()} binding={nested.bind('<MouseWheel>')}",
                    )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_download_action_starts_automatic_pull(self):
        """One-click repair must try the configured package URL first."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
            mock.patch.object(main_gateway, "_runtime_has_package_files", return_value=False),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app._download_runtime = mock.Mock()
                app._runtime_mirror_url = mock.Mock(
                    return_value="https://github.com/Yaro-lu/LingJingAPI/releases/download/v1/runtime.7z"
                )

                app._install_runtime_from_mirror()
                app.update_idletasks()

                app._download_runtime.assert_called_once_with(
                    "https://github.com/Yaro-lu/LingJingAPI/releases/download/v1/runtime.7z",
                    repair_confirmed=False,
                )
                popups = [
                    widget
                    for widget in app.winfo_children()
                    if isinstance(widget, tk.Toplevel)
                    and widget.title() == "自动修复失败"
                ]
                self.assertEqual(popups, [])
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_progress_dialog_uses_neutral_progress_ui(self):
        """Normal installation must look like progress, never like an error log."""
        dialog = None
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                dialog = app._create_runtime_progress_dialog(
                    title="安装运行环境",
                    heading="正在安装运行环境",
                    stage="准备安装",
                    detail="正在读取环境包",
                )
                app.update_idletasks()

                progress = dialog["progress"]
                self.assertIsInstance(progress, ttk.Progressbar)
                self.assertEqual(str(progress.cget("mode")), "determinate")
                self.assertEqual(float(progress.cget("maximum")), 100.0)
                self.assertNotIn(
                    dialog["stage_label"].cget("fg"),
                    {C["warn"], C["error"]},
                )
                self.assertNotIn(
                    dialog["detail_label"].cget("fg"),
                    {C["warn"], C["error"]},
                )
                self.assertFalse(
                    any(
                        isinstance(widget, tk.Text)
                        for widget in self._walk_widgets(dialog["popup"])
                    )
                )

                app._set_runtime_progress(
                    dialog,
                    45,
                    "安装核心模块",
                    "Python · ComfyUI · Torch/CUDA · 网络组件",
                )
                self.assertEqual(dialog["progress_var"].get(), 45)
                self.assertEqual(dialog["stage_var"].get(), "安装核心模块")
                self.assertIn("ComfyUI", dialog["detail_var"].get())
                self.assertNotIn(
                    dialog["stage_label"].cget("fg"),
                    {C["warn"], C["error"]},
                )
                self.assertNotIn(
                    dialog["detail_label"].cget("fg"),
                    {C["warn"], C["error"]},
                )

                app._set_runtime_progress(
                    dialog,
                    45,
                    "安装未完成",
                    "环境包校验失败",
                    error=True,
                )
                self.assertEqual(dialog["stage_var"].get(), "安装未完成")
                self.assertEqual(dialog["stage_label"].cget("fg"), C["error"])

                app._set_runtime_progress(
                    dialog,
                    75,
                    "验证运行模块",
                    "正在检查已安装组件",
                )
                self.assertNotEqual(dialog["stage_label"].cget("fg"), C["error"])
                self.assertNotEqual(dialog["detail_label"].cget("fg"), C["error"])
            finally:
                try:
                    if dialog:
                        dialog["popup"].destroy()
                except Exception:
                    pass
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_install_stage_catalog_names_user_facing_modules(self):
        stages = main_gateway.RUNTIME_INSTALL_STAGES
        self.assertEqual(tuple(stages), (
            "prepare",
            "verify",
            "inspect",
            "extract",
            "validate",
            "stop_services",
            "apply",
        ))
        self.assertIn("Python", stages["extract"][2])
        self.assertIn("ComfyUI", stages["extract"][2])
        self.assertIn("Torch/CUDA", stages["extract"][2])
        self.assertIn("网络组件", stages["extract"][2])
        percentages = [stage[0] for stage in stages.values()]
        self.assertTrue(all(0 <= percent <= 100 for percent in percentages))
        self.assertTrue(all(
            earlier < later
            for earlier, later in zip(percentages, percentages[1:])
        ))

    def test_runtime_extract_activity_updates_elapsed_and_reschedules(self):
        app = object.__new__(GatewayApp)
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.side_effect = [False, True, True]
        dialog = self._fake_runtime_dialog()
        clock = {"value": 0.0}

        with mock.patch.object(
            main_gateway.time,
            "monotonic",
            side_effect=lambda: clock["value"],
        ):
            app._set_runtime_install_stage(dialog, "extract")
            self.assertTrue(dialog["progress"].started)
            self.assertEqual(dialog["progress"].cget("mode"), "indeterminate")
            self.assertIn("正在启动解压进程", dialog["detail_var"].get())

            clock["value"] = 1.0
            _, first_tick = scheduler.pop_next()
            first_tick()
            self.assertIn("解压进程运行中", dialog["detail_var"].get())
            self.assertIn("00:01", dialog["detail_var"].get())

            clock["value"] = 2.0
            _, second_tick = scheduler.pop_next()
            second_tick()
            self.assertIn("00:02", dialog["detail_var"].get())
            self.assertEqual(len(scheduler.callbacks), 1)

    def test_runtime_verify_activity_updates_elapsed_and_reschedules(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        dialog = self._fake_runtime_dialog()
        clock = {"value": 0.0}

        with mock.patch.object(
            main_gateway.time,
            "monotonic",
            side_effect=lambda: clock["value"],
        ):
            app._set_runtime_install_stage(dialog, "verify")
            self.assertTrue(dialog["progress"].started)
            self.assertEqual(dialog["progress"].cget("mode"), "indeterminate")
            self.assertIn("正在校验大文件", dialog["detail_var"].get())

            clock["value"] = 1.0
            _, first_tick = scheduler.pop_next()
            first_tick()
            self.assertIn("00:01", dialog["detail_var"].get())
            self.assertEqual(len(scheduler.callbacks), 1)

    def test_runtime_extract_progress_switches_to_real_percentage(self):
        app = object.__new__(GatewayApp)
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app._process_supervisor = mock.Mock()
        dialog = self._fake_runtime_dialog()

        with mock.patch.object(main_gateway.time, "monotonic", return_value=0.0):
            app._set_runtime_install_stage(dialog, "extract")
            dialog["_activity_progress_queue"].put(50)
            _, progress_tick = scheduler.pop_next()
            progress_tick()

        self.assertFalse(dialog["progress"].started)
        self.assertEqual(dialog["progress"].cget("mode"), "determinate")
        self.assertEqual(dialog["progress_var"].get(), 60)
        self.assertIn("解压进度 50%", dialog["detail_var"].get())
        self.assertIn("00:00", dialog["detail_var"].get())

    def test_runtime_activity_stale_tick_cannot_overwrite_next_stage(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app._process_supervisor = mock.Mock()
        dialog = self._fake_runtime_dialog()

        with mock.patch.object(main_gateway.time, "monotonic", return_value=0.0):
            app._set_runtime_install_stage(dialog, "extract")
            callback_id, stale_tick = next(iter(scheduler.callbacks.items()))
            app._set_runtime_install_stage(dialog, "validate")

            self.assertIn(callback_id, scheduler.cancelled)
            self.assertEqual(len(scheduler.callbacks), 1)
            validate_callbacks = dict(scheduler.callbacks)
            self.assertEqual(dialog["stage_var"].get(), "验证运行模块")
            expected_detail = dialog["detail_var"].get()

            stale_tick()
            self.assertEqual(dialog["stage_var"].get(), "验证运行模块")
            self.assertEqual(dialog["detail_var"].get(), expected_detail)
            self.assertEqual(scheduler.callbacks, validate_callbacks)

    def test_runtime_activity_stops_when_popup_is_destroyed(self):
        app = object.__new__(GatewayApp)
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app._process_supervisor = mock.Mock()
        dialog = self._fake_runtime_dialog()

        with mock.patch.object(main_gateway.time, "monotonic", return_value=0.0):
            activity_token = app._set_runtime_install_stage(dialog, "extract")
            _, stale_tick = next(iter(scheduler.callbacks.items()))
            dialog["popup"].winfo_exists.return_value = False
            app._report_runtime_extract_progress(dialog, activity_token, 55)
            stale_tick()

        self.assertIsNone(dialog.get("_activity_token"))
        self.assertEqual(scheduler.callbacks, {})

    def test_runtime_failure_stops_activity_and_stays_red(self):
        app = object.__new__(GatewayApp)
        scheduler = ManualAfterScheduler()
        app.after = scheduler.after
        app.after_cancel = scheduler.after_cancel
        app._process_supervisor = mock.Mock()
        dialog = self._fake_runtime_dialog()

        with mock.patch.object(main_gateway.time, "monotonic", return_value=0.0):
            activity_token = app._set_runtime_install_stage(dialog, "extract")
            callback_id, stale_tick = next(iter(scheduler.callbacks.items()))
            app._set_runtime_progress(
                dialog,
                45,
                "安装未完成",
                "环境包解压失败",
                error=True,
            )

            self.assertIn(callback_id, scheduler.cancelled)
            self.assertFalse(dialog["progress"].started)
            self.assertEqual(dialog["progress"].cget("mode"), "determinate")
            self.assertEqual(dialog["stage_var"].get(), "安装未完成")
            self.assertEqual(dialog["stage_label"].cget("fg"), C["error"])

            stale_tick()
            app._report_runtime_extract_progress(dialog, activity_token, 88)
            self.assertEqual(dialog["stage_var"].get(), "安装未完成")
            self.assertEqual(dialog["detail_var"].get(), "环境包解压失败")
            self.assertEqual(dialog["stage_label"].cget("fg"), C["error"])
            self.assertEqual(dialog["progress_var"].get(), 45)
            self.assertEqual(scheduler.callbacks, {})

    def test_runtime_install_reports_each_module_stage_in_order(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = lambda _delay, callback: callback()
        app._create_runtime_progress_dialog = mock.Mock(
            return_value={
                "popup": mock.MagicMock(),
                "progress_var": mock.MagicMock(),
                "stage_var": mock.MagicMock(),
            }
        )
        app._set_runtime_progress = mock.Mock()
        app._set_runtime_install_stage = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.run.side_effect = [
            mock.Mock(returncode=0, stdout="members", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        app._begin_runtime_maintenance = mock.Mock(return_value=(True, set(), ""))
        app._end_runtime_maintenance = mock.Mock()
        app._exit_for_runtime_update = mock.Mock()
        app._show_runtime_manual_restart_notice = mock.Mock(return_value=True)

        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "runtime.7z"
            package.write_bytes(b"runtime")
            with (
                mock.patch.object(main_gateway, "BASE_DIR", Path(tmp)),
                mock.patch.object(main_gateway, "_runtime_has_package_files", return_value=False),
                mock.patch.object(main_gateway, "verify_runtime_package", return_value=(True, "a", "a")),
                mock.patch.object(
                    main_gateway,
                    "find_extractor",
                    return_value=("tar", "tar.exe"),
                ),
                mock.patch.object(main_gateway, "archive_list_command", return_value=["list"]),
                mock.patch.object(main_gateway, "archive_extract_command", return_value=["extract"]),
                mock.patch.object(main_gateway, "parse_archive_members", return_value=[]),
                mock.patch.object(main_gateway, "missing_archive_entries", return_value=[]),
                mock.patch.object(main_gateway, "invalid_archive_entries", return_value=[]),
                mock.patch.object(main_gateway, "validate_staged_runtime"),
                mock.patch.object(main_gateway, "launch_runtime_update", return_value=mock.Mock()),
                mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
            ):
                app._extract_runtime(package)

        stages = [
            call.args[1]
            for call in app._set_runtime_install_stage.call_args_list
        ]
        self.assertEqual(stages, list(main_gateway.RUNTIME_INSTALL_STAGES))
        app._show_runtime_manual_restart_notice.assert_called_once()
        app._exit_for_runtime_update.assert_called_once()

    def test_runtime_install_tells_user_to_restart_manually(self):
        _progress, stage, detail = main_gateway.RUNTIME_INSTALL_STAGES["apply"]

        self.assertIn("应用运行环境", stage)
        self.assertIn("手动", detail)
        self.assertIn("重启后生效", detail)

    def test_runtime_repair_confirmations_never_promise_automatic_restart(self):
        source = Path(main_gateway.__file__).read_text(encoding="utf-8")

        self.assertNotIn(
            "安装过程中会暂时停止 ComfyUI、API 和公网连接，完成后自动重新启动",
            source,
        )
        self.assertGreaterEqual(source.count("请手动重新打开，重启后生效"), 2)

    def test_runtime_install_uses_branded_manual_restart_notice(self):
        source = Path(main_gateway.__file__).read_text(encoding="utf-8")
        start = source.index("    def _show_manual_restart_notice")
        end = source.index("    def _show_runtime_manual_restart_notice", start)
        notice_source = source[start:end]

        self.assertNotIn("messagebox", notice_source)
        self.assertIn("程序不会自动重新打开", notice_source)
        self.assertIn("手动打开客户端", notice_source)
        self.assertIn("退出并完成", notice_source)
        self.assertIn("WM_DELETE_WINDOW", notice_source)

    def test_manual_restart_notice_gives_immediate_visible_feedback(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = lambda _delay, callback: callback()
        app._restore_maintenance_dialog = mock.Mock()
        app._center_popup = mock.Mock()
        notice = mock.MagicMock()
        notice.winfo_reqwidth.return_value = 480
        notice.winfo_reqheight.return_value = 320
        popup = mock.MagicMock()
        content = mock.MagicMock()
        dialog = {
            "popup": popup,
            "panel": mock.MagicMock(),
            "content": content,
        }
        action_button = mock.MagicMock()
        status_label = mock.MagicMock()

        def build_button(_parent, _text, command, _style, **_kwargs):
            action_button.pack.side_effect = lambda **_pack_kwargs: command()
            return action_button

        app._button = build_button
        labels = [mock.MagicMock(), mock.MagicMock(), status_label]
        with (
            mock.patch.object(main_gateway.tk, "Frame", return_value=notice),
            mock.patch.object(main_gateway.tk, "Label", side_effect=labels),
        ):
            accepted = app._show_manual_restart_notice(dialog, component="运行环境")

        self.assertTrue(accepted)
        action_button.config.assert_called_once_with(
            state="disabled",
            text="正在退出并完成安装…",
        )
        status_label.config.assert_called_once_with(
            text="正在关闭后台服务并启动安装助手，请稍候…",
            fg=C["primary"],
        )
        self.assertGreaterEqual(popup.update_idletasks.call_count, 2)
        app._center_popup.assert_called_once_with(popup, 572, 404)
        popup.grab_current.return_value.grab_release.assert_called_once_with()
        popup.resizable.assert_called_once_with(True, True)

    def test_restore_maintenance_dialog_reveals_progress_after_handoff_error(self):
        app = object.__new__(GatewayApp)
        popup = mock.MagicMock()
        popup.winfo_exists.return_value = True
        content = mock.MagicMock()
        notice = mock.MagicMock()
        notice.winfo_exists.return_value = True
        dialog = {
            "background_card": None,
            "popup": popup,
            "content": content,
            "restart_notice": notice,
        }

        app._restore_maintenance_dialog(dialog)

        notice.destroy.assert_called_once_with()
        content.pack.assert_called_once_with(
            fill="both",
            expand=True,
            padx=24,
            pady=20,
        )
        self.assertIsNone(dialog["restart_notice"])

    def test_runtime_helper_launch_failure_restores_progress_and_services(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._dashboard_pages = mock.Mock()
        app._last_health = {}
        app.after = lambda _delay, callback: callback()
        popup = mock.MagicMock()
        popup.winfo_exists.return_value = True
        content = mock.MagicMock()
        notice = mock.MagicMock()
        notice.winfo_exists.return_value = True
        dialog = {
            "popup": popup,
            "panel": mock.MagicMock(),
            "content": content,
            "progress_var": FakeVariable(82),
            "stage_var": FakeVariable(),
            "stage_label": FakeWidget(),
            "detail_var": FakeVariable(),
            "detail_label": FakeWidget(),
            "background_card": None,
        }
        app._create_runtime_progress_dialog = mock.Mock(return_value=dialog)
        app._set_runtime_install_stage = mock.Mock()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.run.side_effect = [
            mock.Mock(returncode=0, stdout="members", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        app._process_supervisor.is_running.return_value = False
        app._begin_runtime_maintenance = mock.Mock(
            return_value=(True, {"api", "comfyui"}, "")
        )
        app._end_runtime_maintenance = mock.Mock()
        app._exit_for_runtime_update = mock.Mock()

        def accept_notice(_dialog):
            dialog["restart_notice"] = notice
            return True

        app._show_runtime_manual_restart_notice = accept_notice

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / "runtime.7z"
            package.write_bytes(b"runtime")
            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway, "_runtime_has_package_files", return_value=False),
                mock.patch.object(main_gateway, "_check_runtime_exists", return_value=True),
                mock.patch.object(main_gateway, "verify_runtime_package", return_value=(True, "a", "a")),
                mock.patch.object(main_gateway, "find_extractor", return_value=("tar", "tar.exe")),
                mock.patch.object(main_gateway, "archive_list_command", return_value=["list"]),
                mock.patch.object(main_gateway, "archive_extract_command", return_value=["extract"]),
                mock.patch.object(main_gateway, "parse_archive_members", return_value=[]),
                mock.patch.object(main_gateway, "missing_archive_entries", return_value=[]),
                mock.patch.object(main_gateway, "invalid_archive_entries", return_value=[]),
                mock.patch.object(main_gateway, "validate_staged_runtime"),
                mock.patch.object(
                    main_gateway,
                    "launch_runtime_update",
                    side_effect=RuntimeError("后台安装助手启动失败"),
                ),
                mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
            ):
                app._extract_runtime(package)

        app._end_runtime_maintenance.assert_called_once_with(restart=True)
        app._exit_for_runtime_update.assert_not_called()
        notice.destroy.assert_called_once_with()
        content.pack.assert_called_once_with(
            fill="both",
            expand=True,
            padx=24,
            pady=20,
        )
        self.assertEqual(dialog["stage_var"].get(), "安装未完成")
        self.assertIn("后台安装助手启动失败", dialog["detail_var"].get())
        popup.protocol.assert_called()

    def test_runtime_install_preserves_staging_when_extractor_cannot_stop(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._dashboard_pages = mock.Mock()
        app._last_health = {}
        app.after = lambda _delay, callback: callback()
        app._create_runtime_progress_dialog = mock.Mock(
            return_value={
                "popup": mock.MagicMock(),
                "progress_var": mock.MagicMock(),
                "stage_var": mock.MagicMock(),
            }
        )
        app._set_runtime_progress = mock.Mock()
        app._set_runtime_install_stage = mock.Mock()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.run.return_value = mock.Mock(
            returncode=0,
            stdout="members",
            stderr="",
        )
        app._process_supervisor.run_observed.side_effect = RuntimeError(
            "后台进程超时后无法安全停止"
        )
        app._process_supervisor.is_running.side_effect = (
            lambda role: role == "runtime-install"
        )
        app._end_runtime_maintenance = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / "runtime.7z"
            package.write_bytes(b"runtime")
            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway, "_runtime_has_package_files", return_value=False),
                mock.patch.object(main_gateway, "_check_runtime_exists", return_value=False),
                mock.patch.object(main_gateway, "verify_runtime_package", return_value=(True, "a", "a")),
                mock.patch.object(main_gateway, "find_extractor", return_value=("7z", "7z.exe")),
                mock.patch.object(main_gateway, "archive_list_command", return_value=["list"]),
                mock.patch.object(main_gateway, "archive_extract_command", return_value=["extract"]),
                mock.patch.object(main_gateway, "parse_archive_members", return_value=[]),
                mock.patch.object(main_gateway, "missing_archive_entries", return_value=[]),
                mock.patch.object(main_gateway, "invalid_archive_entries", return_value=[]),
                mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
            ):
                app._extract_runtime(package)

            staging = list(base.glob(".runtime-install-staging-*"))
            self.assertEqual(len(staging), 1)
            self.assertTrue(staging[0].is_dir())
            list_call = app._process_supervisor.run.call_args
            self.assertEqual(list_call.kwargs["encoding"], "utf-8")
            self.assertEqual(list_call.kwargs["errors"], "strict")
            error_calls = [
                call
                for call in app._set_runtime_progress.call_args_list
                if call.kwargs.get("error") is True
            ]
            self.assertEqual(len(error_calls), 1)
            self.assertIn("无法安全停止", error_calls[0].args[3])

    def test_runtime_download_failure_opens_copyable_github_dialog(self):
        """The manual GitHub fallback appears only after automatic pull failure."""
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)

                app._show_runtime_download_fallback("网络连接失败")
                app.update_idletasks()

                popups = [
                    widget
                    for widget in app.winfo_children()
                    if isinstance(widget, tk.Toplevel)
                    and widget.title() == "自动修复失败"
                ]
                self.assertEqual(len(popups), 1)
                popup = popups[0]
                widgets = list(self._walk_widgets(popup))
                url_fields = [
                    widget
                    for widget in widgets
                    if isinstance(widget, tk.Entry)
                    and widget.get() == "https://github.com/Yaro-lu/LingJingAPI"
                ]
                self.assertEqual(len(url_fields), 1)

                copy_button = self._find_by_text(popup, "复制地址")
                self.assertIsNotNone(copy_button)
                copy_button.invoke()
                app.update()
                self.assertEqual(app.clipboard_get(), "https://github.com/Yaro-lu/LingJingAPI")
                self.assertIsNotNone(self._find_by_text(popup, "自动拉取失败，请手动下载"))
                self.assertTrue(
                    any("网络连接失败" in text for text in self._all_text(popup))
                )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_network_error_switches_to_manual_fallback(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app._show_runtime_download_fallback = mock.Mock()

                with (
                    mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
                    mock.patch.object(
                        main_gateway,
                        "_open_download_request",
                        side_effect=OSError("network unavailable"),
                    ),
                ):
                    app._download_runtime("https://example.invalid/runtime.7z")
                app.update()

                app._show_runtime_download_fallback.assert_called_once()
                self.assertIn(
                    "network unavailable",
                    app._show_runtime_download_fallback.call_args.args[0],
                )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_download_reuses_a_verified_cached_package(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                base = Path(tmp)
                target = base / "cache" / main_gateway.RUNTIME_PACKAGE_NAME
                target.parent.mkdir(parents=True)
                target.write_bytes(b"verified cached runtime")
                app._extract_runtime = mock.Mock()
                app.after = lambda _delay, callback, *args: callback(*args)

                with (
                    mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
                    mock.patch.object(main_gateway, "BASE_DIR", base),
                    mock.patch.object(
                        main_gateway,
                        "verify_runtime_package",
                        return_value=(True, "expected", "actual"),
                    ) as verify,
                    mock.patch.object(main_gateway, "_open_download_request") as urlopen,
                ):
                    app._download_runtime("https://example.invalid/runtime.7z")

                verify.assert_called_once_with(target, None)
                urlopen.assert_not_called()
                app._extract_runtime.assert_called_once_with(
                    target,
                    repair_confirmed=False,
                )
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_runtime_download_uses_embedded_hash_without_sidecar_asset(self):
        """A unified two-asset release must fetch the 7z only once."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                response = mock.MagicMock()
                response.status = 200
                response.headers = {"Content-Length": "7"}
                response.read.side_effect = [b"run", b"time", b""]
                response.__enter__.return_value = response
                app._extract_runtime = mock.Mock()
                original_set_runtime_progress = app._set_runtime_progress
                app._set_runtime_progress = mock.Mock(
                    side_effect=original_set_runtime_progress
                )

                with (
                    mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
                    mock.patch.object(main_gateway, "BASE_DIR", Path(tmp)),
                    mock.patch.object(main_gateway, "RUNTIME_PACKAGE_SIZE", 7),
                    mock.patch.object(main_gateway, "RUNTIME_PACKAGE_SHA256",
                                      hashlib.sha256(b"runtime").hexdigest()),
                    mock.patch.object(
                        main_gateway,
                        "_open_download_request",
                        return_value=response,
                    ) as urlopen,
                ):
                    app._download_runtime("https://example.invalid/runtime.7z")
                app.update()

                self.assertEqual(urlopen.call_count, 1)
                self.assertEqual(
                    (Path(tmp) / "cache" / main_gateway.RUNTIME_PACKAGE_NAME).read_bytes(),
                    b"runtime",
                )
                self.assertFalse(
                    Path(
                        f"{Path(tmp) / 'cache' / main_gateway.RUNTIME_PACKAGE_NAME}.sha256"
                    ).exists()
                )
                download_percentages = [
                    call.args[1]
                    for call in app._set_runtime_progress.call_args_list
                    if len(call.args) >= 4 and call.args[2] == "下载运行环境"
                ]
                self.assertEqual(download_percentages, [42, 100])
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_missing_environment_recheck_reports_without_name_error(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app.after = lambda _delay, callback: callback()
        app._finish_runtime_recheck = mock.Mock()
        with mock.patch(
            "app.gui.main_gateway.missing_runtime_paths",
            return_value=["runtime/python/python.exe"],
        ):
            app._retry_env_check()

        result = app._finish_runtime_recheck.call_args.args[0]
        self.assertFalse(result["package_ready"])
        self.assertIn("缺少 1 项", result["message"])

    def test_login_validation_reports_inside_popup_callback(self):
        app = object.__new__(GatewayApp)
        app._server_sync_running = False
        app._get_server_url = lambda: ""
        app._get_server_email = lambda: ""
        app._get_server_password = lambda: ""
        app._set_account_status = mock.Mock()
        results = []

        app._login_and_sync(on_result=lambda success, message: results.append((success, message)))

        self.assertEqual(results, [(False, "请填写服务端地址。")])
        app._set_account_status.assert_called_once_with("请填写服务端地址", "error")

    def test_login_rejects_nonlocal_plain_http_server(self):
        app = object.__new__(GatewayApp)
        app._server_sync_running = False
        app._get_server_url = lambda: "http://api.example.com"
        app._get_server_email = lambda: "user@example.com"
        app._get_server_password = lambda: "password"
        app._set_account_status = mock.Mock()
        results = []

        app._login_and_sync(on_result=lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("HTTPS", results[0][1])
        app._set_account_status.assert_called_once()

    def test_dotted_email_is_preserved_and_valid(self):
        email = "first.last@example.com"

        self.assertEqual(main_gateway._normalize_account_email(email), email)
        self.assertEqual(main_gateway._account_email_error(email), "")

    def test_full_width_email_punctuation_is_normalized_before_login(self):
        self.assertEqual(
            main_gateway._normalize_account_email("  first.last＠EXAMPLE．COM  "),
            "first.last@example.com",
        )

    def test_login_submits_valid_dotted_email_without_removing_periods(self):
        app = object.__new__(GatewayApp)
        app._server_sync_running = False
        app._get_server_url = lambda: "https://api.example.com"
        app._get_server_email = lambda: "first.last@example.com"
        app._get_server_password = lambda: "password"
        app._set_account_status = mock.Mock()
        app._set_account_form_values = mock.Mock()

        with mock.patch.object(threading, "Thread") as thread_cls:
            app._login_and_sync()

        submitted = thread_cls.call_args.kwargs["args"]
        self.assertEqual(submitted[1], "first.last@example.com")
        app._set_account_form_values.assert_called_once_with(email="first.last@example.com")

    def test_json_headers_use_current_app_version(self):
        app = object.__new__(GatewayApp)

        headers = app._json_headers("https://api.example.com", "test-token")

        self.assertIn(f"LingJingClient/{main_gateway.APP_VERSION}", headers["User-Agent"])
        self.assertEqual(headers["Origin"], "https://api.example.com")
        self.assertEqual(headers["Authorization"], "Bearer test-token")

    def test_platform_sync_does_not_upload_prompt_or_task_title(self):
        app = object.__new__(GatewayApp)
        app._last_health = {
            "version": "1.0.1",
            "session_id": "session-test",
            "workflows": [{"id": "image-test", "name": "图片测试", "type": "image"}],
            "current_task": {
                "task_id": "task-test",
                "workflow_id": "image-test",
                "status": "running",
                "progress_percent": 25,
                "prompt_summary": "private prompt",
                "prompt": "private prompt full",
                "title": "private title",
            },
        }
        app._tunnel_url = "https://public.example"
        app._api_key = "sk-local-test"
        app._client_instance_id = "instance-test"
        app._workflow_model_available = mock.Mock(return_value=True)

        payload = app._sync_payload()

        self.assertEqual(payload["current_task"]["task_id"], "task-test")
        self.assertEqual(payload["current_task"]["progress_percent"], 25)
        self.assertNotIn("prompt_summary", payload["current_task"])
        self.assertNotIn("prompt", payload["current_task"])
        self.assertNotIn("title", payload["current_task"])

    def test_account_session_token_is_protected_at_rest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(main_gateway, "BASE_DIR", Path(temp_dir)):
                app = object.__new__(GatewayApp)
                app._server_mode = "logged_in"
                app._server_url_value = "https://api.example.com"
                app._server_user_email = "user@example.com"
                app._server_session_token = "private-session-token"
                app._server_account_profile = {}

                app._save_account_session()
                saved = json.loads(app._account_session_path().read_text(encoding="utf-8"))

                self.assertNotIn("session_token", saved)
                self.assertNotIn("private-session-token", json.dumps(saved))
                restored = object.__new__(GatewayApp)
                restored._server_mode = "unset"
                restored._server_url_value = "https://ai.lol-lu.site"
                restored._server_user_email = ""
                restored._server_session_token = ""
                restored._server_account_profile = {}
                restored._load_account_session_state()
                self.assertEqual(restored._server_session_token, "private-session-token")

    def test_legacy_account_session_token_is_migrated_when_loaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(main_gateway, "BASE_DIR", Path(temp_dir)):
                path = Path(temp_dir) / "runtime" / "account_session.json"
                path.parent.mkdir(parents=True)
                path.write_text(
                    json.dumps(
                        {
                            "mode": "logged_in",
                            "server_url": "https://api.example.com",
                            "email": "user@example.com",
                            "session_token": "legacy-plaintext-token",
                            "profile": {},
                        }
                    ),
                    encoding="utf-8",
                )
                restored = object.__new__(GatewayApp)
                restored._server_mode = "unset"
                restored._server_url_value = "https://ai.lol-lu.site"
                restored._server_user_email = ""
                restored._server_session_token = ""
                restored._server_account_profile = {}

                restored._load_account_session_state()
                saved = json.loads(path.read_text(encoding="utf-8"))

                self.assertEqual(restored._server_session_token, "legacy-plaintext-token")
                self.assertNotIn("session_token", saved)
                self.assertNotIn("legacy-plaintext-token", json.dumps(saved))
                self.assertTrue(saved["session_token_protected"].startswith("dpapi:"))

    def test_local_mode_does_not_start_platform_session_refresh(self):
        app = object.__new__(GatewayApp)
        app._initial_session_sync_done = False
        app._server_mode = "guest"
        app._server_session_token = ""
        app._tunnel_url = "https://example.test"
        app._api_key = "sk-local-test"
        with mock.patch.object(threading, "Thread") as thread:
            app._refresh_saved_login_and_sync()
        thread.assert_not_called()

    def test_many_long_named_workflows_keep_every_management_action_reachable(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.geometry("1090x700")
                app.update()
                app._last_health = {
                    "workflows": [
                        {
                            "id": f"workflow_{index}",
                            "name": f"这是一个用于验证窄窗口布局不会裁切操作按钮的超长工作流名称_{index}",
                            "enabled": True,
                            "available": True,
                            "workflow_json": f"workflow_{index}/workflow.json",
                            "output_type": "image",
                            "dependency_status": "ready",
                            "dependencies": {"nodes": ["SaveImage"]},
                            "nodes_verified": True,
                            "is_default": index == 0,
                        }
                        for index in range(6)
                    ]
                }
                app._dashboard_pages._last_snapshot = ""
                app._dashboard_pages.refresh(app._last_health)
                app._show_page("workflows")
                app.update()

                page = app._pages["resources"]
                buttons = []
                scroll_canvases = []
                for widget in self._walk_widgets(page):
                    try:
                        text = str(widget.cget("text"))
                    except Exception:
                        text = ""
                    if text in {"配置 / 详情", "停用", "设为默认", "模型文件"} and callable(
                        getattr(widget, "invoke", None)
                    ):
                        buttons.append(widget)
                    if isinstance(widget, tk.Canvas):
                        try:
                            if str(widget.cget("yscrollcommand")):
                                scroll_canvases.append(widget)
                        except Exception:
                            pass

                self.assertGreaterEqual(sum(str(item.cget("text")) == "配置 / 详情" for item in buttons), 6)
                self.assertGreaterEqual(sum(str(item.cget("text")) == "停用" for item in buttons), 6)
                self.assertGreaterEqual(sum(str(item.cget("text")) == "设为默认" for item in buttons), 5)
                self.assertTrue(scroll_canvases)
                self.assertFalse(
                    any(isinstance(widget, tk.Scrollbar) for widget in self._walk_widgets(page))
                )
                brand_scrollbars = [
                    widget
                    for widget in self._walk_widgets(page)
                    if isinstance(widget, main_gateway.SlimRoundedScrollbar)
                ]
                self.assertEqual(len(brand_scrollbars), 2)
                self.assertEqual(brand_scrollbars[0].bar_width, 4)
                detail_button = next(
                    button for button in buttons if str(button.cget("text")) == "配置 / 详情"
                )
                self.assertTrue(str(detail_button.bind("<MouseWheel>")))
                self.assertTrue(str(detail_button.bind("<Button-4>")))
                self.assertTrue(str(detail_button.bind("<Button-5>")))
                page_left = page.winfo_rootx()
                page_right = page_left + page.winfo_width()
                self.assertTrue(
                    all(
                        page_left <= button.winfo_rootx()
                        and button.winfo_rootx() + button.winfo_width() <= page_right
                        for button in buttons
                        if button.winfo_ismapped()
                    )
                )
                self.assertLessEqual(page.winfo_reqwidth(), app._page_host.winfo_width())
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_classic_tk_button_converts_pixel_width_to_character_width(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            try:
                parent = tk.Frame(app)
                with mock.patch("app.gui.main_gateway.CTK_AVAILABLE", False):
                    button = app._button(parent, "测试", lambda: None, width=112)
                self.assertLessEqual(int(button.cget("width")), 13)
                button.destroy()
                parent.destroy()
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_unverified_workflow_is_loading_instead_of_missing(self):
        app = object.__new__(GatewayApp)
        app._missing_model_items = mock.Mock(return_value=[])
        workflow = {
            "id": "llm_qwen3_text_gen",
            "name": "qwen3.5",
            "enabled": True,
            "available": False,
            "dependency_status": "unverified",
            "nodes_verified": False,
            "missing_models": [],
            "missing_nodes": [],
        }

        self.assertEqual(app._workflow_status_text(workflow, False), "加载中")

        pages = object.__new__(StaticDashboardPages)
        pages.app = app
        app._model_status = {"missing": {"Qwen3.5": []}}
        app._workflow_model_available = mock.Mock(return_value=False)
        self.assertEqual(
            pages._workflow_state(workflow),
            ("加载中", "neutral", "正在检查模型和 ComfyUI 节点", ""),
        )

    def test_health_update_refreshes_workflows_when_status_changes(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._last_health = {}
        app._update_status = mock.Mock()
        app._update_workflow_display = mock.Mock()
        app._update_task_display = mock.Mock()
        app._dashboard_pages = mock.Mock()
        data = {
            "workflows": [
                {
                    "id": "z_image_t2i_v1",
                    "dependency_status": "ready",
                    "nodes_verified": True,
                }
            ],
            "current_task": None,
        }

        app._on_health_update(data)

        app._update_workflow_display.assert_called_once_with(data)
        app._dashboard_pages.refresh.assert_called_once_with(data)

    def test_workflow_display_fingerprint_tracks_models_and_validation(self):
        app = object.__new__(GatewayApp)
        app._model_status = {"Z-Image": "完整", "all_ok": True}
        loading = [
            {
                "id": "z_image_t2i_v1",
                "dependency_status": "unverified",
                "nodes_verified": False,
            }
        ]
        ready = [
            {
                "id": "z_image_t2i_v1",
                "dependency_status": "ready",
                "nodes_verified": True,
            }
        ]

        loading_fingerprint = app._workflow_display_fingerprint(loading, "z_image_t2i_v1")
        self.assertEqual(
            loading_fingerprint,
            app._workflow_display_fingerprint(list(loading), "z_image_t2i_v1"),
        )
        self.assertNotEqual(
            loading_fingerprint,
            app._workflow_display_fingerprint(ready, "z_image_t2i_v1"),
        )
        app._model_status = {"Z-Image": "缺失", "all_ok": False}
        self.assertNotEqual(
            loading_fingerprint,
            app._workflow_display_fingerprint(loading, "z_image_t2i_v1"),
        )

    def test_unchanged_workflow_snapshot_does_not_rebuild_rows(self):
        app = object.__new__(GatewayApp)
        app._model_status = {"Z-Image": "完整", "all_ok": True}
        app._workflow_canvas = mock.Mock()
        app._workflow_canvas.yview.return_value = (0.0, 1.0)
        image_section = mock.Mock()
        app._wf_sections = {"image": image_section}
        workflows = [
            {
                "id": "z_image_t2i_v1",
                "dependency_status": "ready",
                "nodes_verified": True,
            }
        ]
        app._workflow_display_fingerprint_value = app._workflow_display_fingerprint(
            workflows,
            "z_image_t2i_v1",
        )
        data = {
            "workflows": workflows,
            "default_workflow_id": "z_image_t2i_v1",
        }

        with (
            mock.patch(
                "app.gui.main_gateway.read_local_workflow_catalog",
                return_value=[],
            ),
            mock.patch(
                "app.gui.main_gateway.merge_workflow_catalog",
                return_value=workflows,
            ),
        ):
            app._update_workflow_display(data)

        image_section.winfo_children.assert_not_called()

    def test_capability_labels_are_textual_and_not_color_only(self):
        app = object.__new__(GatewayApp)
        expected = {
            "text_image_to_image": "文/图生图",
            "text_to_image": "文生图",
            "first_last_frame": "首尾帧",
            "text_model": "文本模型",
        }
        for capability, label in expected.items():
            with self.subTest(capability=capability):
                _key, actual, color = app._workflow_capability_meta(
                    {"capability": capability}
                )
                self.assertEqual(actual, label)
                self.assertRegex(color, r"^#[0-9a-fA-F]{6}$")

    def test_repeated_completed_task_does_not_redraw_progress(self):
        app = object.__new__(GatewayApp)
        app._current_task_text = "无任务"
        app._last_completed_outputs = []
        app._last_completed_task_id = ""
        app._task_status_label = mock.Mock()
        app._prog_info = mock.Mock()
        app._workflow_info = mock.Mock()
        app._prog_detail = mock.Mock()
        app._preview_output_btn = mock.Mock()
        app._preview_output_btn.winfo_ismapped.return_value = False
        app._show_progress_panel = mock.Mock()
        app._task_context_text = mock.Mock(return_value="测试任务")
        app._set_progress_bar = mock.Mock()
        app._remember_task_history = mock.Mock()
        app.after = mock.Mock()
        task = {
            "task_id": "task-completed-1",
            "status": "completed",
            "workflow_id": "flux_t2i_v1",
            "progress_percent": 100,
            "elapsed_seconds": 12,
            "outputs": [],
        }

        app._update_task_display(task)
        app._update_task_display(dict(task))

        app._set_progress_bar.assert_called_once_with(100, "已完成 · 100%")
        app.after.assert_called_once_with(15000, app._hide_progress)

    def test_maintenance_progress_can_collapse_to_bottom_right_and_restore(self):
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
        ):
            app = GatewayApp()
            dialog = None
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                dialog = app._create_runtime_progress_dialog(
                    title="安装运行环境",
                    heading="正在安装运行环境",
                    stage="解压核心模块",
                    detail="Python · ComfyUI · Torch/CUDA",
                )
                app.update()

                self._find_by_text(dialog["popup"], "后台修复").invoke()
                app.update()

                self.assertEqual(dialog["popup"].state(), "withdrawn")
                card = dialog["background_card"]
                self.assertIsNotNone(card)
                self.assertEqual(card.place_info().get("anchor"), "se")
                self.assertGreaterEqual(card.winfo_width(), 360)

                app._restore_maintenance_dialog(dialog)
                app.update()
                self.assertEqual(dialog["popup"].state(), "normal")
                self.assertIsNone(dialog["background_card"])
            finally:
                if dialog is not None:
                    app._close_maintenance_dialog(dialog)
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_model_download_dialog_keeps_bottom_actions_visible_and_scrollable(self):
        missing = [
            {
                "path": f"diffusion_models/model-{index}.safetensors",
                "url": f"https://huggingface.co/example/model/resolve/main/model-{index}.safetensors",
                "size_bytes": 1024,
            }
            for index in range(6)
        ]
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
            mock.patch.object(GatewayApp, "_missing_model_items", return_value=missing),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                app._show_model_install_help("Flux2 Klein 4B")
                app.update()
                popup = next(
                    child
                    for child in app.winfo_children()
                    if isinstance(child, tk.Toplevel) and child.title() == "下载模型"
                )
                popup.update()
                popup.geometry("650x545")
                popup.update()
                popup_height = popup.winfo_height()
                for text in ("下载全部模型", "打开模型文件夹", "关闭"):
                    button = self._find_by_text(popup, text)
                    self.assertGreaterEqual(button.winfo_height(), 28)
                    self.assertLessEqual(
                        button.winfo_rooty() + button.winfo_height(),
                        popup.winfo_rooty() + popup_height,
                    )
                item_download_buttons = []
                button_types = (tk.Button,)
                if main_gateway.CTK_AVAILABLE:
                    button_types += (main_gateway.ctk.CTkButton,)
                for widget in self._walk_widgets(popup):
                    if not isinstance(widget, button_types):
                        continue
                    try:
                        if str(widget.cget("text")) == "下载":
                            item_download_buttons.append(widget)
                    except (tk.TclError, ValueError):
                        continue
                self.assertEqual(len(item_download_buttons), len(missing))
                popup_right = popup.winfo_rootx() + popup.winfo_width()
                for button in item_download_buttons:
                    self.assertGreaterEqual(button.winfo_width(), 68)
                    self.assertLessEqual(
                        button.winfo_rootx() + button.winfo_width(),
                        popup_right,
                    )
                scrollbars = [
                    widget
                    for widget in self._walk_widgets(popup)
                    if isinstance(widget, main_gateway.SlimRoundedScrollbar)
                ]
                self.assertEqual(len(scrollbars), 1)
                self.assertTrue(str(popup.bind("<MouseWheel>")))
                self._find_by_text(popup, "关闭").invoke()
            finally:
                app._dashboard_pages.cancel_pending()
                app.destroy()

    def test_workflow_detail_puts_install_model_action_in_header(self):
        workflow = {
            "id": "flux2_klein_4b_v1",
            "name": "FLUX.2 Klein 4B",
            "description": "轻量图像生成工作流",
            "model_group": "Flux2 Klein 4B",
            "capability": "text_image_to_image",
        }
        with (
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(GatewayApp, "_maybe_show_login_prompt", lambda _app: None),
            mock.patch.object(
                GatewayApp,
                "_missing_model_items",
                return_value=[{"path": "missing.safetensors"}],
            ),
        ):
            app = GatewayApp()
            try:
                app.attributes("-alpha", 0.0)
                app.update()
                app._show_workflow_schema(workflow)
                app.update()
                popup = next(
                    child
                    for child in app.winfo_children()
                    if isinstance(child, tk.Toplevel) and child.title().startswith("工作流详情")
                )
                install_button = self._find_by_text(popup, "安装模型")
                self.assertLess(install_button.winfo_rooty() - popup.winfo_rooty(), 120)
                self.assertNotIn("下载缺失模型", set(self._all_text(popup)))
            finally:
                for child in list(app.winfo_children()):
                    if isinstance(child, tk.Toplevel):
                        child.destroy()
                app._dashboard_pages.cancel_pending()
                app.destroy()


if __name__ == "__main__":
    unittest.main()
