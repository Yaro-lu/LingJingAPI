import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp
from app.core.workflow_adaptation import analyze_workflow, mapping_fields
from app.workflow_registry import WorkflowRegistry
from test_workflow_adaptation import example_graph


class RecognitionCallbackTests(unittest.TestCase):
    def app(self):
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        return app

    def ready_status(self):
        return {"runtime": {"status": "installed"}, "comfyui": {"status": "online"},
                "current_task": None, "models": {"status": "incomplete"},
                "workflows": [{"id": "llm_qwen3_text_gen", "available": True, "missing_models": [], "missing_nodes": []}]}

    def callback_app(self):
        app = self.app()
        app._workflow_registry = mock.Mock(return_value=SimpleNamespace(resolve=lambda _: SimpleNamespace(id="llm_qwen3_text_gen")))
        app._local_api_headers = mock.Mock(return_value={"Authorization": "Bearer test-only"})
        return app

    def test_missing_or_partial_model_and_runtime_skip_all_network_calls(self):
        app = self.callback_app()
        for model_ready, missing_runtime, message in [(False, [], "模型未部署完整"), (True, ["runtime/python"], "运行环境未安装完整")]:
            with self.subTest(message=message), mock.patch.object(main_gateway, "_models_dir", return_value=Path("isolated")), \
                 mock.patch.object(main_gateway, "_model_file_ready", return_value=model_ready), \
                 mock.patch.object(main_gateway, "missing_runtime_paths", return_value=missing_runtime), \
                 mock.patch.object(main_gateway.ur, "urlopen") as network:
                with self.assertRaisesRegex(ValueError, message):
                    app._call_workflow_recognition_llm("analyze")
                network.assert_not_called()

    def test_unready_fresh_status_never_submits_inference(self):
        app = self.callback_app()
        app._last_health = self.ready_status()  # A stale ready snapshot must not bypass the check.
        cases = []
        for key, value, message in [("runtime", {"status":"missing"}, "运行环境未就绪"),
                                    ("comfyui", {"status":"offline"}, "ComfyUI 未启动"),
                                    ("current_task", {"status":"running"}, "正在执行任务"),
                                    ("workflows", [], "尚未加载")]:
            status = self.ready_status(); status[key] = value; cases.append((status, message))
        for key, value, message in [("missing_models", ["qwen.safetensors"], "缺少模型"),
                                    ("missing_nodes", ["TextGenerate"], "缺少节点"),
                                    ("available", False, "工作流不可用")]:
            status = self.ready_status(); status["workflows"][0][key] = value; cases.append((status, message))
        for status, message in cases:
            response = mock.MagicMock(); response.__enter__.return_value = response
            response.read.return_value = json.dumps(status).encode()
            with self.subTest(message=message), mock.patch.object(main_gateway, "_models_dir", return_value=Path("isolated")), \
                 mock.patch.object(main_gateway, "_model_file_ready", return_value=True), \
                 mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]), \
                 mock.patch.object(main_gateway.ur, "urlopen", return_value=response) as network:
                with self.assertRaisesRegex(ValueError, message):
                    app._call_workflow_recognition_llm("analyze")
                network.assert_called_once()
                self.assertEqual(network.call_args.args[0].method, "GET")
                self.assertEqual(network.call_args.kwargs["timeout"], 5)

    def test_status_timeout_or_invalid_json_returns_to_manual_without_post(self):
        app = self.callback_app()
        for failure in (TimeoutError("timeout"), ValueError("invalid JSON")):
            with self.subTest(failure=failure), mock.patch.object(main_gateway, "_models_dir", return_value=Path("isolated")), \
                 mock.patch.object(main_gateway, "_model_file_ready", return_value=True), \
                 mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]), \
                 mock.patch.object(main_gateway.ur, "urlopen", side_effect=failure) as network:
                with self.assertRaisesRegex(ValueError, "请手动填写参数"):
                    app._call_workflow_recognition_llm("analyze")
                self.assertEqual(network.call_count, 1)
                self.assertEqual(network.call_args.args[0].method, "GET")

    def test_local_text_callback_pins_model_and_checks_returned_identity(self):
        app = self.app()
        app._workflow_registry = mock.Mock(return_value=SimpleNamespace(resolve=lambda _: SimpleNamespace(id="llm_qwen3_text_gen")))
        app._local_api_headers = mock.Mock(return_value={"Authorization": "Bearer test-only"})
        response = mock.MagicMock()
        response.__enter__.return_value = response
        payload = {"workflow": "llm_qwen3_text_gen", "choices": [{"message": {"content": '{"fields":[]}'}}]}
        response.read.return_value = json.dumps(payload).encode()
        def respond(request, **kwargs):
            reply = mock.MagicMock(); reply.__enter__.return_value = reply
            reply.read.return_value = json.dumps(self.ready_status() if request.method == "GET" else payload).encode()
            return reply
        with mock.patch.object(main_gateway, "_models_dir", return_value=Path("isolated")), \
             mock.patch.object(main_gateway, "_model_file_ready", return_value=True), \
             mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]), \
             mock.patch.object(main_gateway.ur, "urlopen", side_effect=respond) as call:
            self.assertEqual(app._call_workflow_recognition_llm("analyze"), '{"fields":[]}')
            self.assertEqual([c.args[0].method for c in call.call_args_list], ["GET", "POST"])
            self.assertTrue(call.call_args_list[0].args[0].full_url.endswith("/v1/status"))
            request = call.call_args.args[0]
            self.assertTrue(request.full_url.startswith("http://127.0.0.1:"))
            self.assertEqual(json.loads(request.data)["model"], "llm_qwen3_text_gen")
        payload["workflow"] = "another-model"
        response.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(app, "_check_workflow_recognition_ready"), mock.patch.object(main_gateway.ur, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "其他工作流"):
                app._call_workflow_recognition_llm("analyze")

    def test_rules_failure_calls_llm_once_then_returns_manual_error(self):
        app = self.app()
        app._call_workflow_recognition_llm = mock.Mock(side_effect=ValueError("model missing"))
        graph = {"1": {"class_type": "Unknown", "inputs": {"query": "hello"}}, "2": {"class_type": "SaveImage", "inputs": {}}}
        with mock.patch.object(main_gateway.ur, "urlopen", side_effect=OSError("offline")):
            result = app._recognize_workflow(graph)
        app._call_workflow_recognition_llm.assert_called_once()
        self.assertEqual(result["api_mapping_status"], "needs_review")
        self.assertIn("手动填写", result["api_mapping_error"])

    def test_rules_success_does_not_call_model_or_metadata(self):
        app = self.app()
        app._call_workflow_recognition_llm = mock.Mock()
        with mock.patch.object(main_gateway.ur, "urlopen") as metadata:
            result = app._recognize_workflow(example_graph())
        self.assertEqual(result["api_mapping_status"], "ready")
        metadata.assert_not_called()
        app._call_workflow_recognition_llm.assert_not_called()

    def test_failed_import_offers_manual_editor_without_running_a_window(self):
        app = self.app()
        app._show_workflow_mapping_editor = mock.Mock()
        result = {"id": "demo", "adaptation": {"api_mapping_status": "needs_review", "api_mapping_error": "无法识别，请手动填写"}}
        with mock.patch.object(main_gateway.messagebox, "showwarning") as warning:
            app._show_workflow_import_result(result)
        warning.assert_called_once()
        app._show_workflow_mapping_editor.assert_called_once_with(result)

    def test_manual_editor_save_callback_validates_and_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "workflows/demo"
            folder.mkdir(parents=True)
            graph = example_graph()
            (folder / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
            (folder / "manifest.json").write_text(json.dumps({"id": "demo", "type": "image.text_to_image"}), encoding="utf-8")
            registry = WorkflowRegistry(root / "runtime/config.json", root / "workflows")
            app = self.app()
            app._workflow_registry = mock.Mock(return_value=registry)
            app._publish_local_workflows = mock.Mock()
            app._reload_workflows_and_sync = mock.Mock()
            actions = {}
            app._button = mock.Mock(side_effect=lambda parent, label, command, style: actions.setdefault(label, command) and mock.MagicMock())
            editor, reference = mock.MagicMock(), mock.MagicMock()
            editor.get.return_value = json.dumps({"output_type": "image", "fields": mapping_fields(analyze_workflow(graph))})
            with (
                mock.patch.object(main_gateway.tk, "Toplevel") as popup,
                mock.patch.object(main_gateway.tk, "Label"),
                mock.patch.object(main_gateway.tk, "Text", side_effect=[editor, reference]),
                mock.patch.object(main_gateway.threading, "Thread"),
            ):
                app._show_workflow_mapping_editor({"id": "demo"})
                actions["校验并保存"]()
                popup.return_value.destroy.assert_called_once()
            self.assertEqual(registry.get("demo").api_mapping["api_mapping_status"], "ready")
            app._publish_local_workflows.assert_called_once()


if __name__ == "__main__":
    unittest.main()
