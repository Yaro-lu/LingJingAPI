"""Launch a detached Windows helper for runtime replacement after GUI exit."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from app.core.runtime_package import missing_runtime_paths


RUNTIME_UPDATE_HELPER = Path("app/core/runtime_update_helper.ps1")
RUNTIME_UPDATE_RESULT = Path("runtime/runtime-update-result.json")
_STAGING_NAME = re.compile(r"^\.runtime-install-staging-([0-9a-f]{32})$")
_MAX_RESULT_BYTES = 64 * 1024
_TASK_NAME_ENV = "LINGJING_TASK_NAME"
_TASK_EXECUTABLE_ENV = "LINGJING_TASK_EXECUTABLE"
_TASK_ARGUMENTS_ENV = "LINGJING_TASK_ARGUMENTS"
_TASK_CWD_ENV = "LINGJING_TASK_CWD"
_TASK_ACK_PATH_ENV = "LINGJING_TASK_ACK_PATH"
_TASK_OPERATION_ENV = "LINGJING_TASK_OPERATION_ID"
_MAX_TASK_ARGUMENT_CHARS = 8192
_HELPER_START_TIMEOUT_SECONDS = 15
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_TASK_BROKER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskName = [Environment]::GetEnvironmentVariable('LINGJING_TASK_NAME', 'Process')
$executable = [Environment]::GetEnvironmentVariable('LINGJING_TASK_EXECUTABLE', 'Process')
$arguments = [Environment]::GetEnvironmentVariable('LINGJING_TASK_ARGUMENTS', 'Process')
$workingDirectory = [Environment]::GetEnvironmentVariable('LINGJING_TASK_CWD', 'Process')
$ackPath = [Environment]::GetEnvironmentVariable('LINGJING_TASK_ACK_PATH', 'Process')
$operationId = [Environment]::GetEnvironmentVariable('LINGJING_TASK_OPERATION_ID', 'Process')
foreach ($required in @(
    $taskName, $executable, $arguments, $workingDirectory, $ackPath, $operationId
)) {
    if ([string]::IsNullOrWhiteSpace($required)) {
        throw 'Missing Task Scheduler handoff value'
    }
}
if ($operationId -notmatch '^[0-9a-f]{32}$') {
    throw 'Invalid Task Scheduler operation id'
}

function Test-ProcessRunningStrict {
    param([Parameter(Mandatory)][int]$ProcessId)
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        try {
            return -not $process.HasExited
        }
        finally {
            $process.Dispose()
        }
    }
    catch [System.ArgumentException] {
        return $false
    }
}

function Stop-RunningTaskChecked {
    param(
        [Parameter(Mandatory)]$RunningTask,
        [Parameter(Mandatory)][int]$EnginePid
    )
    try {
        $RunningTask.Stop()
    }
    catch {
        if (-not (Test-ProcessRunningStrict -ProcessId $EnginePid)) {
            return
        }
        throw
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-ProcessRunningStrict -ProcessId $EnginePid)) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Task Scheduler helper process $EnginePid did not stop"
}

$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$root = $service.GetFolder('\')
$registered = $null
$running = $null
$enginePid = 0
$acknowledged = $false
try {
    $definition = $service.NewTask(0)
    $definition.RegistrationInfo.Description = 'LingJingAPI runtime update handoff'
    $definition.Settings.Enabled = $true
    $definition.Settings.Hidden = $true
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = 'PT30M'
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    $isElevated = $principal.IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator
    )
    $definition.Principal.UserId = $identity.Name
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = $(if ($isElevated) { 1 } else { 0 })
    $action = $definition.Actions.Create(0)
    $action.Path = $executable
    $action.Arguments = $arguments
    $action.WorkingDirectory = $workingDirectory
    $registered = $root.RegisterTaskDefinition(
        $taskName, $definition, 6, $identity.Name, $null, 3, $null
    )
    $running = $registered.Run($null)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([int]$running.EnginePID -le 0 -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
        $running.Refresh()
    }
    if ([int]$running.EnginePID -le 0) {
        throw 'Task Scheduler did not start the runtime helper'
    }
    $enginePid = [int]$running.EnginePID
    $ackDeadline = [DateTime]::UtcNow.AddSeconds(15)
    $ackError = 'runtime helper acknowledgement was not created'
    while ([DateTime]::UtcNow -lt $ackDeadline) {
        if (Test-Path -LiteralPath $ackPath -PathType Leaf) {
            try {
                if ((Get-Item -LiteralPath $ackPath).Length -gt 16384) {
                    throw 'runtime helper acknowledgement is too large'
                }
                $ack = Get-Content -LiteralPath $ackPath -Raw -Encoding UTF8 |
                    ConvertFrom-Json
                if ([int]$ack.schema_version -ne 1 -or
                    [string]$ack.operation_id -ne $operationId -or
                    [int]$ack.process_id -ne $enginePid) {
                    throw 'runtime helper acknowledgement identity mismatch'
                }
                Remove-Item -LiteralPath $ackPath -Force
                $acknowledged = $true
                break
            }
            catch {
                $ackError = $_.Exception.Message
            }
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $acknowledged) {
        throw "Task Scheduler helper acknowledgement failed: $ackError"
    }
    if (-not (Test-ProcessRunningStrict -ProcessId $enginePid)) {
        $acknowledged = $false
        throw 'Task Scheduler helper exited immediately after acknowledgement'
    }
    [ordered]@{
        EnginePID = $enginePid
        InstanceGuid = [string]$running.InstanceGuid
        OperationId = $operationId
        Acknowledged = $true
    } | ConvertTo-Json -Compress
}
finally {
    if ($null -ne $registered) {
        if (-not $acknowledged -and $null -ne $running -and $enginePid -gt 0) {
            try {
                Stop-RunningTaskChecked -RunningTask $running -EnginePid $enginePid
            }
            catch {
                try { $registered.Enabled = $false } catch {}
                throw "Task Scheduler helper stop failed: $($_.Exception.Message)"
            }
        }
        # 0x80070002 is the only accepted proof that the task is absent.
        # Do not treat Task Scheduler account/configuration failures as deletion.
        $missingHResults = @(-2147024894)
        $cleanupSucceeded = $false
        $cleanupError = 'temporary task still exists'
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                $root.DeleteTask($taskName, 0)
            }
            catch {
                $cleanupError = $_.Exception.Message
            }
            try {
                $null = $root.GetTask($taskName)
                $cleanupError = 'temporary task still exists after deletion'
            }
            catch {
                if ($missingHResults -contains [int]$_.Exception.HResult) {
                    $cleanupSucceeded = $true
                    break
                }
                $cleanupError = $_.Exception.Message
            }
            Start-Sleep -Milliseconds 200
        }
        if (-not $cleanupSucceeded) {
            $stopError = ''
            if ($null -ne $running -and $enginePid -gt 0) {
                try {
                    Stop-RunningTaskChecked -RunningTask $running -EnginePid $enginePid
                }
                catch {
                    $stopError = "; helper stop failed: $($_.Exception.Message)"
                }
            }
            try { $registered.Enabled = $false } catch {}
            throw "Task Scheduler cleanup failed: $cleanupError$stopError"
        }
    }
}
""".strip()


