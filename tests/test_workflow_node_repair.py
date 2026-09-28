import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from app.core.workflow_node_repair import install_workflow_nodes, node_inventory
from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp

class NodeRepairTests(unittest.TestCase):
    def test_inventory_ignores_workflow_install_instructions_and_handles_subgraphs(self):
        inventory = node_inventory({"nodes":[{"type":"Missing", "properties":{"url":"https://evil.invalid/repo"}}],
                                    "definitions":{"subgraphs":[{"nodes":[{"type":"Nested"}]}]}})
        self.assertEqual([n["type"] for n in inventory["nodes"]], ["Missing", "Nested"])
        self.assertNotIn("evil", json.dumps(inventory))

    def test_cli_install_uses_generated_dependencies_and_retries_existing_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp) / "ComfyUI"
            manager = core / "custom_nodes" / "ComfyUI-Manager"
            manager.mkdir(parents=True)
            (manager / "cm-cli.py").write_text("", encoding="utf-8")
            (core / "custom_nodes" / "Known").mkdir()
            commands = []
            def run(role, args, **kwargs):
                commands.append(args)
                if "-c" in args:
                    return SimpleNamespace(returncode=0, stdout='{"torch":"2.8.0"}', stderr="")
                if "deps-in-workflow" in args:
                    Path(args[args.index("--output")+1]).write_text(json.dumps({
                        "custom_nodes":{"https://github.com/example/Known":{"state":"installed"}},
                        "unknown_nodes":["Unresolved"]}), encoding="utf-8")
                self.assertTrue(Path(kwargs["env"]["PIP_CONSTRAINT"]).is_file())
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            supervisor = SimpleNamespace(run=run)
            with mock.patch("app.core.workflow_node_repair.shutil.which", return_value="git"):
                unknown = install_workflow_nodes(core, Path(tmp)/"pythonw.exe", {"nodes":[]}, supervisor, {}, lambda s: None)
            self.assertEqual(unknown, ["Unresolved"])
            self.assertTrue(any("install-deps" in args for args in commands))
            self.assertTrue(any("post-install" in args for args in commands))
            self.assertTrue(all(Path(args[0]).name == "python.exe" for args in commands))

    def _app(self):
        app = object.__new__(GatewayApp)
        app._workflow_registry = mock.Mock(return_value=SimpleNamespace(get=lambda key: SimpleNamespace(folder=Path("workflow"))))
        app._load_json_file = mock.Mock(return_value={"nodes":[]})
        app._has_active_model_transfers = lambda: False
        app._begin_backend_action = mock.Mock(return_value=True)
        app._reserve_runtime_maintenance = mock.Mock(return_value=(True, {"api","comfyui"}, ""))
        app._comfyui_update_live_queue_reason = mock.Mock(return_value="")
        app._stop_api_submission_for_comfyui_update = mock.Mock(return_value="")
        app._process_supervisor = mock.Mock()
        app._process_supervisor.terminate.return_value = ""
        app._backend_env = mock.Mock(return_value=("python.exe", {}))
        app._shutting_down = False
        app._run_ui_backend_step = lambda action: action()
        app._start_comfyui_service_unlocked = mock.Mock()
        app._retry_pending_workflow = mock.Mock(return_value={"id":"same"})
        app._end_runtime_maintenance = mock.Mock()
        app._end_backend_action = mock.Mock()
        return app

    def test_queue_blocks_install_and_does_not_stop_services(self):
        app = self._app()
        app._comfyui_update_live_queue_reason.return_value = "有生成任务"
        with self.assertRaisesRegex(ValueError, "有生成任务"):
            app._repair_workflow_automatically("id", lambda s: None)
        app._stop_api_submission_for_comfyui_update.assert_not_called()
        app._process_supervisor.terminate.assert_not_called()
        app._end_runtime_maintenance.assert_called_once_with(restart=False)

    def test_failed_install_restores_services_and_keeps_pending_workflow(self):
        app = self._app()
        with mock.patch("app.core.workflow_node_repair.install_workflow_nodes", side_effect=ValueError("install failed")):
            with self.assertRaisesRegex(ValueError, "install failed"):
                app._repair_workflow_automatically("id", lambda s: None)
        app._retry_pending_workflow.assert_not_called()
        app._end_runtime_maintenance.assert_called_once_with(restart=True)
        app._end_backend_action.assert_called_once()

    def test_success_restarts_then_retries_same_workflow(self):
        app = self._app()
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch("app.core.workflow_node_repair.install_workflow_nodes", return_value=[]), mock.patch("urllib.request.urlopen", return_value=response):
            result = app._repair_workflow_automatically("id", lambda s: None)
        app._start_comfyui_service_unlocked.assert_called_once()
        app._retry_pending_workflow.assert_called_once_with("id")
        self.assertEqual(result, {"id":"same"})
        app._end_runtime_maintenance.assert_called_once_with(restart=True)

if __name__ == "__main__":
    unittest.main()
