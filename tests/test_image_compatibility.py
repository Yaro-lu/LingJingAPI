import asyncio
import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import server


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def asgi_request(app, method, path, *, headers=None, json_body=None):
    request_headers = {"host": "client.example", **(headers or {})}
    body = b""
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
        request_headers.setdefault("content-length", str(len(body)))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("client.example", 443),
    }
    sent = []
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    response_body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return start["status"], response_headers, response_body


class FakeRegistry:
    workflow_defs = []

    def __init__(self, *args, **kwargs):
        self.workflows = list(self.workflow_defs)
        self.default_workflow_id = self.workflows[0].id

    def scan_folder(self):
        return []

    def load(self):
        return None

    @property
    def enabled_workflows(self):
        return [workflow for workflow in self.workflows if getattr(workflow, "enabled", True)]

    def resolve(self, workflow_id=None):
        if workflow_id in (None, ""):
            return self.workflows[0]
        for workflow in self.workflows:
            if workflow.id == workflow_id:
                return workflow
        return None


class FakeComfyUIClient:
    release = threading.Event()
    started = threading.Event()
    queue_entered = threading.Event()
    queue_release = threading.Event()
    block_queue = False
    fail_queue = False
    uploaded_images = []
    queued_workflows = []

    def __init__(self, *args, **kwargs):
        pass

    def queue_prompt(self, workflow_data):
        self.queued_workflows.append(json.loads(json.dumps(workflow_data)))
        self.queue_entered.set()
        if self.block_queue:
            self.queue_release.wait(timeout=5)
        if self.fail_queue:
            raise OSError("fixture queue unavailable")
        if any(
            node.get("class_type") == "TextOutput"
            for node in workflow_data.values()
        ):
            return "prompt-text-test"
        return "prompt-image-test"

    def upload_input_image(self, image_data, filename, mime_type):
        self.uploaded_images.append(
            {"image_data": image_data, "filename": filename, "mime_type": mime_type}
        )
        return f"api-input/{filename}"

    def get_progress(self, prompt_id, **kwargs):
        self.started.set()
        self.release.wait(timeout=5)
        return {
            "status": "completed",
            "value": 1,
            "max": 1,
            "percent": 100,
            "phase": "completed",
            "label": "completed",
        }

    def get_history(self, prompt_id):
        return {prompt_id: {"kind": "text" if "text" in prompt_id else "image"}}

    def get_output_files(self, history):
        if history.get("kind") == "text":
            return [{"type": "text", "text": "OK"}]
        return [{"filename": "generated.png", "type": "output"}]


class ImageCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        (self.base / "outputs").mkdir(parents=True)
        (self.base / "runtime" / "requests").mkdir(parents=True)
        (self.base / "runtime" / "logs").mkdir(parents=True)
        workflow_dir = self.base / "workflows" / "flux_t2i_v1"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "1": {
                        "class_type": "SaveImage",
                        "inputs": {"filename_prefix": "test"},
                    }
                }
            ),
            encoding="utf-8",
        )
        image_workflow = SimpleNamespace(
            id="flux_t2i_v1",
            name="flux2",
            folder=workflow_dir,
            output_type="image",
            enabled=True,
            description="",
        )
        text_workflow_dir = self.base / "workflows" / "llm_qwen3_text_gen"
        text_workflow_dir.mkdir(parents=True)
        (text_workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "1": {
                        "class_type": "TextOutput",
                        "inputs": {"prompt": ""},
                    }
                }
            ),
            encoding="utf-8",
        )
        text_workflow = SimpleNamespace(
            id="llm_qwen3_text_gen",
            name="qwen3.5",
            folder=text_workflow_dir,
            output_type="text",
            enabled=True,
            description="",
        )
        video_workflow_dir = self.base / "workflows" / "wan_flf2v_v1"
        video_workflow_dir.mkdir(parents=True)
        (video_workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "5": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": "默认负面提示词"},
                        "_meta": {"title": "CLIP Text Encode (Negative Prompt)"},
                    },
                    "9": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "start_frame.png", "upload": "image"},
                        "_meta": {"title": "Load Start Frame"},
                    },
                    "10": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "end_frame.png", "upload": "image"},
                        "_meta": {"title": "Load End Frame"},
                    },
                    "14": {
                        "class_type": "WanVaceToVideo",
                        "inputs": {"length": 49, "width": 480, "height": 832},
                    },
                    "19": {
                        "class_type": "RepeatImageBatch",
                        "inputs": {"image": ["9", 0], "amount": 47},
                    },
                    "17": {
                        "class_type": "CreateVideo",
                        "inputs": {"fps": 16.0},
                    },
                }
            ),
            encoding="utf-8",
        )
        video_workflow = SimpleNamespace(
            id="wan_flf2v_v1",
            name="wan2.1",
            folder=video_workflow_dir,
            output_type="video",
            enabled=True,
            description="",
        )
        ltx_workflow_dir = self.base / "workflows" / "ltx2_3_flf2v_v1"
        ltx_workflow_dir.mkdir(parents=True)
        (ltx_workflow_dir / "workflow.json").write_text(
            json.dumps(
                {
                    "1": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "start_frame.png"},
                        "_meta": {"title": "Load First Frame"},
                    },
                    "2": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "end_frame.png"},
                        "_meta": {"title": "Load Last Frame"},
                    },
                    "3": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": "默认提示词"},
                        "_meta": {"title": "CLIP Text Encode (Positive Prompt)"},
                    },
                    "4": {
                        "class_type": "RandomNoise",
                        "inputs": {"noise_seed": 0},
                    },
                    "5": {
                        "class_type": "ImageScale",
                        "inputs": {"width": 1280, "height": 720},
                    },
                    "6": {
                        "class_type": "EmptyLTXVLatentVideo",
                        "inputs": {
                            "width": 1280,
                            "height": 720,
                            "length": 126,
                        },
                    },
                    "7": {
                        "class_type": "LTXVEmptyLatentAudio",
                        "inputs": {"frames_number": 126, "frame_rate": 24},
                    },
                    "8": {
                        "class_type": "LTXVConditioning",
                        "inputs": {"frame_rate": 24},
                    },
                    "9": {
                        "class_type": "CreateVideo",
                        "inputs": {"fps": 25.0},
                    },
                }
            ),
            encoding="utf-8",
        )
        ltx_workflow = SimpleNamespace(
            id="ltx2_3_flf2v_v1",
            name="LTX-2.3",
            folder=ltx_workflow_dir,
            output_type="video",
            enabled=True,
            description="",
        )
        FakeRegistry.workflow_defs = [
            image_workflow,
            text_workflow,
            video_workflow,
            ltx_workflow,
        ]
        FakeComfyUIClient.release.clear()
        FakeComfyUIClient.started.clear()
        FakeComfyUIClient.queue_entered.clear()
        FakeComfyUIClient.queue_release.clear()
        FakeComfyUIClient.block_queue = False
        FakeComfyUIClient.fail_queue = False
        FakeComfyUIClient.uploaded_images.clear()
        FakeComfyUIClient.queued_workflows.clear()
        self.fake_state = SimpleNamespace(
            api_key="sk-test-image",
            admin_key="sk-admin-test",
            session_id="session-test",
            base_url="https://client.example",
            local_api="http://127.0.0.1:18188",
            set_offline=lambda: None,
        )
        self.fake_config = SimpleNamespace(
            base_dir=self.base,
            models_dir=self.base / "models",
            outputs_dir=self.base / "outputs",
            directory=lambda name: self.base / "runtime" / "logs" if name == "logs" else self.base / name,
            workflows_dir=self.base / "workflows",
            requests_dir=self.base / "runtime" / "requests",
            logs_dir=self.base / "runtime" / "logs",
            runtime_dir=self.base / "runtime",
            comfyui_url="http://127.0.0.1:8188",
            server_port=18188,
        )
        self.patchers = [
            mock.patch.object(server, "BASE_DIR", self.base),
            mock.patch.object(server, "config", self.fake_config),
            mock.patch.object(server, "state", self.fake_state),
            mock.patch.object(server, "tunnel", None),
            mock.patch.object(server, "WorkflowRegistry", FakeRegistry),
            mock.patch.object(server, "ComfyUIClient", FakeComfyUIClient),
        ]
        for patcher in self.patchers:
            patcher.start()
        with server._task_lock:
            server.current_task.clear()
            server.task_records.clear()
        with server._comfy_nodes_lock:
            server._comfy_nodes_cache.update(
                {"url": "", "checked_at": 0.0, "nodes": None}
            )
        self.app = server.create_app()
        self.auth = {"authorization": "Bearer sk-test-image"}
        self.admin_auth = {"authorization": "Bearer sk-admin-test"}

    async def asyncTearDown(self):
        FakeComfyUIClient.release.set()
        FakeComfyUIClient.queue_release.set()
        await asyncio.sleep(0.05)
        with server._task_lock:
            server.current_task.clear()
            server.task_records.clear()
        with server._comfy_nodes_lock:
            server._comfy_nodes_cache.update(
                {"url": "", "checked_at": 0.0, "nodes": None}
            )
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _image_body(**extra):
        return {
            "model": "doubao-seedream-5-0-260128",
            "prompt": "一枚紫色水晶立方体",
            "size": "1024x1024",
            **extra,
        }

    async def test_prefer_respond_async_returns_task_contract_immediately(self):
        release_timer = threading.Timer(0.8, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        status, headers, body = await asgi_request(
            self.app,
            "POST",
            "/api/v3/images/generations",
            headers={**self.auth, "prefer": "respond-async"},
            json_body=self._image_body(),
        )
        elapsed = time.perf_counter() - started
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(payload["id"], payload["task_id"])
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(payload["workflow_id"], "flux_t2i_v1")
        self.assertEqual(payload["data"], [])
        self.assertEqual(headers["location"], payload["status_path"])
        self.assertEqual(headers["retry-after"], "3")
        self.assertEqual(headers["preference-applied"], "respond-async")
        FakeComfyUIClient.release.set()
        release_timer.cancel()

    async def test_body_async_true_returns_task_contract(self):
        release_timer = threading.Timer(0.8, FakeComfyUIClient.release.set)
        release_timer.start()
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/api/v3/images/generations",
            headers=self.auth,
            json_body=self._image_body(**{"async": True}),
        )
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "submitted")
        self.assertTrue(payload["status_url"].endswith(payload["status_path"]))
        FakeComfyUIClient.release.set()
        release_timer.cancel()

    async def test_direct_workflow_call_preserves_its_save_image_prefix(self):
        status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/flux_t2i_v1",
            headers=self.auth,
            json_body={"prompt": "保留工作流自己的输出名称"},
        )

        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["1"]["inputs"]["filename_prefix"], "test")

    async def test_explicit_filename_prefix_overrides_workflow_default(self):
        status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/flux_t2i_v1",
            headers=self.auth,
            json_body={
                "prompt": "调用方指定输出名称",
                "filename_prefix": "caller-prefix",
            },
        )

        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["1"]["inputs"]["filename_prefix"], "caller-prefix")

    async def test_video_request_uploads_frames_and_injects_real_load_image_names(self):
        frame_data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/wan_flf2v_v1",
            headers=self.auth,
            json_body={
                "prompt": "镜头缓慢推进",
                "start_image": frame_data_url,
                "end_image": frame_data_url,
                "duration": 5,
                "seed": 7,
            },
        )
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["workflow_id"], "wan_flf2v_v1")
        self.assertEqual(payload["status_path"], f"/v1/tasks/{payload['task_id']}")
        self.assertEqual(len(FakeComfyUIClient.uploaded_images), 2)
        self.assertTrue(all(item["image_data"] == PNG_BYTES for item in FakeComfyUIClient.uploaded_images))
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertTrue(queued["9"]["inputs"]["image"].startswith("api-input/lingjing_task_"))
        self.assertTrue(queued["10"]["inputs"]["image"].startswith("api-input/lingjing_task_"))
        self.assertEqual(queued["14"]["inputs"]["length"], 81)
        self.assertEqual(queued["19"]["inputs"]["amount"], 79)
        self.assertEqual(queued["14"]["inputs"]["width"], 480)
        self.assertEqual(queued["14"]["inputs"]["height"], 832)
        self.assertEqual(queued["17"]["inputs"]["fps"], 16.0)
        self.assertEqual(queued["5"]["inputs"]["text"], "默认负面提示词")
        request_record = next(self.fake_config.requests_dir.glob("task_*.json"))
        self.assertNotIn("data:image", request_record.read_text(encoding="utf-8"))

    async def test_vace_video_duration_keeps_latent_and_transition_batches_aligned(self):
        frame_data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/wan_flf2v_v1",
            headers=self.auth,
            json_body={
                "prompt": "镜头缓慢横移",
                "start_image": frame_data_url,
                "end_image": frame_data_url,
                "duration": 8,
                "seed": 11,
                "negative_prompt": "自定义负面提示词",
            },
        )

        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["14"]["inputs"]["length"], 129)
        self.assertEqual(queued["19"]["inputs"]["amount"], 127)
        self.assertEqual(queued["17"]["inputs"]["fps"], 16.0)
        self.assertEqual(queued["5"]["inputs"]["text"], "自定义负面提示词")

    async def test_ltx_duration_and_fps_keep_video_and_audio_latents_aligned(self):
        frame_data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/ltx2_3_flf2v_v1",
            headers=self.auth,
            json_body={
                "prompt": "镜头从首帧平滑过渡到尾帧",
                "start_image": frame_data_url,
                "end_image": frame_data_url,
                "duration": 4,
                "fps": 20,
                "width": 960,
                "height": 544,
                "seed": 13,
            },
        )

        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["6"]["inputs"]["length"], 81)
        self.assertEqual(queued["7"]["inputs"]["frames_number"], 81)
        self.assertEqual(queued["7"]["inputs"]["frame_rate"], 20)
        self.assertEqual(queued["8"]["inputs"]["frame_rate"], 20)
        self.assertEqual(queued["9"]["inputs"]["fps"], 20)
        self.assertEqual(queued["5"]["inputs"]["width"], 960)
        self.assertEqual(queued["5"]["inputs"]["height"], 544)
        self.assertEqual(queued["6"]["inputs"]["width"], 960)
        self.assertEqual(queued["6"]["inputs"]["height"], 544)
        self.assertEqual(queued["3"]["inputs"]["text"], "镜头从首帧平滑过渡到尾帧")
        self.assertEqual(queued["4"]["inputs"]["noise_seed"], 13)

    async def test_ltx_explicit_frames_override_duration_and_reuse_workflow_fps(self):
        frame_data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/ltx2_3_flf2v_v1",
            headers=self.auth,
            json_body={
                "prompt": "固定输出帧数",
                "start_image": frame_data_url,
                "end_image": frame_data_url,
                "duration": 9,
                "frames": 130,
            },
        )

        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["6"]["inputs"]["length"], 130)
        self.assertEqual(queued["7"]["inputs"]["frames_number"], 130)
        self.assertEqual(queued["7"]["inputs"]["frame_rate"], 25)
        self.assertEqual(queued["8"]["inputs"]["frame_rate"], 25)
        self.assertEqual(queued["9"]["inputs"]["fps"], 25)

    def test_frames_validation_accepts_only_one_to_one_thousand(self):
        self.assertEqual(server._validated_generation_body({"frames": 1})["frames"], 1)
        self.assertEqual(server._validated_generation_body({"frames": 1000})["frames"], 1000)
        self.assertNotIn("frames", server._validated_generation_body({"frames": None}))

        for invalid in (0, 1001, True, "not-an-integer"):
            with self.subTest(frames=invalid):
                with self.assertRaises(server.HTTPException) as raised:
                    server._validated_generation_body({"frames": invalid})
                self.assertEqual(raised.exception.status_code, 422)

    def test_filename_prefix_allows_safe_subfolders_and_rejects_path_escape(self):
        validated = server._validated_generation_body(
            {"filename_prefix": "video/我的镜头"}
        )
        self.assertEqual(validated["filename_prefix"], "video/我的镜头")

        for invalid in ("../outside", "/absolute", "C:\\outside", "folder//name"):
            with self.subTest(filename_prefix=invalid):
                with self.assertRaises(server.HTTPException) as raised:
                    server._validated_generation_body({"filename_prefix": invalid})
                self.assertEqual(raised.exception.status_code, 422)

    async def test_video_request_rejects_missing_or_fake_frames_before_upload(self):
        frame_data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        missing_status, _headers, missing_body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/wan_flf2v_v1",
            headers=self.auth,
            json_body={"prompt": "test", "start_image": frame_data_url},
        )
        fake_status, _headers, fake_body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/run/wan_flf2v_v1",
            headers=self.auth,
            json_body={
                "prompt": "test",
                "start_image": "data:image/png;base64," + base64.b64encode(b"not-png").decode("ascii"),
                "end_image": frame_data_url,
            },
        )

        self.assertEqual(missing_status, 422)
        self.assertIn("尾帧", json.loads(missing_body)["detail"])
        self.assertEqual(fake_status, 422)
        self.assertIn("声明格式", json.loads(fake_body)["detail"])
        self.assertEqual(FakeComfyUIClient.uploaded_images, [])

    async def test_root_short_url_runs_current_default_workflow_asynchronously(self):
        status, headers, body = await asgi_request(
            self.app,
            "POST",
            "/",
            headers=self.auth,
            json_body=self._image_body(),
        )
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertEqual(payload["workflow_id"], "flux_t2i_v1")
        self.assertEqual(payload["workflow_name"], "flux2")
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(headers["location"], payload["status_path"])
        self.assertTrue(payload["status_url"].endswith(payload["status_path"]))
        request_files = list(self.fake_config.requests_dir.glob("task_*.json"))
        self.assertEqual(len(request_files), 1)
        saved_request = json.loads(request_files[0].read_text(encoding="utf-8"))
        self.assertNotIn("body", saved_request)
        self.assertEqual(saved_request["parameters"]["width"], 1024)
        self.assertEqual(saved_request["parameters"]["height"], 1024)
        self.assertNotIn("一枚紫色水晶立方体", json.dumps(saved_request, ensure_ascii=False))

    async def test_public_health_is_minimal_and_private_status_requires_key(self):
        health_status, _headers, health_body = await asgi_request(
            self.app, "GET", "/health"
        )
        private_status, _headers, _body = await asgi_request(
            self.app, "GET", "/v1/status"
        )
        health = json.loads(health_body)

        self.assertEqual(health_status, 200)
        self.assertEqual(set(health), {"status", "version"})
        self.assertNotIn("session_id", health)
        self.assertNotIn("current_task", health)
        self.assertEqual(private_status, 401)

    async def test_generation_key_cannot_call_management_endpoint(self):
        forbidden_status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/reload",
            headers=self.auth,
            json_body={},
        )
        admin_status, _headers, admin_body = await asgi_request(
            self.app,
            "POST",
            "/v1/workflows/reload",
            headers=self.admin_auth,
            json_body={},
        )

        self.assertEqual(forbidden_status, 403)
        self.assertEqual(admin_status, 200)
        self.assertTrue(json.loads(admin_body)["ok"])

    async def test_malformed_authorization_bytes_return_401_instead_of_500(self):
        status, _headers, body = await asgi_request(
            self.app,
            "GET",
            "/v1/models",
            headers={"authorization": "Bearer ÿ"},
        )

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "unauthorized")

    async def test_negative_content_length_is_rejected(self):
        status, _headers, body = await asgi_request(
            self.app,
            "GET",
            "/v1/models",
            headers={**self.auth, "content-length": "-1"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_content_length")

    async def test_oversized_body_and_invalid_generation_parameters_are_rejected(self):
        too_large_status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/",
            headers={
                **self.auth,
                "content-length": str(server.MAX_REQUEST_BODY_BYTES + 1),
            },
            json_body={"prompt": "small"},
        )
        invalid_status, _headers, invalid_body = await asgi_request(
            self.app,
            "POST",
            "/",
            headers=self.auth,
            json_body=self._image_body(width=16384, height=16384, steps=999),
        )

        self.assertEqual(too_large_status, 413)
        self.assertEqual(invalid_status, 422)
        self.assertIn("detail", json.loads(invalid_body))

    async def test_queue_failure_releases_task_slot_without_writing_request(self):
        FakeComfyUIClient.fail_queue = True

        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/",
            headers=self.auth,
            json_body=self._image_body(),
        )

        self.assertEqual(status, 502)
        self.assertNotIn("fixture queue unavailable", body.decode("utf-8"))
        self.assertEqual(list(self.fake_config.requests_dir.glob("task_*.json")), [])
        with server._task_lock:
            self.assertEqual(server.current_task, {})

    async def test_concurrent_requests_cannot_both_pass_task_reservation(self):
        FakeComfyUIClient.block_queue = True
        first = asyncio.create_task(
            asgi_request(
                self.app,
                "POST",
                "/",
                headers=self.auth,
                json_body=self._image_body(**{"async": True}),
            )
        )
        entered = await asyncio.to_thread(FakeComfyUIClient.queue_entered.wait, 2)
        self.assertTrue(entered)

        second_status, _headers, _body = await asgi_request(
            self.app,
            "POST",
            "/",
            headers=self.auth,
            json_body=self._image_body(**{"async": True}),
        )
        FakeComfyUIClient.queue_release.set()
        first_status, _headers, _body = await asyncio.wait_for(first, timeout=2)

        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 429)

    async def test_named_short_url_resolves_case_insensitive_workflow_name(self):
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/FLUX2",
            headers=self.auth,
            json_body=self._image_body(),
        )
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertEqual(payload["workflow_id"], "flux_t2i_v1")
        self.assertEqual(payload["requested_workflow"], "FLUX2")

    async def test_qwen_short_url_uses_english_workflow_name(self):
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/qwen3.5",
            headers=self.auth,
            json_body={"prompt": "写一段测试文本"},
        )
        payload = json.loads(body)

        self.assertEqual(status, 202)
        self.assertEqual(payload["workflow_id"], "llm_qwen3_text_gen")
        self.assertEqual(payload["workflow_name"], "qwen3.5")

    async def test_unknown_short_url_returns_not_found(self):
        status, _headers, body = await asgi_request(
            self.app,
            "POST",
            "/not-a-workflow",
            headers=self.auth,
            json_body=self._image_body(),
        )
        payload = json.loads(body)

        self.assertEqual(status, 404)
        self.assertIn("not-a-workflow", payload["detail"])

    async def test_sync_wait_keeps_event_loop_responsive_and_ark_shape(self):
        release_timer = threading.Timer(0.35, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        post_task = asyncio.create_task(
            asgi_request(
                self.app,
                "POST",
                "/api/v3/images/generations",
                headers=self.auth,
                json_body=self._image_body(),
            )
        )
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.perf_counter() - started
        status_status, _headers, status_body = await asyncio.wait_for(
            asgi_request(
                self.app,
                "GET",
                "/v1/tasks/status",
                headers=self.auth,
            ),
            timeout=0.25,
        )
        FakeComfyUIClient.release.set()
        status, _headers, body = await asyncio.wait_for(post_task, timeout=2)
        payload = json.loads(body)

        self.assertLess(heartbeat_elapsed, 0.2)
        self.assertEqual(status_status, 200)
        self.assertIn(json.loads(status_body)["status"], {"pending", "running", "completed"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "doubao-seedream-5-0-260128")
        self.assertEqual(len(payload["data"]), 1)
        self.assertTrue(payload["data"][0]["url"].endswith("/generated.png"))
        self.assertTrue(payload["task_id"].startswith("task_"))
        release_timer.cancel()

    async def test_chat_compatibility_wait_is_nonblocking_and_shape_is_unchanged(self):
        release_timer = threading.Timer(0.35, FakeComfyUIClient.release.set)
        release_timer.start()
        started = time.perf_counter()
        post_task = asyncio.create_task(
            asgi_request(
                self.app,
                "POST",
                "/v1/chat/completions",
                headers=self.auth,
                json_body={
                    "model": "llm_qwen3_text_gen",
                    "messages": [{"role": "user", "content": "只回复 OK"}],
                },
            )
        )
        await asyncio.sleep(0.05)
        heartbeat_elapsed = time.perf_counter() - started
        status_status, _headers, _body = await asyncio.wait_for(
            asgi_request(
                self.app,
                "GET",
                "/v1/tasks/status",
                headers=self.auth,
            ),
            timeout=0.25,
        )
        FakeComfyUIClient.release.set()
        status, _headers, body = await asyncio.wait_for(post_task, timeout=2)
        payload = json.loads(body)

        self.assertLess(heartbeat_elapsed, 0.2)
        self.assertEqual(status_status, 200)
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "OK")
        self.assertNotIn("data", payload)
        release_timer.cancel()

    async def test_completion_waits_until_outputs_are_ready(self):
        entered, release = threading.Event(), threading.Event()
        original = FakeComfyUIClient.get_history
        def slow_history(client, prompt_id):
            entered.set()
            release.wait(timeout=3)
            return original(client, prompt_id)
        with mock.patch.object(FakeComfyUIClient, "get_history", slow_history):
            try:
                status, _, body = await asgi_request(self.app, "POST", "/v1/workflows/run/flux_t2i_v1",
                                                   headers=self.auth, json_body=self._image_body())
                self.assertEqual(status, 200)
                task_id = json.loads(body)["task_id"]
                FakeComfyUIClient.release.set()
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                _, _, response = await asgi_request(self.app, "GET", f"/v1/tasks/{task_id}", headers=self.auth)
                self.assertEqual(json.loads(response)["status"], "running")
            finally:
                release.set()
            for _ in range(100):
                _, _, response = await asgi_request(self.app, "GET", f"/v1/tasks/{task_id}", headers=self.auth)
                record = json.loads(response)
                if record["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(record["status"], "completed")
            self.assertTrue(record["outputs"])

    async def test_completed_task_has_ark_data_and_relative_download_path(self):
        record = {
            "id": "task_done",
            "task_id": "task_done",
            "workflow_id": "flux_t2i_v1",
            "status": "completed",
            "outputs": [{"filename": "result.png", "type": "output"}],
        }
        payload = server._task_api_response(record)

        self.assertEqual(payload["data"], [{"url": payload["outputs"][0]["url"]}])
        self.assertEqual(
            payload["outputs"][0]["download_path"],
            "/v1/files/task_done/result.png",
        )
        self.assertNotIn("sk-test-image", json.dumps(payload))

    async def test_text_and_unknown_outputs_are_not_reported_as_ark_images(self):
        text_payload = server._task_api_response(
            {
                "task_id": "task_text",
                "status": "completed",
                "outputs": [{"type": "text", "text": "OK"}],
            }
        )
        unknown_payload = server._task_api_response(
            {
                "task_id": "task_unknown",
                "status": "completed",
                "outputs": [{"filename": "result.bin", "type": "output"}],
            }
        )

        self.assertNotIn("data", text_payload)
        self.assertNotIn("data", unknown_payload)

    async def test_cross_origin_file_download_requires_bearer_and_returns_png(self):
        output = self.base / "outputs" / "result.png"
        output.write_bytes(PNG_BYTES)
        with server._task_lock:
            server.task_records["task_file"] = {
                "task_id": "task_file",
                "status": "completed",
                "outputs": [{"filename": output.name}],
            }
        origin = "https://example.test"

        preflight_status, preflight_headers, _ = await asgi_request(
            self.app,
            "OPTIONS",
            "/v1/files/task_file/result.png",
            headers={
                "origin": origin,
                "access-control-request-method": "GET",
                "access-control-request-headers": "authorization",
            },
        )
        unauth_status, _headers, _body = await asgi_request(
            self.app,
            "GET",
            "/v1/files/task_file/result.png",
            headers={"origin": origin},
        )
        auth_status, auth_headers, auth_body = await asgi_request(
            self.app,
            "GET",
            "/v1/files/task_file/result.png",
            headers={**self.auth, "origin": origin},
        )

        self.assertEqual(preflight_status, 200)
        self.assertEqual(preflight_headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", preflight_headers)
        self.assertEqual(unauth_status, 401)
        self.assertEqual(auth_status, 200)
        self.assertEqual(auth_headers["content-type"], "image/png")
        self.assertEqual(auth_body, PNG_BYTES)

    async def test_delete_only_registered_completed_media(self):
        output = self.base / "outputs" / "delete-me.png"
        output.write_bytes(PNG_BYTES)
        unrelated = self.base / "outputs" / "keep.png"
        unrelated.write_bytes(PNG_BYTES)
        with server._task_lock:
            server.task_records["deletion"] = {"task_id":"deletion", "status":"completed",
                                               "outputs":[{"filename":output.name}]}
        status, _, _ = await asgi_request(self.app, "DELETE", "/v1/files/deletion/delete-me.png")
        self.assertEqual(status,401)
        self.assertTrue(output.exists())
        status, _, _ = await asgi_request(self.app, "DELETE", "/v1/files/deletion/keep.png", headers=self.auth)
        self.assertEqual(status,404)
        self.assertTrue(unrelated.exists())
        status, _, body = await asgi_request(self.app, "DELETE", "/v1/files/deletion/delete-me.png", headers=self.auth)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["deleted"])
        self.assertFalse(output.exists())
        self.assertTrue(unrelated.exists())

    async def test_video_preview_requires_auth_and_keeps_original_private(self):
        from PIL import Image
        output = self.base / "outputs" / "movie.mp4"
        output.write_bytes(b"video fixture")
        server._set_task_record("video_preview", {"task_id":"video_preview", "status":"completed", "outputs":[{"filename":output.name}]})
        path = "/v1/files/video_preview/movie.mp4/preview"
        with mock.patch("app.core.single_user_assets._video_first_frame", return_value=Image.new("RGB", (800, 480), "red")) as decode:
            status, _, _ = await asgi_request(self.app, "GET", path)
            self.assertEqual(status, 401)
            decode.assert_not_called()
            status, headers, body = await asgi_request(self.app, "GET", path, headers=self.auth)
            self.assertEqual(status, 200)
            self.assertIn("image/webp", headers["content-type"])
            self.assertNotEqual(body, output.read_bytes())
            decode.assert_called_once()

    async def test_archived_result_survives_memory_reset_and_preview_is_smaller(self):
        from PIL import Image
        from io import BytesIO
        output = self.base / "outputs" / "large.png"
        Image.new("RGB", (1536, 1024), "orange").save(output)
        original = output.read_bytes()
        server._set_task_record("archived", {"task_id":"archived", "status":"completed", "outputs":[{"filename":output.name}]})
        with server._task_lock:
            server.task_records.pop("archived")
        status, _, body = await asgi_request(self.app, "GET", "/v1/tasks/archived", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outputs"][0]["filename"], output.name)
        status, _, _ = await asgi_request(self.app, "GET", "/v1/files/archived/large.png/preview")
        self.assertEqual(status, 401)
        status, headers, body = await asgi_request(self.app, "GET", "/v1/files/archived/large.png/preview", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertIn("image/webp", headers["content-type"])
        with Image.open(BytesIO(body)) as thumbnail:
            self.assertEqual(max(thumbnail.size), 640)
        self.assertEqual(output.read_bytes(), original)
        status, _, _ = await asgi_request(self.app, "DELETE", "/v1/files/archived/large.png", headers=self.auth)
        self.assertEqual(status, 200)
        status, _, _ = await asgi_request(self.app, "GET", "/v1/files/archived/large.png/preview", headers=self.auth)
        self.assertEqual(status, 404)

    async def test_flux_defaults_are_read_from_executable_graph(self):
        from app.workflow_registry import WorkflowDef
        root = Path(__file__).resolve().parents[1] / "workflows"
        folder = root / "flux2_klein_4b_v1"
        workflow = WorkflowDef.from_manifest(folder, json.loads((folder / "manifest.json").read_text(encoding="utf-8")))
        workflow._workflows_dir = root
        schema = server._workflow_payload(workflow)["input_schema"]
        defaults = {field["name"]:field.get("default") for field in schema["inputs"]}
        self.assertEqual((defaults["width"], defaults["height"]), (768,768))
        self.assertEqual(schema["reference_sizing"]["megapixels"], 0.6)

    async def test_output_path_guard_rejects_sibling_prefix(self):
        outputs = (self.base / "outputs").resolve()
        sibling = (self.base / "outputs_evil" / "secret.png").resolve()

        self.assertFalse(server._path_is_within(sibling, outputs))
        self.assertTrue(server._path_is_within(outputs / "safe.png", outputs))

    async def test_output_download_requires_filename_owned_by_real_task(self):
        output = self.base / "outputs" / "unguarded.png"
        output.write_bytes(PNG_BYTES)

        status, _headers, _body = await asgi_request(
            self.app,
            "GET",
            "/v1/files/not-a-task/unguarded.png",
            headers=self.auth,
        )

        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