@dataclass(frozen=True)
class TaskProcessReference:
    """Reference returned after Task Scheduler starts and acknowledges the helper."""

    pid: int
    task_name: str


class _WindowsNamedMutexLease:
    """Owned Win32 mutex handle released by the creating thread."""

    def __init__(self, kernel32, handle: int):
        self._kernel32 = kernel32
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if not handle:
            return
        release_error: OSError | None = None
        if not self._kernel32.ReleaseMutex(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        if not self._kernel32.CloseHandle(handle) and release_error is None:
            release_error = ctypes.WinError(ctypes.get_last_error())
        if release_error is not None:
            raise RuntimeError("无法释放环境更新互斥锁") from release_error


def _runtime_update_mutex_name(base_dir: Path, scope: str) -> str:
    """Return the PowerShell-compatible mutex name for one client root."""
    if scope not in {"Launch", "Transaction"}:
        raise ValueError(f"unsupported runtime update mutex scope: {scope}")
    normalized = str(Path(base_dir).resolve(strict=True)).rstrip("\\/").upper()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"Local\\LingJingAI.RuntimeUpdate.{scope}.{digest}"


def _acquire_windows_named_mutex(
    name: str,
    *,
    busy_message: str,
) -> _WindowsNamedMutexLease:
    """Acquire a named mutex without waiting, failing closed when it is busy."""
    if os.name != "nt":
        raise RuntimeError("运行环境更新互斥锁仅支持 Windows")

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise RuntimeError("无法创建环境更新互斥锁") from ctypes.WinError(
            ctypes.get_last_error()
        )
    wait_result = int(kernel32.WaitForSingleObject(handle, 0))
    if wait_result in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
        return _WindowsNamedMutexLease(kernel32, handle)
    kernel32.CloseHandle(handle)
    if wait_result == _WAIT_TIMEOUT:
        raise RuntimeError(busy_message)
    raise RuntimeError(
        f"无法确认环境更新互斥锁状态（0x{wait_result:08X}）"
    )


@contextmanager
def _runtime_update_launch_guard(base_dir: Path):
    """Serialize handoff and reject launches while a helper owns the runtime."""
    launch = _acquire_windows_named_mutex(
        _runtime_update_mutex_name(base_dir, "Launch"),
        busy_message="另一个环境更新正在启动，请勿重复操作",
    )
    try:
        transaction = _acquire_windows_named_mutex(
            _runtime_update_mutex_name(base_dir, "Transaction"),
            busy_message="环境更新已在后台进行，请等待完成后手动重启客户端",
        )
        transaction.release()
        yield
    finally:
        launch.release()


def _resolved_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label}不存在: {path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{label}不是目录: {resolved}")
    return resolved


def validate_runtime_update_handoff(
    base_dir: Path,
    staging_dir: Path,
) -> tuple[Path, Path, Path, str]:
    """Validate the only paths that the detached updater may mutate."""
    base = _resolved_directory(base_dir, label="客户端目录")
    staging = _resolved_directory(staging_dir, label="环境暂存目录")
    if base == Path(base.anchor):
        raise ValueError("拒绝在磁盘根目录执行运行环境更新")
    if staging.parent != base:
        raise ValueError("环境暂存目录必须是客户端目录的直接子目录")
    match = _STAGING_NAME.fullmatch(staging.name)
    if not match:
        raise ValueError("环境暂存目录名称无效")

    helper = (base / RUNTIME_UPDATE_HELPER).resolve(strict=False)
    expected_helper = base / "app" / "core" / "runtime_update_helper.ps1"
    if helper != expected_helper or not helper.is_file():
        raise ValueError(f"运行环境更新助手缺失: {expected_helper}")

    missing = missing_runtime_paths(staging)
    if missing:
        raise ValueError(f"环境暂存目录不完整，缺少: {', '.join(missing)}")
    return base, staging, helper, match.group(1)


def find_windows_powershell(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Find the system Windows PowerShell executable used outside runtime/python."""
    environ = os.environ if environ is None else environ
    candidates: list[Path] = []
    windows_root = str(environ.get("SystemRoot") or environ.get("WINDIR") or "").strip()
    if windows_root:
        candidates.append(
            Path(windows_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
    resolved = which("powershell.exe")
    if resolved:
        candidates.append(Path(resolved))
    for candidate in candidates:
        try:
            full = candidate.resolve(strict=True)
        except OSError:
            continue
        if full.is_file() and full.name.casefold() == "powershell.exe":
            return full
    raise RuntimeError("未找到系统 Windows PowerShell，无法安全更新运行环境")


def build_runtime_update_command(
    base_dir: Path,
    staging_dir: Path,
    *,
    parent_pid: int,
    powershell_path: Path | None = None,
    wait_timeout_seconds: int = 180,
    restart_client: bool = False,
) -> list[str]:
    """Build a shell-free argument vector for the detached update helper."""
    base, staging, helper, operation_id = validate_runtime_update_handoff(
        base_dir,
        staging_dir,
    )
    if int(parent_pid) <= 0:
        raise ValueError("父进程 PID 无效")
    if not 30 <= int(wait_timeout_seconds) <= 600:
        raise ValueError("等待 GUI 退出的超时时间无效")
    powershell = Path(powershell_path or find_windows_powershell()).resolve(strict=True)
    if not powershell.is_file() or powershell.name.casefold() != "powershell.exe":
        raise ValueError("运行环境更新只能使用 Windows PowerShell")
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-ParentPid",
        str(int(parent_pid)),
        "-BaseDir",
        str(base),
        "-StagingDir",
        str(staging),
        "-OperationId",
        operation_id,
        "-WaitTimeoutSeconds",
        str(int(wait_timeout_seconds)),
    ]
    if not restart_client:
        command.append("-NoRestart")
    return command


def _powershell_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _validated_runtime_update_values(
    command: list[str],
    *,
    base_dir: Path,
    powershell_path: Path,
) -> dict[str, object]:
    """Validate the exact fixed argv before encoding it for Task Scheduler."""
    target = [str(item) for item in command]
    expected_flags = {
        1: "-NoLogo",
        2: "-NoProfile",
        3: "-NonInteractive",
        4: "-ExecutionPolicy",
        5: "Bypass",
        6: "-File",
        8: "-ParentPid",
        10: "-BaseDir",
        12: "-StagingDir",
        14: "-OperationId",
        16: "-WaitTimeoutSeconds",
    }
    if (
        len(target) not in (18, 19)
        or (len(target) == 19 and target[18] != "-NoRestart")
        or any(target[index] != value for index, value in expected_flags.items())
    ):
        raise ValueError("运行环境更新命令结构无效")

    base = Path(base_dir).resolve(strict=True)
    powershell = Path(powershell_path).resolve(strict=True)
    try:
        command_powershell = Path(target[0]).resolve(strict=True)
        helper = Path(target[7]).resolve(strict=True)
        parent_pid = int(target[9])
        command_base = Path(target[11]).resolve(strict=True)
        staging = Path(target[13]).resolve(strict=True)
        timeout_seconds = int(target[17])
    except (OSError, ValueError) as exc:
        raise ValueError("运行环境更新命令参数无效") from exc

    operation_id = target[15]
    staging_match = _STAGING_NAME.fullmatch(staging.name)
    expected_helper = (base / RUNTIME_UPDATE_HELPER).resolve(strict=True)
    if command_powershell != powershell or powershell.name.casefold() != "powershell.exe":
        raise ValueError("运行环境更新命令未使用系统 Windows PowerShell")
    if helper != expected_helper or command_base != base or staging.parent != base:
        raise ValueError("运行环境更新命令路径超出客户端目录")
    if staging_match is None or staging_match.group(1) != operation_id:
        raise ValueError("运行环境更新命令的操作编号无效")
    if not 1 <= parent_pid <= 2147483647 or not 30 <= timeout_seconds <= 600:
        raise ValueError("运行环境更新命令的进程或超时参数无效")
    return {
        "base": base,
        "powershell": powershell,
        "helper": helper,
        "parent_pid": parent_pid,
        "staging": staging,
        "operation_id": operation_id,
        "timeout_seconds": timeout_seconds,
        "restart_client": len(target) == 18,
    }


def _task_action_arguments(command: list[str], *, base_dir: Path, powershell_path: Path) -> tuple[str, str]:
    values = _validated_runtime_update_values(
        command,
        base_dir=base_dir,
        powershell_path=powershell_path,
    )
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {_powershell_literal(values['helper'])} "
        f"-ParentPid {values['parent_pid']} "
        f"-BaseDir {_powershell_literal(values['base'])} "
        f"-StagingDir {_powershell_literal(values['staging'])} "
        f"-OperationId {values['operation_id']} "
        f"-WaitTimeoutSeconds {values['timeout_seconds']} "
        + ("\n" if values["restart_client"] else "-NoRestart\n")
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    arguments = subprocess.list2cmdline(
        [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
    )
    if not arguments.isascii() or len(arguments) > _MAX_TASK_ARGUMENT_CHARS:
        raise RuntimeError("后台运行环境更新参数过长或编码无效")
    return arguments, str(values["operation_id"])


def _helper_started_paths(base_dir: Path, operation_id: str) -> tuple[Path, Path]:
    runtime_dir = Path(base_dir) / "runtime"
    stem = f"runtime-update-started-{operation_id}"
    return runtime_dir / f"{stem}.json", runtime_dir / f"{stem}.tmp"


def _helper_commit_paths(base_dir: Path, operation_id: str) -> tuple[Path, Path]:
    runtime_dir = Path(base_dir) / "runtime"
    stem = f"runtime-update-commit-{operation_id}"
    return runtime_dir / f"{stem}.json", runtime_dir / f"{stem}.tmp"


def _clear_helper_started_markers(base_dir: Path, operation_id: str) -> None:
    for marker in _helper_started_paths(base_dir, operation_id):
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"无法清理旧的运行环境启动标记: {marker}") from exc
        if marker.exists():
            raise RuntimeError(f"旧的运行环境启动标记仍然存在: {marker}")


def _clear_helper_commit_markers(base_dir: Path, operation_id: str) -> None:
    for marker in _helper_commit_paths(base_dir, operation_id):
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"无法清理旧的运行环境提交标记: {marker}") from exc
        if marker.exists():
            raise RuntimeError(f"旧的运行环境提交标记仍然存在: {marker}")


def _write_helper_commit(base_dir: Path, operation_id: str, process_id: int) -> Path:
    marker, temporary = _helper_commit_paths(base_dir, operation_id)
    if int(process_id) <= 0:
        raise RuntimeError("后台环境更新助手进程号无效，未授权切换环境")
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "process_id": int(process_id),
        "committed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("无法授权后台环境更新助手切换环境") from exc
    return marker


def _wait_for_helper_started(
    base_dir: Path,
    operation_id: str,
    expected_pid: int,
    *,
    timeout_seconds: float = _HELPER_START_TIMEOUT_SECONDS,
) -> dict[str, object]:
    marker, _temporary = _helper_started_paths(base_dir, operation_id)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        try:
            if marker.is_file() and marker.stat().st_size <= 16 * 1024:
                payload = json.loads(marker.read_text(encoding="utf-8-sig"))
                if (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == 1
                    and payload.get("operation_id") == operation_id
                    and int(payload.get("process_id", 0)) == int(expected_pid)
                ):
                    marker.unlink(missing_ok=True)
                    return payload
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise RuntimeError("后台环境更新助手未启动，请保持客户端打开后重试")


def _stop_unacknowledged_helper(process, *, timeout_seconds: float = 3.0) -> None:
    """Stop a direct helper before retrying through Task Scheduler.

    The helper writes its acknowledgement before waiting for the GUI or moving
    any runtime files.  Therefore an unacknowledged process is safe to stop, but
    a second helper must never be started until the first process has exited.
    """
    try:
        if process.poll() is not None:
            return
    except (AttributeError, OSError) as exc:
        raise RuntimeError("无法确认未响应的环境更新助手状态") from exc

    try:
        process.terminate()
    except OSError as exc:
        try:
            if process.poll() is not None:
                return
        except OSError:
            pass
        raise RuntimeError("无法停止未响应的环境更新助手") from exc

    try:
        process.wait(timeout=max(0.1, float(timeout_seconds)))
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        process.kill()
        process.wait(timeout=max(0.1, float(timeout_seconds)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "未响应的环境更新助手仍在运行，已取消备用启动以避免重复安装"
        ) from exc


def _launch_via_task_scheduler(
    command: list[str],
    *,
    base_dir: Path,
    powershell_path: Path,
    runner=None,
) -> TaskProcessReference:
    """Run the helper with the current interactive token outside the GUI Job."""
    base = Path(base_dir).resolve(strict=True)
    powershell = Path(powershell_path).resolve(strict=True)
    action_arguments, operation_id = _task_action_arguments(
        command,
        base_dir=base,
        powershell_path=powershell,
    )
    task_name = f"LingJingAI-RuntimeUpdate-{operation_id}"
    _clear_helper_started_markers(base, operation_id)
    _clear_helper_commit_markers(base, operation_id)
    started_marker, _started_temporary = _helper_started_paths(base, operation_id)
    encoded_script = base64.b64encode(
        _TASK_BROKER_SCRIPT.encode("utf-16le")
    ).decode("ascii")
    broker_command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_script,
    ]
    broker_env = os.environ.copy()
    broker_env[_TASK_NAME_ENV] = task_name
    broker_env[_TASK_EXECUTABLE_ENV] = str(powershell)
    broker_env[_TASK_ARGUMENTS_ENV] = action_arguments
    broker_env[_TASK_CWD_ENV] = str(powershell.parent)
    broker_env[_TASK_ACK_PATH_ENV] = str(started_marker.resolve(strict=False))
    broker_env[_TASK_OPERATION_ENV] = operation_id
    runner = runner or subprocess.run
    completed = runner(
        broker_command,
        cwd=str(base),
        env=broker_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    )
    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"无法启动后台环境更新助手：{detail or '任务计划服务启动失败'}")

    payload = None
    for line in reversed(str(completed.stdout or "").splitlines()):
        try:
            candidate = json.loads(line.strip().lstrip("\ufeff"))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise RuntimeError("后台环境更新助手未返回进程信息")
    process_id = int(payload.get("EnginePID", 0))
    if process_id <= 0:
        raise RuntimeError("任务计划服务未返回后台环境更新进程")
    if (
        payload.get("Acknowledged") is not True
        or payload.get("OperationId") != operation_id
    ):
        raise RuntimeError("任务计划服务未返回 PID 绑定的启动确认信息")
    _write_helper_commit(base, operation_id, process_id)
    return TaskProcessReference(pid=process_id, task_name=task_name)


def _launch_runtime_update_unlocked(
    base_dir: Path,
    staging_dir: Path,
    *,
    parent_pid: int,
    powershell_path: Path | None = None,
    popen_factory=None,
    task_launcher=None,
    started_waiter=None,
    restart_client: bool = False,
):
    """Start the updater after the caller has serialized the handoff."""
    if os.name != "nt":
        raise RuntimeError("运行环境自动更新仅支持 Windows")
    base, _, _, operation_id = validate_runtime_update_handoff(base_dir, staging_dir)
    command = build_runtime_update_command(
        base,
        staging_dir,
        parent_pid=parent_pid,
        powershell_path=powershell_path,
        restart_client=restart_client,
    )
    popen_factory = popen_factory or subprocess.Popen
    base_creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )
    breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    popen_kwargs = dict(
        cwd=str(base),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        startupinfo=startupinfo,
    )
    _clear_helper_started_markers(base, operation_id)
    _clear_helper_commit_markers(base, operation_id)
    try:
        process = popen_factory(
            command,
            creationflags=base_creation_flags | breakaway_flag,
            **popen_kwargs,
        )
        process_id = int(getattr(process, "pid", 0))
        if process_id <= 0:
            raise RuntimeError("后台环境更新助手未返回有效进程号")
        waiter = started_waiter or _wait_for_helper_started
        try:
            waiter(base, operation_id, process_id)
        except RuntimeError as direct_error:
            _stop_unacknowledged_helper(process)
            launcher = task_launcher or _launch_via_task_scheduler
            try:
                return launcher(
                    command,
                    base_dir=base,
                    powershell_path=Path(command[0]).resolve(strict=True),
                )
            except Exception as scheduled_error:
                raise RuntimeError(
                    "直接启动环境更新助手未确认，且 Windows 任务计划备用启动失败："
                    f"{scheduled_error}"
                ) from direct_error
        try:
            _write_helper_commit(base, operation_id, process_id)
        except RuntimeError:
            _stop_unacknowledged_helper(process)
            raise
        return process
    except OSError as exc:
        # A parent Job may forbid CREATE_BREAKAWAY_FROM_JOB (ERROR_ACCESS_DENIED).
        # Retrying CreateProcess without breakaway makes the helper die with the GUI.
        # Task Scheduler starts with the current interactive token outside the GUI Job.
        if getattr(exc, "winerror", None) != 5:
            raise
        launcher = task_launcher or _launch_via_task_scheduler
        return launcher(
            command,
            base_dir=base,
            powershell_path=Path(command[0]).resolve(strict=True),
        )


def launch_runtime_update(
    base_dir: Path,
    staging_dir: Path,
    *,
    parent_pid: int,
    powershell_path: Path | None = None,
    popen_factory=None,
    task_launcher=None,
    started_waiter=None,
    restart_client: bool = False,
):
    """Start one serialized updater outside the GUI Job/process tree."""
    if os.name != "nt":
        raise RuntimeError("运行环境自动更新仅支持 Windows")
    base, _, _, _ = validate_runtime_update_handoff(base_dir, staging_dir)
    with _runtime_update_launch_guard(base):
        return _launch_runtime_update_unlocked(
            base,
            staging_dir,
            parent_pid=parent_pid,
            powershell_path=powershell_path,
            popen_factory=popen_factory,
            task_launcher=task_launcher,
            started_waiter=started_waiter,
            restart_client=restart_client,
        )


def consume_runtime_update_result(base_dir: Path) -> dict[str, object] | None:
    """Read and remove the fixed updater result file shown after restart."""
    base = Path(base_dir).resolve(strict=False)
    result_path = base / RUNTIME_UPDATE_RESULT
    try:
        if not result_path.is_file() or result_path.stat().st_size > _MAX_RESULT_BYTES:
            return None
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
