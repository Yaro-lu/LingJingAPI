import json
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core import runtime_update
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = REPOSITORY_ROOT / "app" / "core" / "runtime_update_helper.ps1"


def _write_runtime_fixture(root: Path, payload: bytes):
    for relative in REQUIRED_RUNTIME_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _prepare_handoff(tmp: str, operation_id: str = "a" * 32):
    base = Path(tmp) / "client"
    helper = base / runtime_update.RUNTIME_UPDATE_HELPER
    helper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HELPER_SOURCE, helper)
    staging = base / f".runtime-install-staging-{operation_id}"
    _write_runtime_fixture(staging, b"new-runtime")
    return base, staging, helper


def _query_task_registration(task_name: str) -> dict[str, object]:
    powershell = Path(shutil.which("powershell.exe"))
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskName = [Environment]::GetEnvironmentVariable('LINGJING_TEST_TASK_NAME', 'Process')
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$root = $service.GetFolder('\')
try {
    $task = $root.GetTask($taskName)
    [ordered]@{ Exists = $true; HResult = 0; Name = [string]$task.Name } |
        ConvertTo-Json -Compress
}
catch {
    [ordered]@{
        Exists = $false
        HResult = [int]$_.Exception.HResult
        Message = [string]$_.Exception.Message
    } | ConvertTo-Json -Compress
}
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = dict(os.environ)
    environment["LINGJING_TEST_TASK_NAME"] = task_name
    completed = subprocess.run(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line.strip().lstrip("\ufeff"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"Task Scheduler lookup returned no JSON: {completed.stdout!r}")


class RuntimeUpdateLaunchTests(unittest.TestCase):
    def test_helper_source_is_ascii_for_windows_powershell_51(self):
        self.assertTrue(
            HELPER_SOURCE.read_bytes().isascii(),
            "Windows PowerShell 5.1 may parse a BOM-less script as ANSI",
        )

    def test_helper_requires_restarted_gui_ready_ack(self):
        source = HELPER_SOURCE.read_text(encoding="ascii")
        for marker in (
            "LINGJING_RUNTIME_UPDATE_OPERATION_ID",
            "runtime-update-ready-$OperationId.json",
            "runtime-update-started-$OperationId.json",
            "Write-StartedMarker",
            "Confirm-ClientReady",
            "-PassThru",
            "RESTART_READY",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_helper_rechecks_parent_pid_after_wait_on_windows_powershell_51(self):
        source = HELPER_SOURCE.read_text(encoding="ascii")
        self.assertIn("function Get-ProcessIfRunningStrict", source)
        self.assertIn("catch [System.ArgumentException]", source)
        self.assertIn("$parentAfterWait = Get-ProcessIfRunningStrict", source)
        self.assertNotIn("Get-Process -Id $ParentPid -ErrorAction SilentlyContinue", source)
        self.assertNotIn("if (-not $parent.HasExited)", source)
        self.assertNotIn("-and -not $runningParent.HasExited", source)

    def test_helper_requires_pid_bound_commit_before_replacing_runtime(self):
        source = HELPER_SOURCE.read_text(encoding="ascii")
        for marker in (
            "runtime-update-commit-$OperationId.json",
            "Confirm-CommitMarker",
            "commit.process_id",
            "COMMIT_CONFIRMED",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

    def test_handoff_accepts_only_exact_direct_staging_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, helper = _prepare_handoff(tmp)

            actual = runtime_update.validate_runtime_update_handoff(base, staging)

            self.assertEqual(actual, (base.resolve(), staging.resolve(), helper.resolve(), "a" * 32))

    def test_handoff_rejects_staging_outside_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _staging, _helper = _prepare_handoff(tmp)
            outside = Path(tmp) / f".runtime-install-staging-{'b' * 32}"
            _write_runtime_fixture(outside, b"new-runtime")

            with self.assertRaisesRegex(ValueError, "直接子目录"):
                runtime_update.validate_runtime_update_handoff(base, outside)

    def test_handoff_rejects_unexpected_staging_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _staging, _helper = _prepare_handoff(tmp)
            staging = base / "runtime-staging"
            _write_runtime_fixture(staging, b"new-runtime")

            with self.assertRaisesRegex(ValueError, "名称无效"):
                runtime_update.validate_runtime_update_handoff(base, staging)

    def test_powershell_discovery_uses_built_in_windows_51_for_portability(self):
        with tempfile.TemporaryDirectory() as tmp:
            windows_root = Path(tmp) / "Windows"
            legacy = (
                windows_root
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            pwsh = Path(tmp) / "PowerShell" / "7" / "pwsh.exe"
            pwsh.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            pwsh.write_bytes(b"pwsh-7")
            legacy.write_bytes(b"powershell-5")

            actual = runtime_update.find_windows_powershell(
                environ={"SystemRoot": str(windows_root)},
                which=lambda name: str(pwsh) if name == "pwsh.exe" else None,
            )

            self.assertEqual(actual, legacy.resolve())

    def test_command_is_shell_free_and_pins_validated_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")

            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=4321,
                powershell_path=powershell,
            )

            self.assertIsInstance(command, list)
            self.assertEqual(command[0], str(powershell.resolve()))
            self.assertEqual(command[command.index("-File") + 1], str(helper.resolve()))
            self.assertEqual(command[command.index("-BaseDir") + 1], str(base.resolve()))
            self.assertEqual(command[command.index("-StagingDir") + 1], str(staging.resolve()))
            self.assertEqual(command[command.index("-OperationId") + 1], "a" * 32)
            self.assertIn("-NoRestart", command)

    def test_quick_repair_can_request_a_verified_client_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            command = runtime_update.build_runtime_update_command(
                base, staging, parent_pid=4321, powershell_path=powershell,
                restart_client=True,
            )
            self.assertNotIn("-NoRestart", command)
            values = runtime_update._validated_runtime_update_values(
                command, base_dir=base, powershell_path=powershell,
            )
            self.assertTrue(values["restart_client"])
            arguments, _operation = runtime_update._task_action_arguments(
                command, base_dir=base, powershell_path=powershell,
            )
            encoded = arguments.rsplit(" ", 1)[-1].strip('"')
            self.assertNotIn("-NoRestart", base64.b64decode(encoded).decode("utf-16le"))

    def test_launch_is_hidden_and_not_registered_with_process_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            process = mock.Mock(pid=5678)
            popen = mock.Mock(return_value=process)
            started_waiter = mock.Mock()
            started_marker, started_temporary = runtime_update._helper_started_paths(
                base,
                "a" * 32,
            )
            started_marker.parent.mkdir(parents=True, exist_ok=True)
            started_marker.write_text("stale", encoding="ascii")
            started_temporary.write_text("stale", encoding="ascii")

            def assert_markers_cleared(*_args, **_kwargs):
                self.assertFalse(started_marker.exists())
                self.assertFalse(started_temporary.exists())
                return process

            popen.side_effect = assert_markers_cleared

            with mock.patch.object(runtime_update.os, "name", "nt"):
                result = runtime_update.launch_runtime_update(
                    base,
                    staging,
                    parent_pid=4321,
                    powershell_path=powershell,
                    popen_factory=popen,
                    started_waiter=started_waiter,
                )

            self.assertIs(result, process)
            args, kwargs = popen.call_args
            self.assertIsInstance(args[0], list)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["cwd"], str(base.resolve()))
            self.assertFalse(kwargs["creationflags"] & 0x00000008)
            self.assertTrue(kwargs["creationflags"] & 0x08000000)
            self.assertTrue(kwargs["creationflags"] & 0x00000200)
            self.assertTrue(kwargs["creationflags"] & 0x01000000)
            self.assertTrue(kwargs["startupinfo"].dwFlags & 0x00000001)
            self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            started_waiter.assert_called_once_with(base.resolve(), "a" * 32, 5678)
            commit_marker, _commit_temporary = runtime_update._helper_commit_paths(
                base,
                "a" * 32,
            )
            commit = json.loads(commit_marker.read_text(encoding="utf-8"))
            self.assertEqual(commit["operation_id"], "a" * 32)
            self.assertEqual(commit["process_id"], 5678)

    def test_launch_uses_task_scheduler_when_job_policy_denies_breakaway(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            denied = OSError("breakaway denied")
            denied.winerror = 5
            popen = mock.Mock(side_effect=denied)
            task_launcher = mock.Mock(return_value=mock.sentinel.process)

            with mock.patch.object(runtime_update.os, "name", "nt"):
                result = runtime_update.launch_runtime_update(
                    base,
                    staging,
                    parent_pid=4321,
                    powershell_path=powershell,
                    popen_factory=popen,
                    task_launcher=task_launcher,
                )

            self.assertIs(result, mock.sentinel.process)
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x01000000)
            task_launcher.assert_called_once_with(
                command,
                base_dir=base.resolve(),
                powershell_path=powershell.resolve(),
            )

    def test_launch_falls_back_when_direct_helper_exits_without_acknowledging(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            process = mock.Mock(pid=5678)
            process.poll.return_value = 1
            popen = mock.Mock(return_value=process)
            started_waiter = mock.Mock(
                side_effect=RuntimeError("helper did not acknowledge")
            )
            task_launcher = mock.Mock(return_value=mock.sentinel.scheduled_process)

            with mock.patch.object(runtime_update.os, "name", "nt"):
                result = runtime_update.launch_runtime_update(
                    base,
                    staging,
                    parent_pid=4321,
                    powershell_path=powershell,
                    popen_factory=popen,
                    task_launcher=task_launcher,
                    started_waiter=started_waiter,
                )

            self.assertIs(result, mock.sentinel.scheduled_process)
            process.terminate.assert_not_called()
            command = popen.call_args.args[0]
            task_launcher.assert_called_once_with(
                command,
                base_dir=base.resolve(),
                powershell_path=powershell.resolve(),
            )

    def test_launch_stops_live_unacknowledged_helper_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            process = mock.Mock(pid=6789)
            process.poll.return_value = None
            process.wait.return_value = 0
            task_launcher = mock.Mock(return_value=mock.sentinel.scheduled_process)

            with mock.patch.object(runtime_update.os, "name", "nt"):
                result = runtime_update.launch_runtime_update(
                    base,
                    staging,
                    parent_pid=4321,
                    powershell_path=powershell,
                    popen_factory=mock.Mock(return_value=process),
                    task_launcher=task_launcher,
                    started_waiter=mock.Mock(
                        side_effect=RuntimeError("helper did not acknowledge")
                    ),
                )

            self.assertIs(result, mock.sentinel.scheduled_process)
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=3.0)
            task_launcher.assert_called_once()

    def test_task_broker_is_hidden_and_passes_encoded_unicode_safe_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = Path(tmp) / "灵境 客户端"
            container.mkdir()
            base, staging, _helper = _prepare_handoff(str(container))
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=4321,
                powershell_path=powershell,
            )
            started_marker, started_temporary = runtime_update._helper_started_paths(
                base,
                "a" * 32,
            )
            started_marker.parent.mkdir(parents=True, exist_ok=True)
            started_marker.write_text("stale", encoding="ascii")
            started_temporary.write_text("stale", encoding="ascii")
            commit_marker = base / "runtime" / f"runtime-update-commit-{'a' * 32}.json"
            commit_temporary = base / "runtime" / f"runtime-update-commit-{'a' * 32}.tmp"
            commit_marker.write_text("stale", encoding="ascii")
            commit_temporary.write_text("stale", encoding="ascii")

            def run_broker(*_args, **_kwargs):
                self.assertFalse(started_marker.exists())
                self.assertFalse(started_temporary.exists())
                self.assertFalse(commit_marker.exists())
                self.assertFalse(commit_temporary.exists())
                return mock.Mock(
                    returncode=0,
                    stdout=(
                        '{"EnginePID":9876,"InstanceGuid":"fixture",'
                        '"OperationId":"' + "a" * 32 + '","Acknowledged":true}\n'
                    ),
                    stderr="",
                )

            runner = mock.Mock(side_effect=run_broker)

            result = runtime_update._launch_via_task_scheduler(
                command,
                base_dir=base,
                powershell_path=powershell,
                runner=runner,
            )

            self.assertEqual(result.pid, 9876)
            self.assertEqual(result.task_name, f"LingJingAI-RuntimeUpdate-{'a' * 32}")
            args, kwargs = runner.call_args
            self.assertEqual(args[0][0], str(powershell))
            self.assertIn("-EncodedCommand", args[0])
            target_arguments = kwargs["env"]["LINGJING_TASK_ARGUMENTS"]
            self.assertTrue(target_arguments.isascii())
            self.assertLessEqual(len(target_arguments), 8192)
            self.assertNotIn("灵境 客户端", target_arguments)
            self.assertIn("-WindowStyle Hidden", target_arguments)
            self.assertIn("-EncodedCommand", target_arguments)
            self.assertEqual(kwargs["env"]["LINGJING_TASK_EXECUTABLE"], str(powershell.resolve()))
            self.assertEqual(kwargs["env"]["LINGJING_TASK_CWD"], str(powershell.parent.resolve()))
            self.assertEqual(
                kwargs["env"]["LINGJING_TASK_NAME"],
                f"LingJingAI-RuntimeUpdate-{'a' * 32}",
            )
            self.assertEqual(
                kwargs["env"]["LINGJING_TASK_ACK_PATH"],
                str(started_marker.resolve()),
            )
            self.assertEqual(kwargs["env"]["LINGJING_TASK_OPERATION_ID"], "a" * 32)
            self.assertIn("$definition.Settings.Hidden = $true", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$definition.Principal.LogonType = 3", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("Stop-RunningTaskChecked", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$ack.process_id", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$ack.operation_id", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("for ($attempt = 1; $attempt -le 5; $attempt++)", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$root.DeleteTask($taskName, 0)", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$root.GetTask($taskName)", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$missingHResults = @(-2147024894)", runtime_update._TASK_BROKER_SCRIPT)
            self.assertNotIn("-2147216625", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("$RunningTask.Stop()", runtime_update._TASK_BROKER_SCRIPT)
            self.assertIn("Task Scheduler cleanup failed", runtime_update._TASK_BROKER_SCRIPT)
            self.assertNotIn(
                "try { $root.DeleteTask($taskName, 0) } catch {}",
                runtime_update._TASK_BROKER_SCRIPT,
            )
            self.assertTrue(kwargs["creationflags"] & 0x08000000)
            self.assertNotIn("shell", kwargs)
            commit = json.loads(commit_marker.read_text(encoding="utf-8"))
            self.assertEqual(commit["process_id"], 9876)

    def test_task_broker_result_requires_pid_bound_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=4321,
                powershell_path=powershell,
            )
            runner = mock.Mock(
                return_value=mock.Mock(
                    returncode=0,
                    stdout='{"EnginePID":9876,"InstanceGuid":"fixture"}\n',
                    stderr="",
                )
            )

            with self.assertRaisesRegex(RuntimeError, "确认信息"):
                runtime_update._launch_via_task_scheduler(
                    command,
                    base_dir=base,
                    powershell_path=powershell,
                    runner=runner,
                )

            commit_marker = (
                base / "runtime" / f"runtime-update-commit-{'a' * 32}.json"
            )
            self.assertFalse(commit_marker.exists())

    def test_task_action_rejects_unexpected_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=4321,
                powershell_path=powershell,
            )
            command.append("-Unexpected")

            with self.assertRaisesRegex(ValueError, "结构无效"):
                runtime_update._task_action_arguments(
                    command,
                    base_dir=base,
                    powershell_path=powershell,
                )

    def test_started_ack_rejects_wrong_scheduler_pid(self):
        operation_id = "3" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            marker, _temporary = runtime_update._helper_started_paths(base, operation_id)
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "process_id": 1111,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "未启动"):
                runtime_update._wait_for_helper_started(
                    base,
                    operation_id,
                    2222,
                    timeout_seconds=0.15,
                )

            self.assertTrue(marker.exists(), "mismatched ACK must not be consumed")
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "process_id": 2222,
                    }
                ),
                encoding="utf-8",
            )
            payload = runtime_update._wait_for_helper_started(
                base,
                operation_id,
                2222,
                timeout_seconds=0.5,
            )
            self.assertEqual(payload["process_id"], 2222)
            self.assertFalse(marker.exists())

    def test_launch_does_not_hide_unrelated_create_process_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp)
            powershell = Path(tmp) / "powershell.exe"
            powershell.write_bytes(b"fixture")
            missing_executable = OSError("missing executable")
            missing_executable.winerror = 2
            popen = mock.Mock(side_effect=missing_executable)

            with mock.patch.object(runtime_update.os, "name", "nt"):
                with self.assertRaises(OSError):
                    runtime_update.launch_runtime_update(
                        base,
                        staging,
                        parent_pid=4321,
                        powershell_path=powershell,
                        popen_factory=popen,
                    )

            popen.assert_called_once()

    def test_result_is_consumed_once_from_fixed_runtime_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result_path = base / runtime_update.RUNTIME_UPDATE_RESULT
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps({"schema_version": 1, "success": True, "message": "done"}),
                encoding="utf-8",
            )

            self.assertEqual(runtime_update.consume_runtime_update_result(base)["message"], "done")
            self.assertFalse(result_path.exists())
            self.assertIsNone(runtime_update.consume_runtime_update_result(base))

    def test_gui_never_replaces_its_own_runtime_in_process(self):
        source = (REPOSITORY_ROOT / "app" / "gui" / "main_gateway.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("install_staged_runtime(", source)
        self.assertIn("launch_runtime_update(", source)
        self.assertIn("_exit_for_runtime_update", source)


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is required")
class RuntimeUpdateHelperIntegrationTests(unittest.TestCase):
    def _run_helper(
        self,
        base: Path,
        staging: Path,
        operation_id: str,
        *,
        authorize: bool = True,
    ):
        command = [
                shutil.which("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(base / runtime_update.RUNTIME_UPDATE_HELPER),
                "-ParentPid",
                "2147483000",
                "-BaseDir",
                str(base),
                "-StagingDir",
                str(staging),
                "-OperationId",
                operation_id,
                "-WaitTimeoutSeconds",
                "30",
                "-NoRestart",
            ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
        if authorize:
            runtime_update._write_helper_commit(base, operation_id, process.pid)
        stdout, stderr = process.communicate(timeout=60)
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    def test_helper_swaps_only_runtime_roots_and_preserves_user_data(self):
        operation_id = "c" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            (base / ".venv" / "Scripts").mkdir(parents=True)
            (base / ".venv" / "Scripts" / "python.exe").write_bytes(b"legacy")
            (base / ".venv" / "pyvenv.cfg").write_text("legacy", encoding="utf-8")
            (base / "models").mkdir()
            (base / "models" / "keep.bin").write_bytes(b"user-model")

            completed = self._run_helper(base, staging, operation_id)

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            for relative in REQUIRED_RUNTIME_PATHS:
                self.assertEqual((base / relative).read_bytes(), b"new-runtime")
            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"user-model")
            self.assertFalse((base / ".venv" / "Scripts").exists())
            self.assertFalse((base / ".venv" / "pyvenv.cfg").exists())
            self.assertFalse(staging.exists())
            self.assertFalse((base / f".runtime-install-backup-{operation_id}").exists())
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(encoding="utf-8-sig")
            )
            self.assertTrue(result["success"])
            self.assertFalse(result["rolled_back"])

    def test_helper_rolls_back_completed_moves_after_later_failure(self):
        operation_id = "d" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            (base / "models").mkdir()
            (base / "models" / "keep.bin").write_bytes(b"user-model")
            shutil.rmtree(base / "bin")
            (base / "bin").write_bytes(b"unmanaged-parent-collision")

            completed = self._run_helper(base, staging, operation_id)

            self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
            for relative in REQUIRED_RUNTIME_PATHS[:-1]:
                self.assertEqual((base / relative).read_bytes(), b"old-runtime")
            self.assertEqual((base / "bin").read_bytes(), b"unmanaged-parent-collision")
            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"user-model")
            self.assertFalse(staging.exists())
            self.assertFalse((base / f".runtime-install-backup-{operation_id}").exists())
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(encoding="utf-8-sig")
            )
            self.assertFalse(result["success"])
            self.assertTrue(result["rolled_back"])

    def test_helper_records_preflight_failure_and_persistent_log(self):
        operation_id = "e" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            missing = staging / "runtime" / "ComfyUI" / "main.py"
            missing.unlink()

            completed = self._run_helper(base, staging, operation_id)

            self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(encoding="utf-8-sig")
            )
            self.assertFalse(result["success"])
            self.assertFalse(result["rolled_back"])
            self.assertEqual(result["result_code"], "preflight_failed")
            self.assertIn("Runtime staging is incomplete", result["message"])
            log = (base / "runtime" / "runtime-update-helper.log").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn(operation_id, log)
            self.assertIn("PREFLIGHT_FAILED", log)
            self.assertIn("Runtime staging is incomplete", log)

    def test_helper_refuses_to_replace_runtime_without_pid_bound_commit(self):
        operation_id = "9" * 32
        with tempfile.TemporaryDirectory() as tmp:
            base, staging, _helper = _prepare_handoff(tmp, operation_id)
            _write_runtime_fixture(base, b"old-runtime")

            completed = self._run_helper(
                base,
                staging,
                operation_id,
                authorize=False,
            )

            self.assertEqual(completed.returncode, 1, completed.stderr or completed.stdout)
            for relative in REQUIRED_RUNTIME_PATHS:
                self.assertEqual((base / relative).read_bytes(), b"old-runtime")
            self.assertTrue(staging.exists(), "uncommitted staging must remain retryable")
            result = json.loads(
                (base / runtime_update.RUNTIME_UPDATE_RESULT).read_text(
                    encoding="utf-8-sig"
                )
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["result_code"], "preflight_failed")
            self.assertIn("commit was not confirmed", result["message"])

    def test_task_scheduler_handoff_applies_from_unicode_space_path_without_restart(self):
        operation_id = "f" * 32
        with tempfile.TemporaryDirectory() as tmp:
            container = Path(tmp) / "带空格 中文路径"
            container.mkdir()
            base, staging, _helper = _prepare_handoff(str(container), operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            powershell = Path(shutil.which("powershell.exe"))
            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=2147483000,
                powershell_path=powershell,
                wait_timeout_seconds=30,
            )

            process = runtime_update._launch_via_task_scheduler(
                command,
                base_dir=base,
                powershell_path=powershell,
            )

            result_path = base / runtime_update.RUNTIME_UPDATE_RESULT
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not result_path.is_file():
                time.sleep(0.1)
            diagnostic_logs = []
            for log_name in (
                "runtime-update-bootstrap.log",
                "runtime-update-helper.log",
            ):
                log_path = base / "runtime" / log_name
                if log_path.is_file():
                    diagnostic_logs.append(
                        f"{log_name}: {log_path.read_text(encoding='utf-8-sig', errors='replace')}"
                    )
            self.assertTrue(
                result_path.is_file(),
                "Task Scheduler helper did not finish; "
                f"logs={diagnostic_logs or ['<none>']}",
            )
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
            self.assertTrue(payload["success"], payload)
            for relative in REQUIRED_RUNTIME_PATHS:
                self.assertEqual((base / relative).read_bytes(), b"new-runtime")
            self.assertFalse(staging.exists())
            log = (base / "runtime" / "runtime-update-helper.log").read_text(
                encoding="utf-8-sig"
            )
            self.assertIn("STARTED_ACK", log)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "RESTART_SKIPPED" not in log:
                time.sleep(0.1)
                log = (base / "runtime" / "runtime-update-helper.log").read_text(
                    encoding="utf-8-sig"
                )
            self.assertIn("RESTART_SKIPPED no-restart requested", log)
            registration = _query_task_registration(process.task_name)
            self.assertFalse(registration["Exists"], registration)
            self.assertEqual(registration["HResult"], -2147024894)

    def test_task_scheduler_stops_unacknowledged_helper_before_deleting_task(self):
        import psutil

        operation_id = "4" * 32
        with tempfile.TemporaryDirectory() as tmp:
            container = Path(tmp) / "未确认 helper 中文 空格"
            container.mkdir()
            base, staging, helper = _prepare_handoff(str(container), operation_id)
            pid_file = base / "unacknowledged-helper-pid.txt"
            helper.write_text(
                "$base = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)\n"
                "$pidFile = Join-Path $base 'unacknowledged-helper-pid.txt'\n"
                "Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII\n"
                "Start-Sleep -Seconds 60\n",
                encoding="ascii",
            )
            powershell = Path(shutil.which("powershell.exe"))
            command = runtime_update.build_runtime_update_command(
                base,
                staging,
                parent_pid=2147483000,
                powershell_path=powershell,
                wait_timeout_seconds=30,
            )
            task_name = f"LingJingAI-RuntimeUpdate-{operation_id}"

            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                runtime_update._launch_via_task_scheduler(
                    command,
                    base_dir=base,
                    powershell_path=powershell,
                )

            self.assertTrue(pid_file.is_file(), "fake helper never started")
            helper_pid = int(pid_file.read_text(encoding="ascii").strip())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and psutil.pid_exists(helper_pid):
                time.sleep(0.1)
            self.assertFalse(
                psutil.pid_exists(helper_pid),
                f"unacknowledged helper PID {helper_pid} was left running",
            )
            registration = _query_task_registration(task_name)
            self.assertFalse(registration["Exists"], registration)

    def test_task_scheduler_helper_survives_restrictive_gui_job_close(self):
        import ctypes
        from ctypes import wintypes
        import psutil

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        operation_id = "2" * 32
        child = None
        job = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        with tempfile.TemporaryDirectory() as tmp:
            container = Path(tmp) / "受限 Job 中文 空格"
            container.mkdir()
            base, staging, _helper = _prepare_handoff(str(container), operation_id)
            _write_runtime_fixture(base, b"old-runtime")
            powershell = Path(shutil.which("powershell.exe"))
            go = base / "test-go"
            ready = base / "test-handoff-ready.json"
            allow_exit = base / "test-allow-exit"
            result_path = base / runtime_update.RUNTIME_UPDATE_RESULT
            child_source = r"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

