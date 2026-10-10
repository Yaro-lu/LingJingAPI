import json
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from app.core.beginner_mode import (
    automatic_repair_profile, beginner_service_state, build_example_launch_url,
    load_interface_preferences, save_interface_preferences,
    beginner_download_plan, beginner_model_download_progress,
)
from app.core.quick_repair import QUICK_REPAIR_PROFILES
from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp


def ready_health(profile="above_12gb"):
    return {
        "comfyui": {"status": "online"},
        "workflows": [{"id": key, "available": True} for key in QUICK_REPAIR_PROFILES[profile]["workflows"]],
    }


class BeginnerPolicyTests(unittest.TestCase):
    def test_first_launch_invalid_preferences_and_mode_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime" / "interface.json"
            defaults = {"mode": "beginner", "onboarding_complete": False, "profile": ""}
            self.assertEqual(load_interface_preferences(path), defaults)
            path.parent.mkdir()
            for invalid in ("{bad", "[]", '{"mode":"other","onboarding_complete":"true","profile":"other"}', '{"profile":[]}'):
                path.write_text(invalid, encoding="utf-8")
                self.assertEqual(load_interface_preferences(path), defaults)
            expected = {"mode": "expert", "onboarding_complete": True, "profile": "at_most_12gb"}
            save_interface_preferences(path, {**expected, "api_key": "must-not-be-saved"})
            self.assertEqual(load_interface_preferences(path), expected)
            self.assertNotIn("must-not-be-saved", path.read_text(encoding="utf-8"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_automatic_profiles_keep_the_approved_boundary_and_pending_plan(self):
        self.assertEqual(automatic_repair_profile(12 * 1024), "at_most_12gb")
        self.assertEqual(automatic_repair_profile(12 * 1024 + 1), "above_12gb")
        self.assertEqual(automatic_repair_profile(0), "above_12gb")
        self.assertEqual(automatic_repair_profile(16 * 1024, "at_most_12gb"), "at_most_12gb")

    def state(self, **changes):
        args = dict(environment={"ready": True}, lights={"api": "online", "comfyui": "online", "tunnel": "offline"}, health=ready_health(), profile="above_12gb", onboarding_complete=True, api_key_ready=True)
        args.update(changes)
        return beginner_service_state(**args)

    def test_ready_requires_real_workflows_but_not_a_public_tunnel(self):
        self.assertEqual(self.state()[0:2], ("ready", "启动成功"))
        health = ready_health()
        health["workflows"][2]["available"] = False
        self.assertEqual(self.state(health=health)[0], "ready")
        health["workflows"][0]["available"] = False
        self.assertEqual(self.state(health=health)[0], "repair")
        self.assertEqual(self.state(api_key_ready=False)[0], "starting")
        self.assertEqual(self.state(health={})[0], "starting")
        self.assertEqual(self.state(onboarding_complete=False)[0], "ready")

    def test_starting_failure_and_environment_repair_are_distinct(self):
        self.assertEqual(self.state(repair_phase="models")[0], "starting")
        self.assertEqual(self.state(maintaining=True)[0], "starting")
        self.assertEqual(self.state(repair_phase="failed")[0:2], ("failed", "启动失败"))
        self.assertEqual(self.state(start_blocked=True)[0], "failed")
        self.assertEqual(self.state(lights={"api": "offline", "comfyui": "online"})[0], "failed")
        self.assertEqual(self.state(environment={"ready": False})[0], "repair")
        self.assertEqual(self.state(onboarding_complete=False, backend_error="端口占用")[0], "failed")

    def test_example_launch_passes_key_only_in_fragment_and_only_to_loopback(self):
        url = build_example_launch_url("http://127.0.0.1:19111", "generation-only &#+")
        parsed = urlsplit(url)
        self.assertEqual(parsed.query, "")
        self.assertEqual(parse_qs(parsed.fragment), {"lingjing_key": ["generation-only &#+"]})
        for invalid in ("https://attacker.invalid", "http://192.168.1.5:19111", "http://127.0.0.1:19111/path", "http://user:pass@127.0.0.1:19111", "http://127.0.0.1:19111?redirect=x"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_example_launch_url(invalid, "key")


class BeginnerStartupTests(unittest.TestCase):
    def app(self, complete=False, pending=""):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._quick_repair_prompt_seen = False
        app._quick_repair_state = {"dismissed": True, "pending_profile": pending}
        app._ui_mode = "beginner"
        app._interface_preferences = {"onboarding_complete": complete}
        app._model_status = {"Qwen3.5": "完整"}
        app._refresh_beginner_view = mock.Mock()
        app._show_quick_repair_dialog = mock.Mock()
        app.after = lambda _delay, callback: callback()
        return app

    def test_missing_qwen_offers_configuration_free_repair_once(self):
        app = self.app()
        app._model_status["Qwen3.5"] = "缺失"
        app._offer_quick_repair(True, 8 * 1024)
        app._offer_quick_repair(True, 8 * 1024)
        app._show_quick_repair_dialog.assert_called_once_with(initial_profile="at_most_12gb", vram_mb=8 * 1024, auto_resume=False, simplified=True)

    def test_existing_install_is_detected_without_onboarding_preferences(self):
        app = self.app(complete=False)
        app._offer_quick_repair(True, 8 * 1024)
        app._show_quick_repair_dialog.assert_not_called()
        app._refresh_beginner_view.assert_called_once()

    def test_successful_onboarding_keeps_existing_lightweight_startup_policy(self):
        app = self.app(complete=True)
        app._offer_quick_repair(True, 8 * 1024)
        app._show_quick_repair_dialog.assert_not_called()
        app._model_status["Qwen3.5"] = "缺失"
        app._offer_quick_repair(True, 8 * 1024)
        self.assertTrue(app._show_quick_repair_dialog.call_args.kwargs["simplified"])

    def test_runtime_restart_resumes_pending_models_even_when_qwen_is_ready(self):
        app = self.app(complete=True, pending="at_most_12gb")
        app._offer_quick_repair(True, 16 * 1024)
        self.assertTrue(app._show_quick_repair_dialog.call_args.kwargs["auto_resume"])
        self.assertEqual(app._show_quick_repair_dialog.call_args.kwargs["initial_profile"], "at_most_12gb")

    def test_automatic_offer_has_no_full_model_scan(self):
        with mock.patch.object(main_gateway, "_check_models_status", side_effect=AssertionError("No full scan")):
            self.app()._offer_quick_repair(False, 0)

    def test_delayed_prompt_respects_a_switch_to_expert(self):
        app = self.app()
        app.after = mock.Mock()
        app._offer_quick_repair(False, 8 * 1024)
        app._ui_mode = "expert"
        # A late healthy snapshot suppresses a stale prompt too.
        app._show_beginner_startup_repair("at_most_12gb", 8192, "", True, True)
        app._show_quick_repair_dialog.assert_not_called()
        app.after.call_args.args[1]()
        self.assertFalse(app._show_quick_repair_dialog.call_args.kwargs["simplified"])


class BeginnerDownloadTests(unittest.TestCase):
    def test_selected_unique_files_count_existing_mappings_and_partials_without_hashing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            models = root / "models"
            models.mkdir()
            (models / "ready.bin").write_bytes(b"a" * 80)
            (models / "missing.bin.part").write_bytes(b"b" * 20)
            items = [{"path": "ready.bin", "size_bytes": 80}, {"path": "missing.bin", "size_bytes": 120}]
            with mock.patch("app.core.beginner_mode.profile_model_items", return_value=items), mock.patch("app.core.model_maintenance.model_file_sha256_matches", side_effect=AssertionError("No startup hashes")):
                plan = beginner_download_plan(root, models, "above_12gb", True)
                self.assertEqual(plan["model_bytes"], 200)
                self.assertEqual(plan["remaining_bytes"], 100)
                self.assertEqual(plan["runtime_have"], plan["runtime_bytes"])
                first = beginner_download_plan(root, models, "above_12gb", False)
                self.assertEqual(first["remaining_bytes"], plan["runtime_bytes"] + 100)

    def test_bytes_weighted_progress_survives_resume_and_recalculates_invalid_files(self):
        plan = {"runtime_bytes": 100, "model_bytes": 1000, "total_bytes": 1100}
        controls = [
            {"item": {"size_bytes": 900}, "state": "downloading", "downloaded_bytes": 450},
            {"item": {"size_bytes": 100}, "state": "done"},
        ]
        done, percent = beginner_model_download_progress(plan, controls)
        self.assertEqual(done, 650)
        self.assertAlmostEqual(percent, 65000 / 1100)
        controls[0]["state"] = "done"
        self.assertEqual(beginner_model_download_progress(plan, controls), (1100, 100))
        self.assertEqual(beginner_model_download_progress(plan, []), (1100, 100))


class BeginnerHiddenInterfaceTests(unittest.TestCase):
    """Use only our own withdrawn Tk windows; no desktop or browser automation."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root_dir = Path(self.temporary.name)
        (self.root_dir / "runtime").mkdir()
        original_init = main_gateway.WindowBase.__init__
        def hidden_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            window.withdraw()
        original_popup = tk.Toplevel.__init__
        def hidden_popup(window, *args, **kwargs):
            original_popup(window, *args, **kwargs)
            window.withdraw()
        for patch in (
            mock.patch.object(main_gateway, "BASE_DIR", self.root_dir),
            mock.patch.object(threading.Thread, "start", lambda _thread: None),
            mock.patch.object(main_gateway.WindowBase, "__init__", hidden_init),
            mock.patch.object(tk.Toplevel, "__init__", hidden_popup),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.app = GatewayApp()
        self.addCleanup(self.app.destroy)

    def test_modes_preserve_the_original_expert_objects_and_selected_page(self):
        app = self.app
        self.assertEqual(app._ui_mode, "beginner")
        self.assertEqual(app._app_shell.winfo_manager(), "")
        self.assertEqual(app._beginner_page.winfo_manager(), "pack")
        pages = dict(app._pages)
        app._set_interface_mode("expert")
        app._show_page("settings")
        app._set_interface_mode("beginner")
        app._set_interface_mode("expert")
        self.assertEqual(app._current_page_id, "settings")
        self.assertEqual(app._pages, pages)
        self.assertEqual(app._beginner_page.winfo_manager(), "")
        self.assertEqual(app._app_shell.winfo_manager(), "pack")
        self.assertEqual(load_interface_preferences(app._interface_preferences_path)["mode"], "expert")

    def test_beginner_repair_has_no_visible_profile_options_and_expert_keeps_them(self):
        app = self.app
        app._environment_status = {"ready": True, "vram_mb": 8 * 1024}
        app._open_beginner_repair()
        dialog = app._quick_repair_popup
        self.assertTrue(dialog["simplified"])
        self.assertEqual(dialog["profile_var"].get(), "at_most_12gb")
        self.assertEqual(dialog["skip_check"].winfo_manager(), "")
        self.assertEqual(next(iter(dialog["option_frames"].values())).master.winfo_manager(), "")
        app._close_quick_repair_dialog()
        app._set_interface_mode("expert")
        app._show_quick_repair_dialog(vram_mb=8 * 1024)
        self.assertFalse(app._quick_repair_popup["simplified"])
        self.assertEqual(app._quick_repair_popup["skip_check"].winfo_manager(), "pack")
        self.assertEqual(next(iter(app._quick_repair_popup["option_frames"].values())).master.winfo_manager(), "pack")

    def test_real_service_snapshot_enables_creation_and_uses_generation_key(self):
        app = self.app
        app._environment_status = {"ready": True}
        app._light_states.update(api="online", comfyui="online", tunnel="offline")
        app._last_health = ready_health()
        app._local_url = "http://127.0.0.1:18188"
        app._lan_url = "http://192.168.1.20:18188"
        app._api_key = "test-generation-only"
        app._mark_beginner_prepared("above_12gb")
        self.assertEqual(app._beginner_status_label.cget("text"), "启动成功")
        self.assertEqual(app._beginner_create_button.cget("state"), "normal")
        self.assertEqual(app._beginner_url_var.get(), app._lan_url)
        self.assertTrue(app._beginner_key_entry.cget("show"))
        with mock.patch("app.gui.beginner_mode.webbrowser.open", return_value=True) as browser:
            app._open_beginner_example()
            self.assertEqual(parse_qs(urlsplit(browser.call_args.args[0]).fragment), {"lingjing_key": ["test-generation-only"]})
            app._light_states["api"] = "offline"
            app._open_beginner_example()
            browser.assert_called_once()
        self.assertEqual(app._beginner_create_button.cget("state"), "disabled")
        self.assertEqual(app._beginner_status_label.cget("text"), "启动失败")
        self.assertEqual(load_interface_preferences(app._interface_preferences_path)["profile"], "above_12gb")

    def test_successful_beginner_repair_returns_to_the_simple_page_automatically(self):
        app = self.app
        app._show_quick_repair_dialog(initial_profile="above_12gb", simplified=True)
        dialog = app._quick_repair_popup
        run = {"profile": "above_12gb", "phase": "verify", "health_event_before": 0, "cancelled": threading.Event(), "controls": []}
        app._quick_repair_run = run
        app._health_event_applied = 1
        app._last_health = ready_health()
        with mock.patch.object(app, "after") as schedule:
            app._quick_repair_check_workflows(run)
            self.assertEqual(run["phase"], "complete")
            self.assertTrue(load_interface_preferences(app._interface_preferences_path)["onboarding_complete"])
            delay, callback = schedule.call_args.args
            self.assertEqual(delay, 750)
            callback()
        self.assertIsNone(app._quick_repair_popup)
        self.assertEqual(app._ui_mode, "beginner")

    def test_blocked_update_can_still_enter_confirmed_runtime_repair(self):
        app = self.app
        app._runtime_start_blocked = True
        app._open_beginner_repair()
        self.assertFalse(app._environment_status["ready"])
        with (
            mock.patch.object(app, "_runtime_mirror_url", return_value="https://example.test/runtime.7z"),
            mock.patch.object(app, "_download_runtime") as download,
        ):
            app._begin_quick_repair()
            download.assert_called_once_with("https://example.test/runtime.7z", repair_confirmed=True, auto_restart=True)

    def test_model_download_bar_uses_actual_bytes_instead_of_file_count(self):
        app = self.app
        app._show_quick_repair_dialog(initial_profile="above_12gb", simplified=True)
        control = {"item": {"path": "large.bin", "size_bytes": 900}, "state": "downloading", "downloaded_bytes": 450}
        small = {"item": {"path": "small.bin", "size_bytes": 100}, "state": "done"}
        run = {"controls": [small, control], "index": 1, "current": control,
               "download_plan": {"total_bytes": 1100, "runtime_bytes": 100, "model_bytes": 1000}}
        app._quick_repair_run = run
        app._quick_repair_poll_model(run)
        self.assertAlmostEqual(app._quick_repair_popup["progress_var"].get(), 65000 / 1100)
        self.assertIn("总下载进度", app._quick_repair_popup["status_var"].get())

    def test_runtime_phase_has_a_separate_byte_weighted_total_download_bar(self):
        app = self.app
        dialog = app._create_runtime_progress_dialog(title="测试", heading="测试", stage="下载环境", detail="隔离测试")
        self.addCleanup(dialog["popup"].destroy)
        plan = {"runtime_bytes": 100, "model_bytes": 900, "total_bytes": 1000,
                "runtime_have": 20, "model_have": 300, "remaining_bytes": 680}
        app._attach_beginner_runtime_download(dialog, plan)
        self.assertEqual(dialog["total_download_var"].get(), 32)
        app._update_beginner_runtime_download(dialog, plan, 60)
        self.assertEqual(dialog["total_download_var"].get(), 36)
        self.assertIn("36.0%", dialog["total_download_label"].get())
