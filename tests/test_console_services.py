import threading
import time
import unittest
import os
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp


class ConsoleServiceTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = object.__new__(GatewayApp)
        app._shutting_down = False
        app._backend_action_lock = threading.Lock()
        app._backend_action_name = ""
        app._runtime_maintenance_active = mock.Mock(return_value=False)
        app._footer_label = mock.Mock()
        app._set_light = mock.Mock()
        app._health_poll_thread = None
        app._health_poll_lock = threading.Lock()
        app._health_poll_generation = 0
        app._health_event_sequence = 0
        app._health_event_applied = 0
        app._health_needs_full_refresh = True
        app._poll_run = False
        app._ensure_health_polling = mock.Mock()
        app._tunnel_url = ""
        app._local_url = main_gateway.API_BASE
        app._api_key = "sk-local-test"
        app._initial_session_sync_done = False
        app._set_public_url = mock.Mock()
        app._set_local_url = mock.Mock()
        app.after = lambda _delay, callback: callback()
        return app

    @staticmethod
    def _wait_until(predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_tunnel_retry_uses_private_admin_key(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            app._retry_tunnel_service()

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer sk-admin-console-test",
        )
        app._ensure_health_polling.assert_called_once()

    def test_regular_health_update_checks_workflow_panel_for_state_changes(self):
        app = self._app()
        app._last_health = {}
        app._update_status = mock.Mock()
        app._update_workflow_display = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._update_task_display = mock.Mock()
        app._update_generation_queue = mock.Mock()
        data = {"workflows": [{"id": "unchanged"}], "current_task": None}

        app._on_health_update(data)

        self.assertIs(app._last_health, data)
        app._update_status.assert_called_once_with(data)
        app._update_workflow_display.assert_called_once_with(data)
        app._dashboard_pages.refresh.assert_called_once_with(data)
        app._update_task_display.assert_called_once_with(None)
        app._update_generation_queue.assert_called_once_with({}, None)

    def test_backend_python_disables_user_site_packages(self):
        app = self._app()

        with mock.patch.dict(os.environ, {"PYTHONNOUSERSITE": "0"}):
            _python_exe, env = app._backend_env()

        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_tunnel_retry_can_reload_persisted_admin_key(self):
        app = self._app()
        app._api_key = "sk-local-stale-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            runtime_state.return_value.admin_key = "sk-admin-persisted-test"
            app._retry_tunnel_service()

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer sk-admin-persisted-test",
        )

    def test_tunnel_retry_without_any_key_fails_without_sending_request(self):
        app = self._app()
        app._api_key = ""

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen") as urlopen,
        ):
            runtime_state.return_value.admin_key = ""
            app._retry_tunnel_service()

        urlopen.assert_not_called()
        app._ensure_health_polling.assert_not_called()
        self.assertTrue(app._backend_action_lock.acquire(blocking=False))
        app._backend_action_lock.release()

    def test_tunnel_retry_reports_server_rejection_without_polling(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        response = mock.Mock()
        response.read.return_value = b'{"ok":false,"error":"restart refused"}'

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", return_value=response),
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            app._retry_tunnel_service()

        app._ensure_health_polling.assert_not_called()
        self.assertTrue(
            any(
                "restart refused" in str(call.kwargs.get("text", ""))
                for call in app._footer_label.config.call_args_list
            )
        )

    def test_tunnel_retry_finishing_during_shutdown_does_not_restart_polling(self):
        app = self._app()
        app._api_key = "sk-local-console-test"
        request_started = threading.Event()
        allow_response = threading.Event()
        errors = []
        response = mock.Mock()
        response.read.return_value = b'{"ok":true}'

        def slow_urlopen(*_args, **_kwargs):
            request_started.set()
            allow_response.wait(1)
            return response

        def run_retry():
            try:
                app._retry_tunnel_service()
            except BaseException as exc:
                errors.append(exc)

        with (
            mock.patch("app.gui.main_gateway.RuntimeState") as runtime_state,
            mock.patch("urllib.request.urlopen", side_effect=slow_urlopen),
        ):
            runtime_state.return_value.admin_key = "sk-admin-console-test"
            worker = threading.Thread(target=run_retry, daemon=True)
            worker.start()
            self.assertTrue(request_started.wait(0.5))
            app._shutting_down = True
            allow_response.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        app._ensure_health_polling.assert_not_called()

    def test_backend_restart_does_not_block_the_ui_caller(self):
        app = self._app()
        app._startup_sequence = mock.Mock()

        def slow_shutdown():
            time.sleep(0.25)
            return ""

        app._shutdown_backend = mock.Mock(side_effect=slow_shutdown)

        started_at = time.monotonic()
        app._restart_backend()
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))

    def test_repeated_restart_clicks_share_one_background_action(self):
        app = self._app()
        app._startup_sequence = mock.Mock()
        shutdown_started = threading.Event()
        allow_shutdown = threading.Event()

        def slow_shutdown():
            shutdown_started.set()
            allow_shutdown.wait(1)
            return ""

        app._shutdown_backend = mock.Mock(side_effect=slow_shutdown)

        app._restart_backend()
        self.assertTrue(shutdown_started.wait(0.5))
        app._restart_backend()

        self.assertEqual(app._shutdown_backend.call_count, 1)
        allow_shutdown.set()
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))

    def test_failed_restart_releases_action_for_a_later_retry(self):
        app = self._app()
        app._startup_sequence = mock.Mock()
        app._shutdown_backend = mock.Mock(
            side_effect=[RuntimeError("service query failed"), ""]
        )

        app._restart_backend()
        self.assertTrue(
            self._wait_until(lambda: app._shutdown_backend.call_count == 1)
        )
        self.assertTrue(
            self._wait_until(
                lambda: app._backend_action_lock.acquire(blocking=False)
            )
        )
        app._backend_action_lock.release()

        app._restart_backend()
        self.assertTrue(self._wait_until(lambda: app._startup_sequence.called))
        app._ensure_health_polling.assert_called()

    def test_initial_startup_blocks_a_concurrent_manual_restart(self):
        app = self._app()
        startup_started = threading.Event()
        allow_startup = threading.Event()
        startup_finished = threading.Event()

        def slow_startup():
            startup_started.set()
            allow_startup.wait(1)
            startup_finished.set()

        app._startup_sequence = slow_startup
        app._shutdown_backend = mock.Mock(return_value="")

        self.assertTrue(app._request_backend_start("启动后台"))
        self.assertTrue(startup_started.wait(0.5))
        app._restart_backend()

        app._shutdown_backend.assert_not_called()
        allow_startup.set()
        self.assertTrue(startup_finished.wait(0.5))

    def test_environment_recheck_does_not_interrupt_startup_probe(self):
        app = self._app()
        startup_started = threading.Event()
        allow_startup = threading.Event()

        def slow_startup():
            startup_started.set()
            allow_startup.wait(1)

        app._startup_sequence = slow_startup
        app._retry_env_check = mock.Mock()

        self.assertTrue(app._request_backend_start("启动后台"))
        self.assertTrue(startup_started.wait(0.5))
        app._start_background_runtime_recheck()

        app._retry_env_check.assert_not_called()
        allow_startup.set()

    def test_health_polling_does_not_start_while_shutting_down(self):
        app = self._app()
        app._shutting_down = True
        app._poll_run = True

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            GatewayApp._ensure_health_polling(app)

        self.assertFalse(app._poll_run)
        thread.assert_not_called()

    def test_health_poll_replacement_supersedes_an_exiting_worker(self):
        app = self._app()
        old_thread = mock.Mock()
        old_thread.is_alive.return_value = True
        app._health_poll_thread = old_thread
        app._health_poll_generation = 1
        app._poll_run = False

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            replacement = mock.Mock()
            thread.return_value = replacement
            GatewayApp._ensure_health_polling(app)

        self.assertEqual(app._health_poll_generation, 2)
        self.assertIs(app._health_poll_thread, replacement)
        replacement.start.assert_called_once()
        self.assertFalse(app._health_poll_active(1))
        self.assertTrue(app._health_poll_active(2))

    def test_api_retry_retires_live_health_poll_before_restarting(self):
        app = self._app()
        old_thread = mock.Mock()
        old_thread.is_alive.return_value = True
        app._health_poll_thread = old_thread
        app._health_poll_generation = 7
        app._poll_run = True
        api_proc = mock.Mock()
        app._api_proc = api_proc
        app._kill_proc = mock.Mock(return_value="")
        app._start_api_service = mock.Mock()
        app._ensure_health_polling = GatewayApp._ensure_health_polling.__get__(app)
        scheduled_delays = []
        app.after = lambda delay, callback: (
            scheduled_delays.append(delay),
            callback(),
        )[1]

        with mock.patch.object(main_gateway.threading, "Thread") as thread:
            replacement = mock.Mock()
            replacement.is_alive.return_value = True
            thread.return_value = replacement
            app._retry_api_service()

        app._kill_proc.assert_called_once_with(api_proc, "API")
        app._start_api_service.assert_called_once_with()
        self.assertEqual(app._health_poll_generation, 9)
        self.assertIs(app._health_poll_thread, replacement)
        replacement.start.assert_called_once_with()
        self.assertNotIn(200, scheduled_delays)
        self.assertFalse(app._finish_health_poll(7))
        self.assertTrue(app._poll_run)

    def test_queued_old_health_snapshot_cannot_overwrite_replacement(self):
        app = self._app()
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        app._health_poll_generation = 2
        app._poll_run = True

        app._deliver_health_snapshot(1, {"status": "stale"}, first=True)
        app._deliver_health_snapshot(1, {"status": "stale"})

        app._on_first_health.assert_not_called()
        app._on_health_update.assert_not_called()

    def test_queued_old_health_failure_cannot_clear_replacement(self):
        app = self._app()
        app._on_server_unreachable = mock.Mock()
        app._health_poll_generation = 2
        app._poll_run = True

        app._deliver_health_failure(1)

        app._on_server_unreachable.assert_not_called()

    def test_true_liveness_failure_marks_unreachable_but_worker_keeps_running(self):
        app = self._app()
        app._ensure_health_polling = GatewayApp._ensure_health_polling.__get__(app)
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        app._on_server_unreachable = mock.Mock()
        response = mock.Mock()
        response.read.return_value = b'{"status":"ok"}'
        status_calls = 0

        def urlopen(request, timeout):
            nonlocal status_calls
            if request.full_url.endswith("/v1/status"):
                status_calls += 1
                if status_calls == 1:
                    return response
            raise OSError("down")

        active_when_reported = []
        app._on_server_unreachable.side_effect = lambda: active_when_reported.append(
            app._poll_run
        )
        sleep_calls = 0

        def stop_after_outage(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 4:
                app._poll_run = False

        with (
            mock.patch("urllib.request.urlopen", side_effect=urlopen),
            mock.patch.object(
                main_gateway.time,
                "sleep",
                side_effect=stop_after_outage,
            ),
        ):
            app._poll_health()

        app._on_server_unreachable.assert_called_once()
        self.assertEqual(active_when_reported, [True])

    def test_slow_status_with_live_api_keeps_polling_and_recovers(self):
        app = self._app()
        app._ensure_health_polling = GatewayApp._ensure_health_polling.__get__(app)
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        app._on_health_degraded = mock.Mock()
        app._on_server_unreachable = mock.Mock()
        status_response = mock.Mock()
        status_response.read.return_value = b'{"status":"ok"}'
        health_response = mock.Mock()
        health_response.read.return_value = b'{"status":"ok"}'
        status_calls = 0

        def urlopen(request, timeout):
            nonlocal status_calls
            if request.full_url.endswith("/health"):
                return health_response
            status_calls += 1
            if 2 <= status_calls <= 4:
                raise TimeoutError("status is still being assembled")
            return status_response

        sleep_calls = 0

        def stop_after_recovery(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 5:
                app._poll_run = False

        with (
            mock.patch("urllib.request.urlopen", side_effect=urlopen),
            mock.patch.object(main_gateway.time, "sleep", side_effect=stop_after_recovery),
        ):
            app._poll_health()

        app._on_first_health.assert_called_once()
        app._on_health_degraded.assert_called_once()
        app._on_health_update.assert_called_once()
        app._on_server_unreachable.assert_not_called()

    def test_health_degraded_preserves_last_trusted_components(self):
        app = self._app()
        app._tunnel_url = "https://still-live.example"
        app._clear_public_url = mock.Mock()

        app._on_health_degraded()

        self.assertEqual(app._api_key, "sk-local-test")
        self.assertEqual(app._tunnel_url, "https://still-live.example")
        app._clear_public_url.assert_not_called()
        app._set_light.assert_called_once_with("api", "online", "状态同步中")

    def test_late_degraded_event_cannot_overwrite_newer_success(self):
        app = self._app()
        app._health_poll_generation = 4
        app._poll_run = True
        app._health_needs_full_refresh = False
        app._on_health_update = mock.Mock()
        app._on_health_degraded = mock.Mock()
        degraded_sequence = app._next_health_event_sequence()
        success_sequence = app._next_health_event_sequence()

        app._deliver_health_snapshot(
            4,
            {"status": "ok"},
            sequence=success_sequence,
        )
        app._deliver_health_degraded(
            4,
            "stale",
            sequence=degraded_sequence,
        )

        app._on_health_update.assert_called_once()
        app._on_health_degraded.assert_not_called()

    def test_dropped_first_snapshot_keeps_full_refresh_required(self):
        app = self._app()
        app._health_poll_generation = 3
        app._poll_run = True
        app._on_first_health = mock.Mock()
        app._on_health_update = mock.Mock()
        dropped_sequence = app._next_health_event_sequence()
        delivered_sequence = app._next_health_event_sequence()

        # The first callback is intentionally never delivered.
        self.assertLess(dropped_sequence, delivered_sequence)
        app._deliver_health_snapshot(
            3,
            {"status": "ok"},
            sequence=delivered_sequence,
        )

        app._on_first_health.assert_called_once()
        app._on_health_update.assert_not_called()
        self.assertFalse(app._health_needs_full_refresh)

    def test_applied_outage_requires_full_refresh_on_recovery(self):
        app = self._app()
        app._health_poll_generation = 6
        app._poll_run = True
        app._health_needs_full_refresh = False
        app._on_server_unreachable = mock.Mock()
        app._on_first_health = mock.Mock()
        failure_sequence = app._next_health_event_sequence()
        recovery_sequence = app._next_health_event_sequence()

        app._deliver_health_failure(6, sequence=failure_sequence)
        app._deliver_health_snapshot(
            6,
            {"status": "ok"},
            sequence=recovery_sequence,
        )

        app._on_server_unreachable.assert_called_once()
        app._on_first_health.assert_called_once()
        self.assertFalse(app._health_needs_full_refresh)

    def test_initial_health_uses_consecutive_failures_not_sticky_success(self):
        app = self._app()
        app._on_server_unreachable = mock.Mock()
        health_response = mock.Mock()
        health_response.read.return_value = b'{"status":"ok"}'
        health_calls = 0

        def urlopen(request, timeout):
            nonlocal health_calls
            if request.full_url.endswith("/health"):
                health_calls += 1
                if health_calls == 1:
                    return health_response
            raise OSError("down")

        sleep_calls = 0

        def stop_after_report(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 61:
                app._poll_run = False

        with (
            mock.patch("urllib.request.urlopen", side_effect=urlopen),
            mock.patch.object(
                main_gateway.time,
                "sleep",
                side_effect=stop_after_report,
            ),
        ):
            app._poll_health()

        app._on_server_unreachable.assert_called_once()

    def test_health_errors_have_user_facing_recovery_messages(self):
        unauthorized = type("HttpError", (), {"code": 401})()
        rate_limited = type(
            "HttpError",
            (),
            {"code": 429, "headers": {"Retry-After": "12"}},
        )()
        server_error = type("HttpError", (), {"code": 503})()

        self.assertIn("访问密钥", GatewayApp._health_degraded_message(unauthorized))
        self.assertIn("较频繁", GatewayApp._health_degraded_message(rate_limited))
        self.assertIn("状态接口", GatewayApp._health_degraded_message(server_error))
        self.assertEqual(
            GatewayApp._health_retry_delay(rate_limited, 3.0),
            12.0,
        )

    def test_malformed_health_error_metadata_cannot_break_retry_logic(self):
        malformed_code = type(
            "MalformedHttpError",
            (),
            {"code": object()},
        )()
        malformed_headers = type(
            "MalformedHeadersError",
            (),
            {
                "code": 429,
                "headers": type(
                    "BadHeaders",
                    (),
                    {"get": lambda self, _name: (_ for _ in ()).throw(RuntimeError())},
                )(),
            },
        )()

        self.assertIn(
            "自动重试",
            GatewayApp._health_degraded_message(malformed_code),
        )
        self.assertEqual(
            GatewayApp._health_retry_delay(malformed_headers, 3.0),
            3.0,
        )

    def test_unreachable_health_preserves_in_memory_api_key_for_recovery(self):
        app = self._app()
        app._api_key = "sk-local-memory-fallback"
        app._clear_public_url = mock.Mock()
        app._key_label = mock.Mock()
        app._url_label = mock.Mock()

        app._on_server_unreachable()

        self.assertEqual(app._api_key, "sk-local-memory-fallback")

    def test_health_status_adopts_reported_local_api(self):
        app = self._app()
        app._server_mode = "guest"
        app._server_session_token = ""
        app._comfy_proc = None
        app._comfy_starting_until = 0

        app._update_status(
            {
                "base_url": "",
                "local_api": "http://127.0.0.1:19001",
                "tunnel": {"status": "starting"},
                "comfyui": {"status": "online"},
            }
        )

        app._set_local_url.assert_called_once_with("http://127.0.0.1:19001")

    def test_offline_tunnel_clears_stale_public_url_but_preserves_local_api(self):
        app = self._app()
        app._tunnel_url = "https://stale.example"
        app._local_url = "http://127.0.0.1:19001"
        app._initial_session_sync_done = True
        app._set_public_url = mock.Mock()
        app._set_local_url = mock.Mock()
        app._server_mode = "guest"
        app._server_session_token = ""
        app._api_key = ""
        app._comfy_proc = None
        app._comfy_starting_until = 0

        app._update_status(
            {
                "base_url": "https://stale.example",
                "local_api": "http://127.0.0.1:19001",
                "tunnel": {"status": "offline"},
                "comfyui": {"status": "online"},
            }
        )

        self.assertEqual(app._tunnel_url, "")
        app._set_public_url.assert_called_once_with("")
        app._set_local_url.assert_called_once_with("http://127.0.0.1:19001")
        self.assertNotIn(mock.call(""), app._set_local_url.call_args_list)
        self.assertIn(
            mock.call("tunnel", "offline", "未连接"),
            app._set_light.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
