import tempfile
import threading
import time
import unittest
import io
import json
import hashlib
import os
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp


class RuntimeMaintenanceTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = object.__new__(GatewayApp)
        app._runtime_maintenance_lock = threading.RLock()
        app._runtime_maintenance_in_progress = False
        app._backend_action_lock = threading.Lock()
        app._backend_action_name = ""
        app._shutting_down = False
        app._last_health = {}
        return app

    def test_model_download_sources_include_domestic_and_official_links(self):
        app = self._app()
        official = "https://huggingface.co/org/repo/resolve/main/model.safetensors"

        sources = app._model_download_sources(official)

        self.assertEqual(sources[0][0], "国内镜像")
        self.assertTrue(sources[0][1].startswith("https://hf-mirror.com/"))
        self.assertEqual(sources[1], ("官方源", official))

    def test_model_download_selects_the_faster_valid_source(self):
        mirror = "https://hf-mirror.com/org/model/resolve/main/model.safetensors"
        official = "https://huggingface.co/org/model/resolve/main/model.safetensors"

        class Probe(io.BytesIO):
            status = 206

            def __init__(self, slow):
                super().__init__(b"model")
                self.slow = slow
                self.headers = {"Content-Range": "bytes 0-4/5", "Content-Length": "5"}

            def read(self, size=-1):
                if self.slow:
                    time.sleep(0.025)
                return super().read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with mock.patch.object(
            main_gateway, "_open_download_request",
            side_effect=lambda request, **_kwargs: Probe(request.full_url == mirror),
        ):
            self.assertEqual(
                main_gateway._rank_model_download_sources([mirror, official], 5),
                [official, mirror],
            )


    def test_model_download_keeps_partial_when_fastest_source_fails_over(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._set_model_download_status = mock.Mock()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()
        mirror = "https://hf-mirror.com/org/model/resolve/main/model.safetensors"
        official = "https://huggingface.co/org/model/resolve/main/model.safetensors"

        class Response(io.BytesIO):
            def __init__(self, payload, status, headers, interrupt=False):
                super().__init__(payload)
                self.status = status
                self.headers = headers
                self.interrupt = interrupt
                self.reads = 0

            def read(self, size=-1):
                self.reads += 1
                if self.interrupt and self.reads > 1:
                    raise urllib.error.URLError(ConnectionResetError(10054, "connection reset"))
                return super().read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        calls = []

        def open_request(request, **_kwargs):
            calls.append(request)
            if request.full_url == mirror:
                if sum(call.full_url == mirror for call in calls) == 1:
                    return Response(b"mo", 200, {"Content-Length": "5", "ETag": '"same"'}, True)
                raise urllib.error.URLError(ConnectionResetError(10054, "connection reset"))
            self.assertEqual(request.get_header("Range"), "bytes=2-")
            self.assertIsNone(request.get_header("If-range"), "ETag belongs to the previous server")
            return Response(b"del", 206, {
                "Content-Length": "3", "Content-Range": "bytes 2-4/5", "ETag": '"official-etag"'
            })

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": mirror, "urls": [mirror, official], "target": target,
                "item": {"size_bytes": 5, "sha256": hashlib.sha256(b"model").hexdigest()},
                "pause_event": threading.Event(), "status_var": mock.Mock(),
            }
            with (
                mock.patch.object(main_gateway, "_rank_model_download_sources", return_value=[mirror, official]),
                mock.patch.object(main_gateway, "_open_download_request", side_effect=open_request),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            self.assertEqual(control["url"], official)
            self.assertGreaterEqual(sum(call.full_url == mirror for call in calls), 7)
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()


    def test_model_download_can_resume_without_server_validator(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._set_model_download_status = mock.Mock()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            def __init__(self, payload, status, headers, interrupt=False):
                super().__init__(payload)
                self.status = status
                self.headers = headers
                self.interrupt = interrupt
                self.reads = 0

            def read(self, size=-1):
                self.reads += 1
                if self.interrupt and self.reads > 1:
                    raise urllib.error.URLError(ConnectionResetError(10054, "connection reset"))
                return super().read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def open_request(request, **_kwargs):
            if request.get_header("Range"):
                self.assertEqual(request.get_header("Range"), "bytes=2-")
                self.assertIsNone(request.get_header("If-range"))
                return Response(b"del", 206, {"Content-Length": "3", "Content-Range": "bytes 2-4/5"})
            return Response(b"mo", 200, {"Content-Length": "5"}, True)

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://hf-mirror.com/org/model/resolve/main/model.safetensors",
                "target": target,
                "item": {"size_bytes": 5, "sha256": hashlib.sha256(b"model").hexdigest()},
                "pause_event": threading.Event(), "status_var": mock.Mock(),
            }
            with (
                mock.patch.object(main_gateway, "_open_download_request", side_effect=open_request),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()


    def test_finished_model_download_triggers_one_model_status_recheck(self):
        app = self._app()
        app._release_model_transfer = mock.Mock()
        app._set_model_download_status = mock.Mock()
        app._footer_label = mock.Mock()
        control = {"state": "downloading", "worker": mock.Mock()}

        with mock.patch.object(threading, "Thread") as thread:
            app._finish_model_download(control)

        self.assertEqual(control["state"], "done")
        self.assertEqual(control["progress_percent"], 100.0)
        thread.assert_called_once_with(target=app._recheck_models, daemon=True)
        thread.return_value.start.assert_called_once_with()

    def test_system_environment_rejects_pre_cuda_13_driver(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA RTX 4090, 576.80, 24564\n",
            )
        )

        self.assertFalse(result["success"])
        self.assertIn("R580", result["error"])
        self.assertEqual(result["driver_version"], "576.80")

    def test_system_environment_accepts_cuda_13_driver(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA RTX 4090, 580.88, 24564\n",
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["driver_version"], "580.88")
        self.assertEqual(result["vram_mb"], 24564)

    def test_system_environment_rejects_pre_rtx_gpu_even_with_enough_vram(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA GeForce GTX 1080 Ti, 580.88, 11264\n",
            )
        )

        self.assertFalse(result["success"])
        self.assertIn("RTX 20", result["error"])

    def test_system_environment_rejects_supported_gpu_below_8gb(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA GeForce RTX 2060, 580.88, 6144\n",
            )
        )

        self.assertFalse(result["success"])
        self.assertIn("8GB", result["error"])

    def test_system_environment_selects_supported_gpu_from_multiple_cards(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout=(
                    "NVIDIA GeForce GTX 1080 Ti, 580.88, 11264\n"
                    "NVIDIA GeForce RTX 3060, 580.88, 12288\n"
                ),
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["gpu_name"], "NVIDIA GeForce RTX 3060")

    def test_torch_probe_allows_slow_first_cuda_initialisation(self):
        run_command = mock.Mock(
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "torch_version": "2.9.1+cu130",
                        "cuda_available": True,
                        "gpu_name": "NVIDIA GeForce RTX 3060 Ti",
                    }
                ),
            )
        )

        with mock.patch.dict(
            os.environ,
            {
                "PYTHONNOUSERSITE": "0",
                "PYTHONHOME": "unsafe-home",
                "PYTHONPATH": "unsafe-path",
                "PYTHONSTARTUP": "unsafe-startup.py",
            },
        ):
            result = main_gateway._check_torch(
                Path("runtime/python/python.exe"),
                run_command,
            )

        self.assertTrue(result["success"])
        self.assertGreaterEqual(run_command.call_args.kwargs["timeout"], 90)
        command = run_command.call_args.args[0]
        self.assertEqual(command[1:4], ["-s", "-B", "-c"])
        probe_env = run_command.call_args.kwargs["env"]
        self.assertEqual(probe_env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(probe_env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(probe_env["PYTHONUTF8"], "1")
        self.assertNotIn("PYTHONHOME", probe_env)
        self.assertNotIn("PYTHONPATH", probe_env)
        self.assertNotIn("PYTHONSTARTUP", probe_env)

    def test_download_redirects_are_rejected_before_transport_follows_them(self):
        original = "https://github.com/Yaro-lu/LingJingAPI/releases/download/v1/runtime.7z"
        request = urllib.request.Request(original)
        handler = main_gateway._DownloadRedirectHandler(
            original,
            main_gateway.RUNTIME_DOWNLOAD_REDIRECT_SUFFIXES,
        )

        for target in (
            "http://127.0.0.1/private",
            "https://127.0.0.1/private",
            "https://github.com.attacker.invalid/runtime.7z",
        ):
            with self.subTest(target=target), mock.patch.object(
                urllib.request.HTTPRedirectHandler,
                "redirect_request",
            ) as delegated:
                with self.assertRaisesRegex(OSError, "未授权的重定向"):
                    handler.redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        target,
                    )
                delegated.assert_not_called()

    def test_download_redirects_allow_only_expected_hf_xet_and_github_hosts(self):
        cases = (
            (
                "https://huggingface.co/org/repo/resolve/main/model.safetensors",
                "https://cdn-lfs.hf.co/file",
                main_gateway.MODEL_DOWNLOAD_REDIRECT_SUFFIXES,
            ),
            (
                "https://huggingface.co/org/repo/resolve/main/model.safetensors",
                "https://transfer.xethub.hf.co/file",
                main_gateway.MODEL_DOWNLOAD_REDIRECT_SUFFIXES,
            ),
            (
                "https://github.com/Yaro-lu/LingJingAPI/releases/download/v1/runtime.7z",
                "https://release-assets.githubusercontent.com/file",
                main_gateway.RUNTIME_DOWNLOAD_REDIRECT_SUFFIXES,
            ),
        )

        for original, target, suffixes in cases:
            with self.subTest(target=target), mock.patch.object(
                urllib.request.HTTPRedirectHandler,
                "redirect_request",
                return_value="delegated",
            ) as delegated:
                handler = main_gateway._DownloadRedirectHandler(original, suffixes)
                result = handler.redirect_request(
                    urllib.request.Request(original),
                    None,
                    302,
                    "Found",
                    {},
                    target,
                )
                self.assertEqual(result, "delegated")
                delegated.assert_called_once()

    def test_maintenance_guard_records_and_releases_owned_services(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.side_effect = lambda role: role == "comfyui"
        app._stop_runtime_for_maintenance = mock.Mock(return_value="")
        app.after = mock.Mock()

        started, running_before, error = app._begin_runtime_maintenance()

        self.assertTrue(started)
        self.assertEqual(running_before, {"comfyui"})
        self.assertEqual(error, "")
        self.assertTrue(app._runtime_maintenance_active())

        app._end_runtime_maintenance(restart=False)
        self.assertFalse(app._runtime_maintenance_active())
        app.after.assert_not_called()

    def test_maintenance_guard_releases_when_service_stop_raises(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.return_value = False
        app._stop_runtime_for_maintenance = mock.Mock(
            side_effect=OSError("process query failed")
        )

        started, running_before, error = app._begin_runtime_maintenance()

        self.assertFalse(started)
        self.assertEqual(running_before, set())
        self.assertIn("停止后台服务失败", error)
        self.assertIn("process query failed", error)
        self.assertFalse(app._runtime_maintenance_active())

    def test_maintenance_status_query_does_not_wait_for_slow_service_stop(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.return_value = True
        stop_started = threading.Event()
        allow_stop = threading.Event()

        def slow_stop():
            stop_started.set()
            allow_stop.wait(2)
            return ""

        app._stop_runtime_for_maintenance = slow_stop
        result = []
        worker = threading.Thread(
            target=lambda: result.append(app._begin_runtime_maintenance()),
            daemon=True,
        )
        worker.start()
        self.assertTrue(stop_started.wait(1))

        started_at = time.monotonic()
        self.assertTrue(app._runtime_maintenance_active())
        self.assertLess(time.monotonic() - started_at, 0.2)

        allow_stop.set()
        worker.join(1)
        self.assertEqual(result[0][0], True)

    def test_service_launch_is_rejected_while_runtime_is_replaced(self):
        app = self._app()
        app._runtime_maintenance_in_progress = True
        app._start_comfyui_service_unlocked = mock.Mock()
        app._start_api_service_unlocked = mock.Mock()

        app._start_comfyui_service()
        app._start_api_service()

        app._start_comfyui_service_unlocked.assert_not_called()
        app._start_api_service_unlocked.assert_not_called()

    def test_runtime_probe_is_rejected_while_replacement_is_active(self):
        app = self._app()
        app._runtime_maintenance_in_progress = True
        app._process_supervisor = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "正在维护"):
            app._run_runtime_probe("torch-check", ["python", "-V"])

        app._process_supervisor.run.assert_not_called()

    def test_startup_does_not_launch_torch_probe_after_maintenance_begins(self):
        app = self._app()
        app.after = lambda _delay, _callback: None

        def begin_maintenance(_runner):
            app._runtime_maintenance_in_progress = True
            return {"success": True, "gpu_name": "fixture"}

        with (
            mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]),
            mock.patch.object(main_gateway, "_check_system_env", side_effect=begin_maintenance),
            mock.patch.object(main_gateway, "_check_torch") as check_torch,
        ):
            app._startup_sequence()

        check_torch.assert_not_called()

    def test_maintenance_end_schedules_backend_restore(self):
        app = self._app()
        callbacks = []
        app.after = lambda _delay, callback: callbacks.append(callback)
        app._startup_sequence = mock.Mock()
        thread = mock.Mock()

        with mock.patch.object(threading, "Thread", return_value=thread) as thread_factory:
            app._end_runtime_maintenance(restart=True)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        thread_factory.assert_called_once()
        thread.start.assert_called_once_with()

    def test_empty_model_placeholder_is_reported_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            requirements = {
                group: {
                    "title": spec["title"],
                    "items": [
                        {"path": item["path"], "size_bytes": len(b"model")}
                        for item in spec["items"]
                    ],
                }
                for group, spec in main_gateway.MODEL_REQUIREMENTS.items()
            }
            paths = [
                item["path"]
                for spec in requirements.values()
                for item in spec["items"]
            ]
            for relative in paths:
                path = base / "models" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway, "MODEL_REQUIREMENTS", requirements),
            ):
                status = main_gateway._check_models_status()
                self.assertFalse(status["all_ok"])
                self.assertEqual(status["Qwen3.5"], "缺失")

                for relative in paths:
                    (base / "models" / relative).write_bytes(b"model")
                status = main_gateway._check_models_status()

            self.assertTrue(status["all_ok"])

    def test_resuming_model_download_is_still_an_active_background_task(self):
        self.assertTrue(main_gateway._model_download_active({"state": "downloading"}))
        self.assertTrue(main_gateway._model_download_active({"state": "resuming"}))
        self.assertTrue(main_gateway._model_download_active({"state": "cancelling"}))
        self.assertTrue(main_gateway._model_download_active({"state": "abandoning"}))
        self.assertFalse(main_gateway._model_download_active({"state": "paused"}))

    def test_startup_failure_is_not_marked_ready_and_does_not_start_backend(self):
        app = self._app()
        app._set_light = mock.Mock()
        app._footer_label = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._update_model_display = mock.Mock()
        app._start_backend = mock.Mock()
        app.after = lambda _delay, callback: callback()

        model_status = {
            "Qwen3.5": "缺失",
            "Flux2": "缺失",
            "Wan2.1": "缺失",
            "all_ok": False,
            "missing": {"Qwen3.5": [], "Flux2": [], "Wan2.1": []},
        }
        with (
            mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]),
            mock.patch.object(
                main_gateway,
                "_check_system_env",
                return_value={"success": False, "error": "NVIDIA 驱动不可用"},
            ),
            mock.patch.object(
                main_gateway,
                "_check_torch",
                return_value={"success": True, "gpu_name": "fixture"},
            ),
            mock.patch.object(main_gateway, "_check_models_status", return_value=model_status),
            mock.patch.object(main_gateway, "_ensure_extra_model_paths"),
        ):
            app._startup_sequence()

        self.assertFalse(app._environment_status["ready"])
        app._start_backend.assert_not_called()
        self.assertIn(
            mock.call("env", "offline", "需处理"),
            app._set_light.call_args_list,
        )

    def test_missing_runtime_offers_quick_repair(self):
        app = self._app()
        app._set_light = mock.Mock()
        app._footer_label = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._offer_quick_repair = mock.Mock()
        app.after = lambda _delay, callback: callback()

        with (
            mock.patch.object(
                main_gateway,
                "missing_runtime_paths",
                return_value=["runtime/python/python.exe"],
            ),
            mock.patch.object(main_gateway, "_check_system_env") as check_system,
            mock.patch.object(main_gateway, "_detect_gpu_memory_mb", return_value=8192),
        ):
            app._startup_sequence()

        app._offer_quick_repair.assert_called_once_with(False, 8192)
        check_system.assert_not_called()
        for key in ("comfyui", "api", "tunnel"):
            self.assertIn(
                mock.call(key, "offline", "等待安装环境"),
                app._set_light.call_args_list,
            )

    def test_maintenance_is_blocked_by_external_runtime_listener(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.terminate.return_value = ""
        app._process_supervisor.is_running.return_value = False
        app._process_supervisor.prepare_port.side_effect = [
            (False, "端口 8188 已被其他程序占用 (PID 24680)"),
            (True, ""),
        ]

        error = app._stop_runtime_for_maintenance()

        self.assertIn("24680", error)
        self.assertIn("手动关闭", error)

    def test_model_download_promotes_only_expected_size(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            status = 200

            def __init__(self, payload: bytes):
                super().__init__(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {
                    "size_bytes": len(b"model"),
                    "sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with mock.patch.object(
                main_gateway,
                "_open_download_request",
                return_value=Response(b"model"),
            ):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()

    def test_model_download_keeps_incomplete_file_as_partial(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            status = 200

            def __init__(self):
                super().__init__(b"bad")
                self.headers = {"Content-Length": "5", "ETag": '"fixture"'}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {
                    "size_bytes": 5,
                    "sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with (
                mock.patch.object(
                    main_gateway,
                    "_open_download_request",
                    side_effect=lambda *_args, **_kwargs: Response(),
                ),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertFalse(target.exists())
            self.assertTrue(target.with_suffix(target.suffix + ".part").exists())
            app._finish_model_download.assert_not_called()
            app._fail_model_download.assert_called_once()

    def test_model_download_promotes_already_complete_partial_without_network(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            partial = target.with_suffix(target.suffix + ".part")
            partial.write_bytes(b"model")
            partial.with_suffix(partial.suffix + ".json").write_text(
                json.dumps(
                    {
                        "url": "https://example.invalid/model.safetensors",
                        "expected_size": len(b"model"),
                        "expected_sha256": hashlib.sha256(b"model").hexdigest(),
                        "validator": '"fixture"',
                    }
                ),
                encoding="utf-8",
            )
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {
                    "size_bytes": len(b"model"),
                    "sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with mock.patch.object(main_gateway, "_open_download_request") as urlopen:
                app._download_model_file(control)

            urlopen.assert_not_called()
            self.assertEqual(target.read_bytes(), b"model")
            self.assertFalse(partial.exists())
            app._finish_model_download.assert_called_once_with(control)

    def test_model_download_never_combines_untrusted_prefix_with_valid_tail(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            def __init__(self, payload: bytes, status: int, headers: dict):
                super().__init__(payload)
                self.status = status
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        responses = [
            Response(b"XX", 200, {"Content-Length": "2", "ETag": '"bad"'}),
            Response(b"cde", 206, {"Content-Length": "3", "Content-Range": "bytes 2-4/5", "ETag": '"good"'}),
            Response(b"model", 200, {"Content-Length": "5", "ETag": '"good"'}),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {
                    "size_bytes": 5,
                    "sha256": hashlib.sha256(b"model").hexdigest(),
                },
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with (
                mock.patch.object(
                    main_gateway,
                    "_open_download_request",
                    side_effect=responses,
                ),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()

    def test_model_resume_waits_for_paused_worker_to_exit(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._download_model_file = mock.Mock()
        pause_event = threading.Event()
        pause_seen = threading.Event()
        allow_exit = threading.Event()

        def old_download():
            pause_event.wait(2)
            pause_seen.set()
            allow_exit.wait(2)

        old_worker = threading.Thread(target=old_download, daemon=True)
        old_worker.start()
        control = {
            "state": "downloading",
            "target": Path(tempfile.gettempdir()) / "lingjing-resume-fixture.safetensors",
            "pause_event": pause_event,
            "stop_event": threading.Event(),
            "status_var": mock.Mock(),
            "button": mock.Mock(),
            "pause_button": mock.Mock(),
            "worker": old_worker,
        }

        app._pause_model_download(control)
        self.assertTrue(pause_seen.wait(1))
        app._start_model_download(control)
        self.assertEqual(control["state"], "resuming")
        app._download_model_file.assert_not_called()

        allow_exit.set()
        old_worker.join(1)
        deadline = time.time() + 1
        while time.time() < deadline and not app._download_model_file.called:
            time.sleep(0.01)

        app._download_model_file.assert_called_once_with(control)
        self.assertEqual(control["state"], "downloading")

    def test_abandon_paused_download_releases_target_only_after_writer_exits(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        pause_event = threading.Event()
        pause_seen = threading.Event()
        allow_exit = threading.Event()
        done = threading.Event()
        target = Path(tempfile.gettempdir()) / "lingjing-abandon-fixture.safetensors"

        def old_download():
            pause_event.wait(2)
            pause_seen.set()
            allow_exit.wait(2)

        old_worker = threading.Thread(target=old_download, daemon=True)
        old_worker.start()
        control = {
            "state": "downloading",
            "target": target,
            "pause_event": pause_event,
            "status_var": mock.Mock(),
            "button": mock.Mock(),
            "pause_button": mock.Mock(),
            "worker": old_worker,
        }
        self.assertTrue(app._claim_model_transfer(control))
        app._pause_model_download(control)
        self.assertTrue(pause_seen.wait(1))

        app._abandon_paused_model_download(control, done.set)

        self.assertEqual(control["state"], "abandoning")
        self.assertTrue(app._model_transfer_active(target))
        self.assertFalse(done.is_set())
        allow_exit.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(control["state"], "abandoned")
        self.assertFalse(app._model_transfer_active(target))

    def test_same_model_target_cannot_start_twice(self):
        app = self._app()
        app._download_model_file = mock.Mock()
        target = Path(tempfile.gettempdir()) / "lingjing-transfer-fixture.safetensors"

        def control():
            return {
                "state": "idle",
                "target": target,
                "urls": ["https://huggingface.co/org/repo/resolve/main/model.safetensors"],
                "item": {"size_bytes": 5, "sha256": hashlib.sha256(b"model").hexdigest()},
                "pause_event": threading.Event(),
                "stop_event": threading.Event(),
                "status_var": mock.Mock(),
                "button": mock.Mock(),
                "pause_button": mock.Mock(),
                "worker": None,
            }

        first = control()
        second = control()
        app._start_model_download(first)
        app._start_model_download(second)

        self.assertEqual(first["state"], "downloading")
        self.assertEqual(second["state"], "idle")
        self.assertIs(app._active_model_transfers[app._model_transfer_key(target)], first)

    def test_cancel_waits_for_writer_then_deletes_partial_and_metadata(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._footer_label = mock.Mock()
        writer_started = threading.Event()
        allow_exit = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            partial = target.with_suffix(target.suffix + ".part")
            metadata = partial.with_suffix(partial.suffix + ".json")
            partial.write_bytes(b"partial")
            metadata.write_text("{}", encoding="utf-8")
            stop_event = threading.Event()

            def writer():
                writer_started.set()
                stop_event.wait(2)
                allow_exit.wait(2)

            worker = threading.Thread(target=writer, daemon=True)
            worker.start()
            self.assertTrue(writer_started.wait(1))
            control = {
                "state": "downloading",
                "target": target,
                "pause_event": threading.Event(),
                "stop_event": stop_event,
                "status_var": mock.Mock(),
                "progress_var": mock.Mock(),
                "button": mock.Mock(),
                "pause_button": mock.Mock(),
                "cancel_button": mock.Mock(),
                "worker": worker,
                "views": [],
            }
            self.assertTrue(app._claim_model_transfer(control))

            app._cancel_model_download(control)

            self.assertEqual(control["state"], "cancelling")
            self.assertTrue(partial.exists())
            allow_exit.set()
            deadline = time.time() + 2
            while time.time() < deadline and control["state"] != "cancelled":
                time.sleep(0.01)

            self.assertEqual(control["state"], "cancelled")
            self.assertFalse(partial.exists())
            self.assertFalse(metadata.exists())
            self.assertFalse(app._model_transfer_active(target))

    def test_reopened_download_view_receives_shared_progress(self):
        app = self._app()
        owner_status = mock.Mock()
        owner_progress = mock.Mock()
        attached_status = mock.Mock()
        attached_progress = mock.Mock()
        owner = {
            "state": "downloading",
            "target": Path(tempfile.gettempdir()) / "lingjing-attached-progress.safetensors",
            "url": "https://example.invalid/model.safetensors",
            "status_var": owner_status,
            "progress_var": owner_progress,
        }
        attached = {
            "owner": owner,
            "status_var": attached_status,
            "progress_var": attached_progress,
        }
        owner["views"] = [owner, attached]

        app._update_model_download_progress(owner, 42.6, 426, 1000)

        owner_progress.set.assert_called_with(42.6)
        attached_progress.set.assert_called_with(42.6)
        self.assertIn("43%", attached_status.set.call_args.args[0])

    def test_runtime_update_result_reports_rollback_in_chinese(self):
        app = self._app()
        result = {
            "success": False,
            "result_code": "install_failed_rolled_back",
            "message": "locked file",
        }
        with (
            mock.patch.object(main_gateway, "consume_runtime_update_result", return_value=result),
            mock.patch.object(main_gateway.messagebox, "showerror") as showerror,
        ):
            app._show_runtime_update_result()

        title, message = showerror.call_args.args
        self.assertEqual(title, "运行环境更新失败")
        self.assertIn("旧环境已恢复", message)
        self.assertIn("locked file", message)
        self.assertIs(showerror.call_args.kwargs["parent"], app)

    def test_runtime_update_handoff_closes_gui_for_external_swap(self):
        app = self._app()
        app._anim_running = True
        app._poll_run = True
        app._complete_destroy = mock.Mock()
        popup = mock.sentinel.popup

        app._exit_for_runtime_update(popup)

        self.assertTrue(app._shutting_down)
        self.assertFalse(app._anim_running)
        self.assertFalse(app._poll_run)
        app._complete_destroy.assert_called_once_with(popup)

    def test_restarted_gui_writes_matching_runtime_ready_ack(self):
        app = self._app()
        operation_id = "f" * 32
        app._pending_runtime_update_result = {
            "operation_id": operation_id,
            "result_code": "installed",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            main_gateway, "BASE_DIR", Path(tmp)
        ), mock.patch.dict(
            main_gateway.os.environ,
            {"LINGJING_RUNTIME_UPDATE_OPERATION_ID": operation_id},
            clear=False,
        ):
            app._write_runtime_restart_ack()
            ack_path = (
                Path(tmp)
                / "runtime"
                / f"runtime-update-ready-{operation_id}.json"
            )
            payload = json.loads(ack_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["operation_id"], operation_id)
        self.assertEqual(payload["pid"], main_gateway.os.getpid())


    def test_comfyui_update_task_guard_covers_all_active_states(self):
        for status in ("reserving", "queued", "pending", "submitted", "running"):
            with self.subTest(status=status):
                self.assertTrue(
                    GatewayApp._comfyui_update_task_active(
                        {"current_task": {"status": status}}
                    )
                )
        self.assertFalse(
            GatewayApp._comfyui_update_task_active(
                {"current_task": {"status": "completed"}}
            )
        )

    def test_comfyui_update_reads_live_queue_when_cached_health_is_idle(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.return_value = True
        client = mock.Mock()
        client.get_queue_status.return_value = {
            "queue_running": [[1, "prompt-live"]],
            "queue_pending": [],
        }

        with mock.patch.object(main_gateway, "ComfyUIClient", return_value=client):
            reason = app._comfyui_update_live_queue_reason()

        self.assertIn("1 个", reason)
        client.get_queue_status.assert_called_once_with()

    def test_comfyui_update_rechecks_local_activity_after_api_is_stopped(self):
        app = self._app()
        app._comfyui_update_local_activity_reason = mock.Mock(
            side_effect=["", "模型下载正在进行"]
        )
        app._comfyui_update_live_queue_reason = mock.Mock(return_value="")
        app._stop_api_submission_for_comfyui_update = mock.Mock(return_value="")

        reason = app._quiesce_comfyui_for_update()

        self.assertEqual(reason, "模型下载正在进行")
        app._stop_api_submission_for_comfyui_update.assert_called_once_with()
        self.assertEqual(app._comfyui_update_live_queue_reason.call_count, 1)

    def test_comfyui_update_rechecks_live_queue_after_api_is_stopped(self):
        app = self._app()
        app._comfyui_update_local_activity_reason = mock.Mock(return_value="")
        app._comfyui_update_live_queue_reason = mock.Mock(
            side_effect=["", "ComfyUI 队列中还有 1 个生成任务"]
        )
        app._stop_api_submission_for_comfyui_update = mock.Mock(return_value="")

        reason = app._quiesce_comfyui_for_update()

        self.assertIn("1 个", reason)
        app._stop_api_submission_for_comfyui_update.assert_called_once_with()
        self.assertEqual(app._comfyui_update_live_queue_reason.call_count, 2)

    def test_runtime_reservation_blocks_new_workflow_and_model_maintenance(self):
        app = self._app()
        app._workflow_management_lock = threading.Lock()
        app._workflow_operation_name = ""
        app._model_transfer_lock = threading.RLock()
        app._active_model_transfers = {}
        app._model_import_in_progress = False
        app._footer_label = mock.Mock()
        app._runtime_maintenance_in_progress = True

        self.assertFalse(app._begin_workflow_operation("工作流导入"))
        self.assertFalse(
            app._claim_model_transfer({"target": Path("models/test.safetensors")})
        )
        self.assertFalse(app._workflow_management_lock.locked())
        self.assertEqual(app._active_model_transfers, {})

    def test_startup_preconsumes_incomplete_results_and_blocks_backend(self):
        app = self._app()
        app._footer_label = mock.Mock()
        app.after = mock.Mock()
        runtime_result = {
            "success": False,
            "result_code": "install_failed_rollback_incomplete",
            "message": "runtime locked",
        }
        comfy_result = {
            "success": False,
            "status": "rollback_incomplete",
            "error": "core locked",
        }
        with (
            mock.patch.object(
                main_gateway,
                "recover_interrupted_update",
                return_value={"status": "no_recovery_needed", "success": True},
            ) as recover,
            mock.patch.object(
                main_gateway, "consume_runtime_update_result", return_value=runtime_result
            ) as consume_runtime,
            mock.patch.object(
                main_gateway, "consume_comfyui_update_result", return_value=comfy_result
            ) as consume_comfy,
            mock.patch.object(main_gateway.messagebox, "showerror") as showerror,
        ):
            app._capture_startup_update_results()
            self.assertFalse(app._request_backend_start("启动后台"))
            app._show_runtime_update_result()
            app._show_comfyui_update_result()
            app._show_runtime_update_result()
            app._show_comfyui_update_result()

        self.assertTrue(app._runtime_start_blocked)
        recover.assert_called_once_with(main_gateway.BASE_DIR)
        consume_runtime.assert_called_once_with(main_gateway.BASE_DIR)
        consume_comfy.assert_called_once_with(main_gateway.BASE_DIR)
        self.assertEqual(showerror.call_count, 2)

    def test_incomplete_interrupted_recovery_blocks_startup_and_prompts_repair(self):
        app = self._app()
        recovery = {
            "status": "recovery_incomplete",
            "success": False,
            "message": "旧核心仍被占用",
            "errors": ["backup locked"],
        }
        with (
            mock.patch.object(
                main_gateway, "recover_interrupted_update", return_value=recovery
            ) as recover,
            mock.patch.object(
                main_gateway, "consume_runtime_update_result", return_value=None
            ),
            mock.patch.object(
                main_gateway, "consume_comfyui_update_result", return_value=None
            ),
            mock.patch.object(main_gateway.messagebox, "showerror") as showerror,
        ):
            app._capture_startup_update_results()
            app._show_comfyui_recovery_result()
            app._show_comfyui_recovery_result()

        self.assertTrue(app._runtime_start_blocked)
        recover.assert_called_once_with(main_gateway.BASE_DIR)
        showerror.assert_called_once()
        self.assertIn("先修复运行环境", showerror.call_args.args[1])

    def test_completed_interrupted_recovery_allows_startup_without_repeat_popup(self):
        app = self._app()
        recovery = {
            "status": "recovered",
            "success": True,
            "message": "旧核心已安全恢复",
        }
        with (
            mock.patch.object(
                main_gateway, "recover_interrupted_update", return_value=recovery
            ),
            mock.patch.object(
                main_gateway, "consume_runtime_update_result", return_value=None
            ),
            mock.patch.object(
                main_gateway, "consume_comfyui_update_result", return_value=None
            ),
            mock.patch.object(main_gateway.messagebox, "showinfo") as showinfo,
        ):
            app._capture_startup_update_results()
            app._show_comfyui_recovery_result()
            app._show_comfyui_recovery_result()

        self.assertFalse(app._runtime_start_blocked)
        showinfo.assert_not_called()

    def test_active_detached_recovery_blocks_backend_prompts_and_closes_once(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._show_active_comfyui_update_notice = mock.Mock()
        app._footer_label = mock.Mock()
        app.after = lambda _delay, callback: callback()
        recovery = {
            "status": "recovery_in_progress",
            "success": False,
            "in_progress": True,
            "message": "ComfyUI 更新仍在进行",
        }
        with (
            mock.patch.object(
                main_gateway, "recover_interrupted_update", return_value=recovery
            ),
            mock.patch.object(
                main_gateway, "consume_runtime_update_result", return_value=None
            ),
            mock.patch.object(
                main_gateway, "consume_comfyui_update_result", return_value=None
            ),
        ):
            app._capture_startup_update_results()
            self.assertFalse(app._request_backend_start("启动后台"))
            app._show_comfyui_recovery_result()
            app._show_comfyui_recovery_result()

        self.assertTrue(app._runtime_start_blocked)
        self.assertFalse(app._shutting_down)
        app._show_active_comfyui_update_notice.assert_called_once_with()
        app._process_supervisor.shutdown_all.assert_not_called()

    def test_overlay_timeout_keeps_staging_when_process_cannot_be_stopped(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.run_observed.side_effect = RuntimeError(
            "后台进程超时后无法安全停止"
        )
        app._process_supervisor.is_running.return_value = True

        with self.assertRaisesRegex(
            main_gateway._ComfyUIOverlayProcessStillRunning,
            "保留暂存",
        ):
            app._run_comfyui_overlay_prepare(["python", "-c", "fixed"])

        app._process_supervisor.run.assert_not_called()

    def test_comfyui_update_result_reports_automatic_rollback(self):
        app = self._app()
        result = {
            "status": "failed_rolled_back",
            "success": False,
            "rolled_back": True,
            "error": "probe failed",
            "cleanup_warnings": ["备份目录仍被占用"],
            "rollback_errors": ["回滚检查提示"],
            "manifest_cleanup_error": "清单待下次启动清理",
        }
        with (
            mock.patch.object(
                main_gateway,
                "consume_comfyui_update_result",
                return_value=result,
            ),
            mock.patch.object(main_gateway.messagebox, "showerror") as showerror,
        ):
            app._show_comfyui_update_result()

        title, message = showerror.call_args.args
        self.assertEqual(title, "ComfyUI 更新失败")
        self.assertIn("旧版本已自动恢复", message)
        self.assertIn("probe failed", message)
        self.assertIn("备份目录仍被占用", message)
        self.assertIn("回滚检查提示", message)
        self.assertIn("清单待下次启动清理", message)

    def test_comfyui_update_success_is_silent_after_manual_restart(self):
        app = self._app()
        app._footer_label = mock.Mock()
        result = {
            "status": "installed",
            "success": True,
            "cleanup_warnings": ["旧备份仍被占用", "x" * 5000],
            "rollback_errors": ["回滚日志提示"],
            "manifest_cleanup_error": "清单文件稍后删除",
        }
        with (
            mock.patch.object(
                main_gateway,
                "consume_comfyui_update_result",
                return_value=result,
            ),
            mock.patch.object(main_gateway.messagebox, "showwarning") as showwarning,
            mock.patch.object(main_gateway.messagebox, "showinfo") as showinfo,
        ):
            app._show_comfyui_update_result()

        showinfo.assert_not_called()
        showwarning.assert_not_called()
        self.assertIn(
            "ComfyUI 已更新",
            app._footer_label.config.call_args.kwargs["text"],
        )

    def test_comfyui_update_handoff_uses_parent_wait_and_ready_manifest(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.side_effect = lambda role: role == "comfyui"
        app._stop_runtime_for_maintenance = mock.Mock(return_value="")
        app._quiesce_comfyui_for_update = mock.Mock(return_value="")
        app._exit_for_runtime_update = mock.Mock()
        app._post_to_ui = lambda callback, delay=0: (callback(), True)[1]
        app.after = mock.Mock()
        app._show_manual_restart_notice = mock.Mock(return_value=True)

        popup = mock.MagicMock()
        popup.winfo_exists.return_value = True
        progress_var = mock.Mock()
        progress_var.get.return_value = 0
        dialog = {
            "popup": popup,
            "progress": mock.Mock(),
            "progress_var": progress_var,
            "stage_var": mock.Mock(),
            "stage_label": mock.Mock(),
            "detail_var": mock.Mock(),
            "detail_label": mock.Mock(),
        }
        app._create_runtime_progress_dialog = mock.Mock(return_value=dialog)

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            base = Path(tmp)
            live = base / "runtime" / "ComfyUI"
            live.mkdir(parents=True)
            (live / "comfyui_version.py").write_text(
                '__version__ = "0.27.0"\n',
                encoding="utf-8",
            )
            staging = base / f".comfyui-update-staging-{'a' * 32}"
            staging.mkdir()
            prepared = mock.Mock()
            prepared.status = "ready"
            prepared.staging_core = staging
            prepared.dependency_overlay = None
            prepared.overlay_requirements_file = None
            prepared.overlay_command = ()
            prepared.release_metadata = {"version": "0.28.0"}
            prepared.dependency_plan.reasons = ()
            prepared.to_manifest.return_value = {
                "schema_version": 1,
                "status": "ready",
                "base_dir": str(base),
                "staging_core": str(staging),
                "dependency_overlay": "",
                "release_metadata": {
                    "repository_url": "https://github.com/Comfy-Org/ComfyUI",
                    "tag_name": "v0.28.0",
                    "version": "0.28.0",
                    "release_id": 1,
                    "archive_sha256": "b" * 64,
                },
            }

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(
                    main_gateway,
                    "prepare_comfyui_update",
                    return_value=prepared,
                ),
                mock.patch.object(
                    main_gateway,
                    "launch_comfyui_update_worker",
                    return_value=mock.sentinel.worker,
                ) as launch_worker,
                mock.patch.object(main_gateway.os, "getpid", return_value=4321),
                mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
            ):
                app._start_comfyui_update()

            manifest = base / f".comfyui-update-manifest-{'a' * 32}.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ready")
            launch_worker.assert_called_once_with(
                manifest,
                restart_client=None,
                parent_pid=4321,
            )
            app._show_manual_restart_notice.assert_called_once_with(
                dialog,
                component="ComfyUI",
            )
            app._exit_for_runtime_update.assert_called_once_with(popup)
            self.assertTrue(app._runtime_maintenance_in_progress)

            with mock.patch.object(main_gateway, "BASE_DIR", base):
                app._cleanup_comfyui_update_paths(prepared, manifest)
            self.assertFalse(manifest.exists())
            self.assertFalse(staging.exists())

    def test_comfyui_update_race_cancellation_restores_services(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.side_effect = lambda role: role in {
            "comfyui",
            "api",
        }
        app._quiesce_comfyui_for_update = mock.Mock(
            return_value="ComfyUI 队列中还有 1 个生成任务"
        )
        app._stop_runtime_for_maintenance = mock.Mock(return_value="")
        app._end_runtime_maintenance = mock.Mock()
        app._post_to_ui = lambda callback, delay=0: (callback(), True)[1]
        app.after = mock.Mock()
        app._set_comfyui_update_progress = mock.Mock()
        app._stop_runtime_progress_activity = mock.Mock()
        popup = mock.MagicMock()
        dialog = {
            "popup": popup,
            "progress": mock.Mock(),
            "progress_var": mock.Mock(),
            "stage_var": mock.Mock(),
            "stage_label": mock.Mock(),
            "detail_var": mock.Mock(),
            "detail_label": mock.Mock(),
        }
        app._create_runtime_progress_dialog = mock.Mock(return_value=dialog)

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            base = Path(tmp)
            live = base / "runtime" / "ComfyUI"
            live.mkdir(parents=True)
            (live / "comfyui_version.py").write_text(
                '__version__ = "0.27.0"\n', encoding="utf-8"
            )
            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway, "prepare_comfyui_update") as prepare,
                mock.patch.object(main_gateway.messagebox, "showinfo") as showinfo,
                mock.patch.object(threading.Thread, "start", lambda thread: thread.run()),
            ):
                app._start_comfyui_update()

        app._stop_runtime_for_maintenance.assert_not_called()
        prepare.assert_not_called()
        app._end_runtime_maintenance.assert_called_once_with(restart=True)
        self.assertIn("暂不能更新", showinfo.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
