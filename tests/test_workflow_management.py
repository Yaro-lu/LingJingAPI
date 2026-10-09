import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp
from app.workflow_registry import WorkflowRegistry


VALID_WORKFLOW = {
    "1": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "checkpoints/example.safetensors"},
    },
    "2": {"class_type": "SaveImage", "inputs": {}},
}


class WorkflowManagementTests(unittest.TestCase):
    @staticmethod
    def _app() -> GatewayApp:
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        return app

    @staticmethod
    def _existing_workflow(base: Path, workflow_id: str = "existing") -> None:
        folder = base / "workflows" / workflow_id
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "id": workflow_id,
                    "name": workflow_id,
                    "type": "image.text_to_image",
                    "engine": "comfyui",
                }
            ),
            encoding="utf-8",
        )
        (folder / "workflow.json").write_text(
            json.dumps({"1": {"class_type": "SaveImage", "inputs": {}}}),
            encoding="utf-8",
        )

    def test_failed_editor_conversion_is_durable_and_repair_reuses_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "workflows").mkdir()
            source = base / "my-editor.json"
            editor = {"nodes": [{"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["example.safetensors"]}], "links": []}
            source.write_text(json.dumps(editor), encoding="utf-8")
            app = self._app()
            app._convert_front_workflow_to_api = mock.Mock(side_effect=ValueError("缺少节点：Example"))
            with mock.patch.object(main_gateway, "BASE_DIR", base):
                result = app._install_workflow_from_path(source)
                folder = Path(result["target"])
                self.assertFalse((folder / "workflow.json").exists())
                self.assertEqual(json.loads((folder / "frontend_workflow.json").read_text(encoding="utf-8")), editor)
                registry = app._workflow_registry()
                pending = registry.get(result["id"])
                self.assertFalse(pending.enabled)
                self.assertIsNone(registry.resolve(result["id"]))
                self.assertEqual(pending.api_mapping["api_mapping_status"], "pending_conversion")
                self.assertEqual(pending.dependencies["models"][0]["name"], "example.safetensors")
                with self.assertRaises(ValueError):
                    registry.set_enabled(result["id"], True)
                before = (folder / "manifest.json").read_bytes()
                with self.assertRaises(ValueError):
                    app._retry_pending_workflow(result["id"])
                self.assertEqual((folder / "manifest.json").read_bytes(), before)
                app._convert_front_workflow_to_api.side_effect = None
                app._convert_front_workflow_to_api.return_value = VALID_WORKFLOW
                with mock.patch.object(WorkflowRegistry, "_save_unlocked", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        app._retry_pending_workflow(result["id"])
                self.assertEqual((folder / "manifest.json").read_bytes(), before)
                self.assertFalse((folder / "workflow.json").exists())
                repaired = app._retry_pending_workflow(result["id"])
                self.assertEqual(repaired["id"], result["id"])
                latest = app._workflow_registry()
                self.assertEqual(len(latest.workflows), 1)
                self.assertTrue(latest.get(result["id"]).enabled)
                self.assertNotEqual(latest.get(result["id"]).api_mapping.get("api_mapping_status"), "pending_conversion")
                self.assertEqual(json.loads((folder / "workflow.json").read_text(encoding="utf-8")), VALID_WORKFLOW)
                self.assertTrue((folder / "frontend_workflow.json").exists())

    def test_bundled_replacement_upgrades_only_previous_pending_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            folder = base / "workflows" / "flux"
            folder.mkdir(parents=True)
            manifest = {
                "id": "flux", "name": "Old", "type": "unknown", "enabled": False,
                "api_mapping_status": "pending_conversion", "description": "等待修复",
            }
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            config = base / "runtime" / "workflow_config.json"
            registry = WorkflowRegistry(config, base / "workflows")
            self.assertFalse(registry.get("flux").enabled)
            replacement = {
                "id": "flux", "name": "New", "type": "image.image_to_image",
                "enabled": True, "replaces_pending_conversion": True,
                "description": "新的轻量版本",
            }
            (folder / "manifest.json").write_text(json.dumps(replacement), encoding="utf-8")
            (folder / "workflow.json").write_text(json.dumps(VALID_WORKFLOW), encoding="utf-8")
            registry.scan_folder()
            self.assertTrue(registry.get("flux").enabled)
            self.assertEqual(registry.get("flux").description, "新的轻量版本")
            registry.set_enabled("flux", False)
            registry.scan_folder()
            self.assertFalse(registry.get("flux").enabled)

    def test_install_registers_workflow_and_preserves_existing_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "workflows").mkdir()
            self._existing_workflow(base)
            config = base / "runtime" / "workflow_config.json"
            registry = WorkflowRegistry(config, base / "workflows")
            self.assertEqual(registry.default_workflow_id, "existing")
            source = base / "new-image.json"
            source.write_text(json.dumps(VALID_WORKFLOW), encoding="utf-8")
            app = self._app()

            with mock.patch.object(main_gateway, "BASE_DIR", base):
                result = app._install_workflow_from_path(source)

            self.assertEqual(result["id"], "new-image")
            self.assertTrue((base / "workflows" / "new-image" / "workflow.json").is_file())
            latest = WorkflowRegistry(config, base / "workflows")
            self.assertEqual(latest.default_workflow_id, "existing")
            self.assertIn("example.safetensors", result["dependencies"]["models"][0]["name"])

    def test_install_failure_keeps_previous_registry_and_removes_all_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            (base / "workflows").mkdir()
            self._existing_workflow(base)
            config = base / "runtime" / "workflow_config.json"
            WorkflowRegistry(config, base / "workflows")
            previous = config.read_bytes()
            source = base / "will-fail.json"
            source.write_text(json.dumps(VALID_WORKFLOW), encoding="utf-8")
            app = self._app()

            registry = WorkflowRegistry(config, base / "workflows")
            app._workflow_registry = mock.Mock(return_value=registry)
            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(
                    registry,
                    "_save_unlocked",
                    side_effect=OSError("simulated registry failure"),
                ),
            ):
                with self.assertRaises(OSError):
                    app._install_workflow_from_path(source)

            self.assertEqual(config.read_bytes(), previous)
            self.assertFalse((base / "workflows" / "will-fail").exists())
            self.assertEqual(list((base / "workflows").glob(".importing_*")), [])

    def test_import_rejects_an_unsafe_workflows_root_before_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            source = base / "unsafe-root.json"
            source.write_text(json.dumps(VALID_WORKFLOW), encoding="utf-8")
            app = self._app()

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch(
                    "app.core.workflow_import._is_reparse_point",
                    side_effect=lambda path: Path(path) == workflows,
                ),
            ):
                with self.assertRaises(ValueError):
                    app._install_workflow_from_path(source)

            self.assertEqual(list(workflows.iterdir()), [])

    def test_async_import_returns_immediately_and_serializes_double_click(self):
        app = self._app()
        app._workflow_management_lock = threading.Lock()
        app._workflow_operation_name = ""
        app._footer_label = mock.Mock()
        app._finish_workflow_import = mock.Mock()
        app._run_ui_backend_step = lambda callback, timeout=20: (callback() or True)
        started = threading.Event()
        release = threading.Event()

        def slow_install(_path):
            started.set()
            release.wait(1)
            return {"id": "async", "name": "async", "workflows": []}

        app._install_workflow_from_path = mock.Mock(side_effect=slow_install)

        began = time.monotonic()
        app._run_workflow_import(Path("first.json"))
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 0.1)
        self.assertTrue(started.wait(0.5))

        app._run_workflow_import(Path("second.json"))
        self.assertEqual(app._install_workflow_from_path.call_count, 1)
        release.set()
        self.assertTrue(app._wait_for_workflow_idle(1))
        app._finish_workflow_import.assert_called_once()

    def test_shutdown_waits_for_inflight_workflow_operation(self):
        app = self._app()
        app._workflow_management_lock = threading.Lock()
        app._workflow_management_lock.acquire()

        def release_later():
            time.sleep(0.12)
            app._workflow_management_lock.release()

        threading.Thread(target=release_later, daemon=True).start()
        began = time.monotonic()
        self.assertTrue(app._wait_for_workflow_idle(1))
        self.assertGreaterEqual(time.monotonic() - began, 0.1)

    def test_success_dialog_is_queued_after_short_ui_completion(self):
        app = self._app()
        app._footer_label = mock.Mock()
        app._publish_local_workflows = mock.Mock()
        app._reload_workflows = mock.Mock()
        app._post_to_ui = mock.Mock(return_value=True)
        app._show_workflow_import_result = mock.Mock()

        with mock.patch.object(threading.Thread, "start", lambda _thread: None):
            app._finish_workflow_import(
                {"id": "new", "name": "New", "workflows": [], "default_workflow_id": ""}
            )

        app._post_to_ui.assert_called_once()
        app._show_workflow_import_result.assert_not_called()

    def test_local_import_is_visible_even_when_api_refresh_is_unavailable(self):
        app = self._app()
        app._last_health = {"status": "offline", "workflows": []}
        app._update_workflow_display = mock.Mock()
        app._dashboard_pages = mock.Mock()
        workflows = [{"id": "local_only", "name": "Local only"}]

        app._publish_local_workflows(workflows, "local_only")

        self.assertEqual(app._last_health["workflows"], workflows)
        self.assertEqual(app._last_health["default_workflow_id"], "local_only")
        app._dashboard_pages.refresh.assert_called_once_with(app._last_health)

    def test_enable_change_runs_in_background_and_refreshes_local_state(self):
        app = self._app()
        app._workflow_management_lock = threading.Lock()
        app._workflow_operation_name = ""
        app._footer_label = mock.Mock()
        registry = mock.Mock()
        registry.enabled_workflows = [mock.Mock(), mock.Mock()]
        registry.set_enabled.return_value = True
        registry.workflows = []
        registry.default_workflow_id = "alpha"
        app._workflow_registry = mock.Mock(return_value=registry)
        app._workflow_records_from_registry = mock.Mock(return_value=[])
        app._publish_local_workflows = mock.Mock()
        app._reload_workflows = mock.Mock()
        app._run_ui_backend_step = lambda callback, timeout=20: (callback() or True)

        began = time.monotonic()
        app._set_workflow_enabled("beta", False)

        self.assertLess(time.monotonic() - began, 0.1)
        self.assertTrue(app._wait_for_workflow_idle(1))
        registry.set_enabled.assert_called_once_with("beta", False)
        app._publish_local_workflows.assert_called_once_with([], "alpha")

    def test_destroyed_import_popup_is_not_used_as_error_parent(self):
        app = self._app()
        app._footer_label = mock.Mock()
        popup = mock.Mock()
        popup.winfo_exists.return_value = False

        with mock.patch("app.gui.main_gateway.messagebox.showerror") as showerror:
            app._fail_workflow_operation("导入失败", "bad package", popup)

        self.assertIs(showerror.call_args.kwargs["parent"], app)

    def test_post_to_ui_refuses_new_callbacks_after_shutdown(self):
        app = self._app()
        app._shutting_down = True
        app.after = mock.Mock()

        self.assertFalse(app._post_to_ui(mock.Mock()))
        app.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
