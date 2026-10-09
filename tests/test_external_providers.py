import base64
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core import external_providers as providers
from tests.test_image_compatibility import asgi_request


class ProviderSettingsTests(unittest.TestCase):
    def test_release_policy_excludes_provider_keys_and_dreamina_login(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn("providers\\.local", script)
        self.assertIn("dreamina-cli", script)

    def test_key_is_protected_and_never_returned_in_public_status(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = providers.ProviderSettings(Path(directory))
            profile = settings.update("deepseek", enabled=True, api_key="test-secret")
            self.assertTrue(profile.configured)
            self.assertNotIn("test-secret", settings.path.read_text(encoding="utf-8"))
            self.assertNotIn("test-secret", str(profile.public_status()))
            self.assertEqual(providers.unprotect_text(settings.get("deepseek").protected_key), "test-secret")

    def test_rejects_insecure_api_base_and_ignores_untrusted_cli_path(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = providers.ProviderSettings(Path(directory))
            with self.assertRaises(ValueError):
                settings.update("deepseek", base_url="http://example.com")
            with self.assertRaises(ValueError):
                settings.update("dreamina", cli_path="calc.exe")

    def test_cloud_catalog_is_available_only_after_local_configuration(self):
        from app import server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = providers.ProviderSettings(root)
            before = server._cloud_workflow_payloads(root)
            self.assertFalse(next(item for item in before if item["id"] == "cloud_deepseek")["available"])
            schemas = {item["id"]: item["input_schema"] for item in before}
            self.assertIn("system_prompt", schemas["cloud_deepseek"]["optional"])
            self.assertIn("size", schemas["cloud_seedream"]["optional"])
            self.assertNotIn("ratio", schemas["cloud_seedream"]["optional"])
            self.assertIn("resolution_type", schemas["cloud_dreamina_image"]["optional"])
            settings.update("deepseek", enabled=True, api_key="test-secret")
            after = server._cloud_workflow_payloads(root)
            self.assertTrue(next(item for item in after if item["id"] == "cloud_deepseek")["available"])

    def test_dreamina_child_environment_is_isolated_from_gateway_secrets(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(providers.os.environ, {"LOCAL_ADMIN_KEY": "admin-secret",
                                                    "APPDATA": "C:\\OtherProject\\Roaming"}):
            env = providers.dreamina_environment(Path(directory))
            self.assertNotIn("LOCAL_ADMIN_KEY", env)
            self.assertTrue(env["APPDATA"].startswith(directory))
            self.assertNotIn("OtherProject", env["APPDATA"])

    def test_dreamina_requires_local_binding_marker_before_listing_as_available(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(providers, "resolve_dreamina_cli", return_value="dreamina.exe"):
            root = Path(directory)
            settings = providers.ProviderSettings(root)
            settings.update("dreamina", enabled=True)
            self.assertFalse(settings.get("dreamina").configured)
            providers._confirm_dreamina_binding(root)
            self.assertTrue(settings.get("dreamina").configured)

    def test_dreamina_binding_waits_for_confirmed_authorization(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(providers, "_cli_call", side_effect=["pending", "login success"]):
            root = Path(directory)
            profile = providers.ProviderProfile("dreamina", "即梦", "", "")
            self.assertFalse(providers.finish_dreamina_login(profile, root, "device-code"))
            self.assertFalse(providers._dreamina_binding_marker(root).exists())
            self.assertTrue(providers.finish_dreamina_login(profile, root, "device-code"))
            self.assertTrue(providers._dreamina_binding_marker(root).exists())


class ProviderExecutionTests(unittest.TestCase):
    def test_external_media_is_saved_under_existing_output_directory(self):
        from app import server
        class Response:
            status_code = 200
            headers = {"Content-Type": "application/octet-stream"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def iter_content(self, _size):
                yield b"\x89PNG\r\n\x1a\n" + b"fixture"
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(server, "_outputs_dir", return_value=Path(directory)), \
             mock.patch.object(server.socket, "getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]), \
             mock.patch("requests.get", return_value=Response()):
            output = server._store_provider_outputs("task_one", [
                {"type": "image", "external_url": "https://cdn.example/result"}], threading.Event())
            self.assertEqual(output[0]["filename"], "external-1.png")
            self.assertTrue((Path(directory) / "task_one" / "external-1.png").is_file())

    def test_deepseek_request_uses_saved_key_and_expected_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = providers.ProviderSettings(root).update("deepseek", enabled=True, api_key="secret")
            with mock.patch.object(providers, "_json_request", return_value={
                "choices": [{"message": {"content": "生成结果"}}]
            }) as request:
                output = providers.execute_provider(profile, {"prompt": "测试"}, root, threading.Event())
            self.assertEqual(output, [{"type": "text", "text": "生成结果"}])
            self.assertEqual(request.call_args.args[1], "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.call_args.args[2], "secret")

    def test_seedream_image_reference_is_forwarded_without_gateway_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = providers.ProviderSettings(root).update("seedream", enabled=True,
                                                                 model_id="seedream-test", api_key="secret")
            with mock.patch.object(providers, "_json_request", return_value={
                "data": [{"url": "https://example.com/result.png"}]
            }) as request:
                output = providers.execute_provider(profile, {"prompt": "测试", "image": "https://example.com/in.png"}, root)
            self.assertEqual(output[0]["type"], "image")
            self.assertEqual(request.call_args.args[3]["image"], "https://example.com/in.png")

    def test_dreamina_input_uses_isolated_file_and_cli_args(self):
        png = b"\x89PNG\r\n\x1a\n" + b"fixture"
        data_url = "data:image/png;base64," + base64.b64encode(png).decode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = providers.ProviderProfile("dreamina", "即梦", "", "", enabled=True, cli_bound=True)
            with mock.patch.object(providers, "resolve_dreamina_cli", return_value="dreamina.exe"), \
                 mock.patch.object(providers, "_cli_call", side_effect=[
                     '{"submit_id":"abc","gen_status":"querying"}',
                     '{"submit_id":"abc","gen_status":"success","image_url":"https://example.com/out.png"}',
                 ]) as call:
                no_cancel = SimpleNamespace(is_set=lambda: False, wait=lambda _seconds: False)
                output = providers.execute_provider(profile, {"prompt": "猫", "kind": "image", "image": data_url}, root, no_cancel)
            self.assertEqual(output[0]["external_url"], "https://example.com/out.png")
            self.assertEqual(call.call_args_list[0].args[2][0], "image2image")
            self.assertFalse(list(root.glob("dreamina-input-*")))

    def test_dreamina_parser_handles_pretty_json(self):
        text = 'task submitted\n{\n  "submit_id": "abc",\n  "gen_status": "success",\n  "video_url": "https://example.com/video.mp4"\n}'
        self.assertEqual(providers._cli_result(text, "video"),
                         ("abc", "success", "https://example.com/video.mp4"))


class ProviderApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_key_can_generate_but_cannot_read_provider_secret(self):
        from app import server
        from app.config import Config
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = Config(base)
            settings = providers.ProviderSettings(config.runtime_dir)
            settings.update("deepseek", enabled=True, api_key="provider-secret")
            settings.update("seedance", enabled=True, api_key="provider-secret", model_id="seedance-test")
            fake_state = SimpleNamespace(api_key="gateway-key", admin_key="admin-key",
                                         local_api="http://127.0.0.1:18188", base_url="",
                                         session_id="test", set_offline=lambda: None)
            with mock.patch.object(server, "BASE_DIR", base), \
                 mock.patch.object(server, "config", config), \
                 mock.patch.object(server, "state", fake_state), \
                 mock.patch.object(server, "tunnel", None), \
                 mock.patch.object(server, "execute_provider", return_value=[{"type": "text", "text": "回答"}]):
                app = server.create_app()
                with server._task_lock:
                    server.task_records.clear()
                    server.current_task.clear()
                code, _, raw = await asgi_request(app, "GET", "/v1/providers",
                                                   headers={"authorization": "Bearer gateway-key"})
                self.assertEqual(code, 200)
                self.assertNotIn(b"provider-secret", raw)
                self.assertNotIn(b"api_key", raw)
                status, _, body = await asgi_request(app, "POST", "/v1/chat/completions",
                                                     headers={"authorization": "Bearer gateway-key"},
                                                     json_body={"model": "cloud_deepseek", "messages": [{"role": "user", "content": "你好"}]})
                self.assertEqual(status, 200, body.decode())
                self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "回答")
                status, _, body = await asgi_request(app, "POST", "/v1/videos/generations",
                                                     headers={"authorization": "Bearer gateway-key"},
                                                     json_body={"model": "seedance-test", "prompt": "猫跑起来"})
                self.assertEqual(status, 202, body.decode())
                video_task_id = json.loads(body)["task_id"]
                for _ in range(100):
                    if server.task_records.get(video_task_id, {}).get("status") == "completed":
                        break
                    await asyncio.sleep(0.01)

    async def test_cloud_tasks_share_queue_and_pending_task_can_be_cancelled(self):
        from app import server
        from app.config import Config
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = Config(base)
            providers.ProviderSettings(config.runtime_dir).update("deepseek", enabled=True, api_key="secret")
            fake_state = SimpleNamespace(api_key="gateway-key", admin_key="admin-key",
                                         local_api="http://127.0.0.1:18188", base_url="",
                                         session_id="test", set_offline=lambda: None)
            entered, release = threading.Event(), threading.Event()
            def slow_provider(_profile, _body, _runtime_dir, _cancel):
                entered.set()
                release.wait(5)
                return [{"type": "text", "text": "完成"}]
            with mock.patch.object(server, "BASE_DIR", base), \
                 mock.patch.object(server, "config", config), \
                 mock.patch.object(server, "state", fake_state), \
                 mock.patch.object(server, "tunnel", None), \
                 mock.patch.object(server, "execute_provider", side_effect=slow_provider):
                app = server.create_app()
                headers = {"authorization": "Bearer gateway-key"}
                try:
                    code, _, first_raw = await asgi_request(app, "POST", "/v1/providers/deepseek/run",
                                                             headers=headers, json_body={"prompt": "一"})
                    self.assertEqual(code, 200)
                    self.assertTrue(entered.wait(2))
                    code, _, second_raw = await asgi_request(app, "POST", "/v1/providers/deepseek/run",
                                                              headers=headers, json_body={"prompt": "二"})
                    second = json.loads(second_raw)
                    self.assertEqual(code, 200)
                    self.assertEqual(second["queue_position"], 1)
                    code, _, cancel_raw = await asgi_request(app, "POST",
                        f"/v1/tasks/{second['task_id']}/cancel", headers=headers)
                    self.assertEqual(code, 200, cancel_raw.decode())
                    self.assertEqual(json.loads(cancel_raw)["status"], "cancelled")
                finally:
                    release.set()
                    first_id = json.loads(first_raw)["task_id"]
                    for _ in range(100):
                        if server.task_records.get(first_id, {}).get("status") == "completed":
                            break
                        await asyncio.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