repo, base, staging, go, ready, allow_exit, powershell = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo))
from app.core import runtime_update

deadline = time.monotonic() + 30
while time.monotonic() < deadline and not go.is_file():
    time.sleep(0.05)
if not go.is_file():
    raise SystemExit(10)
try:
    process = runtime_update.launch_runtime_update(
        base,
        staging,
        parent_pid=os.getpid(),
        powershell_path=powershell,
    )
    ready.write_text(
        json.dumps(
            {
                'type': type(process).__name__,
                'pid': getattr(process, 'pid', 0),
                'task_name': getattr(process, 'task_name', ''),
            }
        ),
        encoding='utf-8',
    )
except Exception:
    ready.write_text(
        json.dumps({'error': traceback.format_exc()}),
        encoding='utf-8',
    )
    raise SystemExit(11)
deadline = time.monotonic() + 30
while time.monotonic() < deadline and not allow_exit.is_file():
    time.sleep(0.05)
raise SystemExit(0 if allow_exit.is_file() else 12)
"""
            try:
                job = kernel32.CreateJobObjectW(None, None)
                self.assertTrue(job, f"CreateJobObjectW failed: {ctypes.get_last_error()}")
                limits = ExtendedLimitInformation()
                limits.BasicLimitInformation.LimitFlags = 0x00002000
                self.assertTrue(
                    kernel32.SetInformationJobObject(
                        job,
                        9,
                        ctypes.byref(limits),
                        ctypes.sizeof(limits),
                    ),
                    f"SetInformationJobObject failed: {ctypes.get_last_error()}",
                )
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-s",
                        "-B",
                        "-c",
                        child_source,
                        str(REPOSITORY_ROOT),
                        str(base),
                        str(staging),
                        str(go),
                        str(ready),
                        str(allow_exit),
                        str(powershell),
                    ],
                    cwd=str(base),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
                self.assertTrue(
                    kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(child._handle))),
                    f"AssignProcessToJobObject failed: {ctypes.get_last_error()}",
                )
                go.write_text("go", encoding="ascii")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not ready.is_file() and child.poll() is None:
                    time.sleep(0.1)
                child_stdout = ""
                child_stderr = ""
                if child.poll() is not None:
                    child_stdout, child_stderr = child.communicate(timeout=5)
                self.assertTrue(
                    ready.is_file(),
                    f"restricted child did not hand off; rc={child.poll()} "
                    f"stdout={child_stdout!r} stderr={child_stderr!r}",
                )
                handoff = json.loads(ready.read_text(encoding="utf-8"))
                self.assertNotIn("error", handoff, handoff.get("error"))
                self.assertEqual(handoff["type"], "TaskProcessReference", handoff)
                self.assertGreater(int(handoff["pid"]), 0)
                helper_pid = int(handoff["pid"])
                helper_create_time = psutil.Process(helper_pid).create_time()
                self.assertFalse(result_path.exists(), "helper replaced runtime before GUI exit")
                helper_log = base / "runtime" / "runtime-update-helper.log"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if helper_log.is_file() and "WAIT_PARENT" in helper_log.read_text(
                        encoding="utf-8-sig", errors="replace"
                    ):
                        break
                    time.sleep(0.1)
                self.assertIn(
                    "WAIT_PARENT",
                    helper_log.read_text(encoding="utf-8-sig", errors="replace"),
                )
                allow_exit.write_text("exit", encoding="ascii")
                child_stdout, child_stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, child_stderr or child_stdout)
                self.assertTrue(kernel32.CloseHandle(job))
                job = None

                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not result_path.is_file():
                    time.sleep(0.1)
                self.assertTrue(result_path.is_file(), "helper died when restrictive Job closed")
                payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
                self.assertTrue(payload["success"], payload)
                self.assertFalse(staging.exists())

                def same_helper_is_running():
                    try:
                        process = psutil.Process(helper_pid)
                        return abs(process.create_time() - helper_create_time) < 0.01
                    except psutil.Error:
                        return False

                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and same_helper_is_running():
                    time.sleep(0.1)
                self.assertFalse(
                    same_helper_is_running(),
                    f"runtime helper PID {helper_pid} remained after completion",
                )
                log_text = helper_log.read_text(encoding="utf-8-sig", errors="replace")
                self.assertIn(
                    "RESTART_SKIPPED no-restart requested",
                    log_text,
                )
            finally:
                allow_exit.write_text("exit", encoding="ascii")
                if job:
                    kernel32.CloseHandle(job)
                if child is not None and child.poll() is None:
                    child.wait(timeout=10)

    def test_task_scheduler_runs_hidden_action_as_current_user_and_cleans_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "带空格 中文路径"
            base.mkdir(parents=True)
            marker = base / "task-marker.txt"
            powershell = Path(shutil.which("powershell.exe"))
            target_script = (
                f"Set-Content -LiteralPath '{str(marker).replace(chr(39), chr(39) * 2)}' "
                "-Value 'OK' -Encoding UTF8; Start-Sleep -Seconds 1"
            )
            target_encoded = base64.b64encode(
                target_script.encode("utf-16le")
            ).decode("ascii")
            target_arguments = subprocess.list2cmdline(
                [
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    target_encoded,
                ]
            )
            task_name = f"LingJingAI-RuntimeTest-{os.getpid()}-{time.time_ns()}"
            broker_script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskName = [Environment]::GetEnvironmentVariable('LINGJING_TASK_NAME', 'Process')
$executable = [Environment]::GetEnvironmentVariable('LINGJING_TASK_EXECUTABLE', 'Process')
$arguments = [Environment]::GetEnvironmentVariable('LINGJING_TASK_ARGUMENTS', 'Process')
$workingDirectory = [Environment]::GetEnvironmentVariable('LINGJING_TASK_CWD', 'Process')
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$root = $service.GetFolder('\')
$registered = $null
try {
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'LingJingAI runtime handoff test'
    $definition.Settings.Enabled = $true
    $definition.Settings.Hidden = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.ExecutionTimeLimit = 'PT5M'
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition.Principal.UserId = $user
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $action = $definition.Actions.Create(0)
    $action.Path = $executable
    $action.Arguments = $arguments
    $action.WorkingDirectory = $workingDirectory
    $registered = $root.RegisterTaskDefinition($taskName, $definition, 6, $user, $null, 3, $null)
    $running = $registered.Run($null)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([int]$running.EnginePID -le 0 -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
        $running.Refresh()
    }
    [ordered]@{
        EnginePID = [int]$running.EnginePID
        InstanceGuid = [string]$running.InstanceGuid
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $registered) {
        try { $root.DeleteTask($taskName, 0) } catch {}
    }
}
""".strip()
            broker_encoded = base64.b64encode(
                broker_script.encode("utf-16le")
            ).decode("ascii")
            environment = dict(os.environ)
            environment["LINGJING_TASK_NAME"] = task_name
            environment["LINGJING_TASK_EXECUTABLE"] = str(powershell)
            environment["LINGJING_TASK_ARGUMENTS"] = target_arguments
            environment["LINGJING_TASK_CWD"] = str(powershell.parent)
            completed = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    broker_encoded,
                ],
                cwd=str(base),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not marker.is_file():
                time.sleep(0.1)
            self.assertTrue(
                marker.is_file(),
                f"Task Scheduler target did not run; broker={completed.stdout!r} {completed.stderr!r}",
            )
            registration = _query_task_registration(task_name)
            self.assertFalse(registration["Exists"], registration)
            self.assertEqual(registration["HResult"], -2147024894)

if __name__ == "__main__":
    unittest.main()
