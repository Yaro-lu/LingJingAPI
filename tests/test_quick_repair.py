import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest import mock

from app.core import quick_repair
from app.gui.main_gateway import GatewayApp


class QuickRepairPlanTests(unittest.TestCase):
    def test_vram_boundary_and_unknown_gpu(self):
        self.assertEqual(quick_repair.profile_for_vram(12 * 1024 + 1), "above_12gb")
        self.assertEqual(quick_repair.profile_for_vram(12 * 1024), "at_most_12gb")
        self.assertEqual(quick_repair.profile_for_vram(8 * 1024), "at_most_12gb")
        self.assertIsNone(quick_repair.profile_for_vram(0))

    def test_selected_workflows_and_model_groups_match_the_approved_design(self):
        high = quick_repair.QUICK_REPAIR_PROFILES["above_12gb"]
        low = quick_repair.QUICK_REPAIR_PROFILES["at_most_12gb"]
        self.assertEqual(high["names"], ("Qwen3.5", "FLUX.2 Klein 4B", "性价比H3"))
        self.assertEqual(low["names"], ("Qwen3.5", "FLUX.2 Klein 9B", "高质量H3"))
        for profile in (high, low):
            self.assertEqual(len(profile["workflows"]), 3)
            self.assertEqual(len(profile["groups"]), 3)
            self.assertEqual(len(quick_repair.profile_model_items(
                "above_12gb" if profile is high else "at_most_12gb"
            )), 8)

    def test_model_plan_deduplicates_paths_and_requires_pinned_sources(self):
        item = {"path": "vae/shared.safetensors", "url": "https://example.test/x", "size_bytes": 3, "sha256": "a" * 64}
        requirements = {
            group: {"items": [item]}
            for group in quick_repair.QUICK_REPAIR_PROFILES["above_12gb"]["groups"]
        }
        self.assertEqual(quick_repair.profile_model_items("above_12gb", requirements), [item])
        requirements["Qwen3.5"] = {"items": [{**item, "sha256": ""}]}
        with self.assertRaisesRegex(ValueError, "校验信息"):
            quick_repair.profile_model_items("above_12gb", requirements)

    def test_only_missing_or_wrong_size_files_enter_download_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = {"path": "vae/a.safetensors", "size_bytes": 3}
            with mock.patch.object(quick_repair, "profile_model_items", return_value=[item]):
                self.assertEqual(quick_repair.missing_profile_items(root, "above_12gb"), [item])
                target = root / item["path"]
                target.parent.mkdir()
                target.write_bytes(b"abc")
                self.assertEqual(quick_repair.missing_profile_items(root, "above_12gb"), [])
                target.write_bytes(b"a")
                self.assertEqual(quick_repair.missing_profile_items(root, "above_12gb"), [item])

    def test_clicked_repair_hashes_existing_models_without_changing_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "vae" / "a.safetensors"
            target.parent.mkdir()
            target.write_bytes(b"bad")
            item = {
                "path": "vae/a.safetensors",
                "size_bytes": 3,
                "sha256": hashlib.sha256(b"good").hexdigest(),
            }
            with mock.patch.object(quick_repair, "profile_model_items", return_value=[item]):
                missing, invalid = quick_repair.inspect_existing_profile_models(
                    root, "above_12gb"
                )
            self.assertEqual(missing, [item])
            self.assertEqual(invalid, [target])
            self.assertEqual(target.read_bytes(), b"bad")

    def test_clicked_repair_preserves_wrong_size_file_for_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "vae" / "a.safetensors"
            target.parent.mkdir()
            target.write_bytes(b"short")
            item = {
                "path": "vae/a.safetensors",
                "size_bytes": 10,
                "sha256": hashlib.sha256(b"0123456789").hexdigest(),
            }
            with mock.patch.object(quick_repair, "profile_model_items", return_value=[item]):
                missing, invalid = quick_repair.inspect_existing_profile_models(root, "above_12gb")
            self.assertEqual(missing, [item])
            self.assertEqual(invalid, [target])
            self.assertEqual(target.read_bytes(), b"short")

    def test_dismissal_and_pending_resume_persist_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime" / "quick_repair.json"
            self.assertFalse(quick_repair.load_quick_repair_state(path)["dismissed"])
            quick_repair.save_quick_repair_state(
                path, dismissed=True, pending_profile="at_most_12gb"
            )
            self.assertEqual(
                quick_repair.load_quick_repair_state(path),
                {"dismissed": True, "pending_profile": "at_most_12gb"},
            )


class QuickRepairStartupTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._quick_repair_prompt_seen = False
        app._quick_repair_state = {"dismissed": False, "pending_profile": ""}
        app._model_status = {"Qwen3.5": "完整", "Flux2 Klein 4B": "完整", "H3 Regular": "完整"}
        app.after = lambda _delay, callback: callback()
        app._show_quick_repair_dialog = mock.Mock()
        return app

    def test_ready_install_with_selected_models_does_not_show_prompt(self):
        app = self._app()
        app._offer_quick_repair(True, 16 * 1024)
        app._show_quick_repair_dialog.assert_not_called()

    def test_missing_environment_shows_prompt_once(self):
        app = self._app()
        app._offer_quick_repair(False, 16 * 1024)
        app._offer_quick_repair(False, 16 * 1024)
        app._show_quick_repair_dialog.assert_called_once()

    def test_pending_repair_resumes_even_if_prompt_was_dismissed(self):
        app = self._app()
        app._quick_repair_state = {"dismissed": True, "pending_profile": "at_most_12gb"}
        app._offer_quick_repair(True, 16 * 1024)
        self.assertTrue(app._show_quick_repair_dialog.call_args.kwargs["auto_resume"])

    def test_missing_runtime_hands_off_to_auto_restart_installer(self):
        app = self._app()
        app._environment_status = {"ready": False}
        app._quick_repair_run = None
        app._quick_repair_popup = {
            "profile_var": mock.Mock(get=lambda: "at_most_12gb"),
            "skip_var": mock.Mock(get=lambda: False),
            "status_var": mock.Mock(),
            "progress_var": mock.Mock(),
            "skip_check": mock.Mock(),
            "start_button": mock.Mock(),
            "cancel_button": mock.Mock(),
            "option_frames": {},
            "popup": mock.Mock(),
        }
        app._save_quick_repair_state = mock.Mock()
        app._runtime_mirror_url = mock.Mock(return_value="https://example.test/runtime.7z")
        app._download_runtime = mock.Mock()
        app._extract_runtime = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "app.gui.main_gateway.BASE_DIR", Path(tmp)
        ):
            app._begin_quick_repair()
        app._save_quick_repair_state.assert_called_once_with(
            dismissed=False, pending_profile="at_most_12gb"
        )
        app._download_runtime.assert_called_once_with(
            "https://example.test/runtime.7z", repair_confirmed=True, auto_restart=True
        )
        self.assertIsNone(app._quick_repair_popup)

    def test_driver_failure_does_not_redownload_runtime_package(self):
        app = self._app()
        app._environment_status = {
            "ready": False,
            "package_ready": True,
            "hardware_ready": False,
            "message": "未检测到 NVIDIA 显卡驱动",
        }
        app._quick_repair_run = None
        app._quick_repair_popup = {
            "profile_var": mock.Mock(get=lambda: "above_12gb"),
            "skip_var": mock.Mock(get=lambda: False),
            "skip_check": mock.Mock(),
            "start_button": mock.Mock(),
            "cancel_button": mock.Mock(),
            "option_frames": {},
        }
        app._save_quick_repair_state = mock.Mock()
        app._quick_repair_fail = mock.Mock()
        app._download_runtime = mock.Mock()
        app._begin_quick_repair()
        app._quick_repair_fail.assert_called_once()
        self.assertIn("显卡驱动", app._quick_repair_fail.call_args.args[0])
        app._download_runtime.assert_not_called()

    def test_verification_waits_for_backend_restart_instead_of_using_stale_health(self):
        app = self._app()
        run = {"profile": "above_12gb", "phase": "models"}
        app._quick_repair_run = run
        app._quick_repair_popup = {
            "status_var": mock.Mock(),
            "progress_var": mock.Mock(),
        }
        app._health_event_applied = 7
        app._restart_backend = mock.Mock(return_value=False)
        app._start_background_model_recheck = mock.Mock()
        app.after = mock.Mock()
        app._quick_repair_verify(run)
        self.assertEqual(run["phase"], "verify")
        self.assertNotIn("health_event_before", run)
        app._start_background_model_recheck.assert_not_called()
        app.after.assert_called_once()

        app.after.reset_mock()
        app._restart_backend.return_value = True
        app._quick_repair_verify(run)
        self.assertEqual(run["health_event_before"], 7)
        app._start_background_model_recheck.assert_called_once()
        app.after.assert_called_once()

    def test_online_backend_reports_missing_nodes_without_false_success(self):
        app = self._app()
        run = {
            "profile": "above_12gb",
            "health_event_before": 7,
            "verify_deadline": float("inf"),
            "phase": "verify",
        }
        app._quick_repair_run = run
        app._quick_repair_popup = {"progress_var": mock.Mock(), "status_var": mock.Mock()}
        app._health_event_applied = 8
        app._last_health = {
            "comfyui": {"status": "online"},
            "workflows": [{
                "id": "lingjing_h3_3060ti_regular_it2v",
                "name": "性价比H3",
                "available": False,
                "missing_nodes": ["MiniMaxH3ImageToVideo"],
            }],
        }
        app._quick_repair_fail = mock.Mock()
        app.after = mock.Mock()
        app._quick_repair_check_workflows(run)
        self.assertIn("MiniMaxH3ImageToVideo", app._quick_repair_fail.call_args.args[0])
        app.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
