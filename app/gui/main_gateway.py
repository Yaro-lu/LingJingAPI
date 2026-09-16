"""
灵境造片厂 — 主界面
- 启动 ComfyUI + API 服务器 + Cloudflare Tunnel
- 环境检测 / 运行时检查 / 模型检查
- 进度监控面板
- 系统托盘（最小化到托盘）
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import sys
import threading
import time
import json
import os
import queue
import re
import shutil
import tempfile
import unicodedata
import webbrowser
import msvcrt
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import urllib.request as urllib_request

try:
    import customtkinter as ctk
except Exception:
    ctk = None

BASE_DIR = Path(__file__).parent.parent.parent
GUI_ASSET_DIR = Path(__file__).parent / "assets"
MIN_NVIDIA_DRIVER_MAJOR = 580
MIN_SUPPORTED_VRAM_MB = 8 * 1024
MAX_BACKEND_LOG_BYTES = 8 * 1024 * 1024
MODEL_DOWNLOAD_REDIRECT_SUFFIXES = (
    "huggingface.co",
    "hf.co",
    "xethub.hf.co",
    "hf-mirror.com",
)
RUNTIME_DOWNLOAD_REDIRECT_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    APP_VERSION = "dev"
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", APP_VERSION):
    APP_VERSION = "dev"

from app.core.runtime_package import (  # noqa: E402
    REQUIRED_RUNTIME_PATHS,
    RUNTIME_PACKAGE_NAME,
    RUNTIME_PACKAGE_SIZE,
    RUNTIME_PACKAGE_SHA256,
    SevenZipProgressParser,
    archive_extract_command,
    archive_list_command,
    find_extractor,
    invalid_archive_entries,
    missing_archive_entries,
    missing_runtime_paths,
    parse_archive_members,
    validate_staged_runtime,
    verify_runtime_package,
    resolve_runtime_download_url,
)
from app.core.runtime_download import download_runtime_package
from app.core.runtime_update import (  # noqa: E402
    consume_runtime_update_result,
    launch_runtime_update,
)
from app.core.comfyui_update import (  # noqa: E402
    ComfyUIUpdateError,
    prepare_comfyui_update,
)
from app.core.comfyui_git_update import update_git_comfyui
from app.core.comfyui_conversion import install_frontend_bridge, convert_with_comfyui
from app.core.comfyui_update_worker import (  # noqa: E402
    consume_update_result as consume_comfyui_update_result,
    launch_worker as launch_comfyui_update_worker,
    recover_interrupted_update,
)
from app.core.process_supervisor import ProcessSupervisor  # noqa: E402
from app.engines.comfyui_client import ComfyUIClient  # noqa: E402
import urllib.request as ur
from app.core.workflow_adaptation import (  # noqa: E402
    analyze_workflow, candidates, convert_editor_workflow, graph_hash,
    infer_output_type, make_mapping, mapping_fields, validate_graph,
)
from app.core.model_maintenance import (  # noqa: E402
    MODEL_REQUIREMENTS,
    check_model_groups,
    cleanup_incomplete_imports,
    import_model_directory,
    model_file_ready,
    model_file_sha256_matches,
    unsafe_model_files,
)
from app.core.runtime_state import RuntimeState  # noqa: E402
from app.core.secret_store import protect_text, unprotect_text  # noqa: E402
from app.config import Config  # noqa: E402
from app.core.workflow_dependencies import (  # noqa: E402
    clear_model_index_cache,
    normalize_workflow_dependencies,
    workflow_dependency_report,
)
from app.core.workflow_import import (  # noqa: E402
    cleanup_stale_workflow_imports,
    copy_workflow_assets,
    create_import_workspace,
    ensure_safe_workflows_root,
    effective_workflow_root,
    extract_zip_safely,
)
from app.gui.dashboard_pages import StaticDashboardPages  # noqa: E402
from app.workflow_registry import (  # noqa: E402
    WorkflowRegistry,
    merge_workflow_catalog,
    read_local_workflow_catalog,
)

_INSTANCE_LOCK_HANDLE = None
CTK_AVAILABLE = ctk is not None
PROJECT_REPOSITORY = "Yaro-lu/LingJingAPI"
PROJECT_HOMEPAGE_URL = f"https://github.com/{PROJECT_REPOSITORY}"
PROJECT_RELEASES_URL = f"{PROJECT_HOMEPAGE_URL}/releases/latest"
PROJECT_LATEST_RELEASE_API_URL = (
    f"https://api.github.com/repos/{PROJECT_REPOSITORY}/releases/latest"
)
MAX_RELEASE_METADATA_BYTES = 64 * 1024


def _release_version_tuple(value: str):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_newer_release(current_version: str, latest_version: str) -> bool:
    current = _release_version_tuple(current_version)
    latest = _release_version_tuple(latest_version)
    return bool(current and latest and latest > current)


def _fetch_latest_release_version(*, urlopen=None, timeout: float = 6.0) -> str:
    """Return the latest stable GitHub release version without trusting its URL."""
    opener = urlopen or urllib_request.urlopen
    request = urllib_request.Request(
        PROJECT_LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"LingJing-Desktop/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response = opener(request, timeout=min(8.0, max(1.0, float(timeout))))
    try:
        payload = response.read(MAX_RELEASE_METADATA_BYTES + 1)
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            closer()
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_RELEASE_METADATA_BYTES:
        raise ValueError("GitHub Release 响应无效或过大")
    data = json.loads(bytes(payload).decode("utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("draft") is True
        or data.get("prerelease") is True
    ):
        return ""
    version = _release_version_tuple(data.get("tag_name"))
    return ".".join(str(part) for part in version) if version else ""


def _validate_download_target_url(
    requested_url: str,
    target_url: str,
    *,
    allowed_suffixes: tuple[str, ...],
) -> str:
    """Validate one download target against the original HTTPS host family."""
    requested = urlsplit(str(requested_url or ""))
    if (
        requested.scheme.lower() != "https"
        or not requested.hostname
        or requested.username
        or requested.password
    ):
        raise IOError("下载地址必须使用 HTTPS")
    final_url = str(target_url or "").strip()
    final = urlsplit(final_url)
    final_host = str(final.hostname or "").casefold().rstrip(".")
    initial_host = str(requested.hostname or "").casefold().rstrip(".")
    allowed = final_host == initial_host or any(
        final_host == suffix or final_host.endswith(f".{suffix}")
        for suffix in allowed_suffixes
    )
    if (
        final.scheme.lower() != "https"
        or not final_host
        or final.username
        or final.password
        or not allowed
    ):
        raise IOError("下载发生了未授权的重定向")
    return final_url


class _DownloadRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Validate every redirect before urllib connects to the next target."""

    def __init__(self, requested_url: str, allowed_suffixes: tuple[str, ...]):
        super().__init__()
        self._requested_url = str(requested_url or "")
        self._allowed_suffixes = tuple(allowed_suffixes)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urljoin(req.full_url, newurl)
        _validate_download_target_url(
            self._requested_url,
            resolved,
            allowed_suffixes=self._allowed_suffixes,
        )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _open_download_request(request, *, timeout: float, allowed_suffixes: tuple[str, ...]):
    """Open a download with redirect validation enforced before each connection."""
    requested_url = str(getattr(request, "full_url", "") or "")
    _validate_download_target_url(
        requested_url,
        requested_url,
        allowed_suffixes=allowed_suffixes,
    )
    opener = urllib_request.build_opener(
        _DownloadRedirectHandler(requested_url, allowed_suffixes)
    )
    return opener.open(request, timeout=timeout)


def _validate_download_response_url(
    requested_url: str,
    response,
    *,
    allowed_suffixes: tuple[str, ...],
) -> str:
    """Recheck the final response URL as defense in depth."""
    final_url = requested_url
    getter = getattr(response, "geturl", None)
    if callable(getter):
        observed = getter()
        if isinstance(observed, str) and observed.strip():
            final_url = observed.strip()
    return _validate_download_target_url(
        requested_url,
        final_url,
        allowed_suffixes=allowed_suffixes,
    )


class _ComfyUIOverlayProcessStillRunning(RuntimeError):
    """The overlay builder may still own files, so cleanup must not race it."""

# ── 配色 ──────────────────────────────────────────────
C = {
    "bg":       "#f1f5fc",
    "surface":  "#ffffff",
    "card":     "#ffffff",
    "primary":  "#5545fc",
    "primary2": "#6d5cff",
    "accent":   "#3844e1",
    "success":  "#20b15e",
    "warn":     "#ff9900",
    "error":    "#ef4444",
    "text":     "#172033",
    "text2":    "#52657f",
    "muted":    "#9ca9bf",
    "border":   "#c5d4ee",
    "border2":  "#d7e2f4",
    "entry":    "#fbfdff",
    "hover":    "#f4f8ff",
    "progress_bg": "#ecf0f5",
    "shadow":   "#dfe8f5",
    "button":   "#fbfdff",
    "sidebar":  "#ffffff",
    "sidebar_active": "#eeecff",
    "sidebar_border": "#dfe7f3",
    "soft_primary": "#eeecff",
    "soft_success": "#eaf9f1",
    "soft_warn": "#fff5e6",
    "soft_error": "#fff0f0",
}
F = {
    "brand":  ("Microsoft YaHei UI", 22, "bold"),
    "title":  ("Microsoft YaHei UI", 14, "bold"),
    "h2":     ("Microsoft YaHei UI", 11, "bold"),
    "bold":   ("Microsoft YaHei UI", 10, "bold"),
    "normal": ("Microsoft YaHei UI", 10),
    "button": ("Microsoft YaHei UI", 10),
    "body":   ("Microsoft YaHei UI", 10),
    "small":  ("Microsoft YaHei UI", 9),
    "tiny":   ("Microsoft YaHei UI", 9),
    "mono":   ("Consolas", 10),
    "url":    ("Consolas", 10),
}

RUNTIME_INSTALL_STAGES = {
    "prepare": (5, "准备安装", "正在读取运行环境包"),
    "verify": (15, "校验安装包", "正在验证文件完整性"),
    "inspect": (30, "检查环境包结构", "正在确认模块和目录是否完整"),
    "extract": (
        45,
        "解压并安装核心模块",
        "Python · ComfyUI · Torch/CUDA · 网络组件",
    ),
    "validate": (75, "验证运行模块", "正在检查已安装组件"),
    "stop_services": (85, "切换运行环境", "正在安全停止后台服务"),
    "apply": (95, "应用运行环境", "客户端将退出，请手动重新打开，重启后生效"),
}

RUNTIME_LONG_STAGE_STATUS = {
    "verify": "正在校验大文件",
    "inspect": "正在读取压缩包目录",
    "validate": "正在检查已解压文件",
}

LAYOUT = {
    "window_w": 1180,
    "window_h": 760,
    "min_w": 1090,
    "min_h": 700,
    "sidebar_w": 204,
    "outer": 18,
    "gap": 10,
    "top_h": 78,
    "status_h": 38,
    "info_h": 184,  # Three 56px URL cards with two 8px gaps.
    "info_row_h": 56,
    "info_gap": 8,
    "left_w": 240,
    "actions_h": 42,
    "footer_h": 32,
}

_LOCAL_CONFIG = Config(BASE_DIR)
API_PORT = _LOCAL_CONFIG.server_port
try:
    COMFY_PORT = int(urlsplit(_LOCAL_CONFIG.comfyui_url).port or 8188)
except (TypeError, ValueError):
    COMFY_PORT = 8188
API_BASE = f"http://127.0.0.1:{API_PORT}"
COMFY_BASE = _LOCAL_CONFIG.comfyui_url.rstrip("/") or f"http://127.0.0.1:{COMFY_PORT}"
SERVER_SYNC_MAX_RETRIES = 3
TORCH_PROBE_TIMEOUT_SECONDS = 90
_LOOPBACK_SERVER_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RUNTIME_UPDATE_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ══════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════

def _active_local_config() -> Config:
    """Use the startup snapshot while allowing tests to replace ``BASE_DIR``."""
    try:
        if _LOCAL_CONFIG.base_dir.resolve(strict=False) == BASE_DIR.resolve(strict=False):
            return _LOCAL_CONFIG
    except (OSError, RuntimeError):
        pass
    return Config(BASE_DIR)


def _models_dir() -> Path:
    return _active_local_config().models_dir


def _workflows_dir() -> Path:
    return _active_local_config().workflows_dir


def _outputs_dir() -> Path:
    return _active_local_config().outputs_dir


def _logs_dir() -> Path:
    return _active_local_config().logs_dir


def _load_local_config():
    try:
        return Config(BASE_DIR).as_dict()
    except Exception:
        return Config(BASE_DIR)._default_config()


def _detect_gpu_memory_mb():
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
        return max(values) if values else 0
    except Exception:
        return 0


def _comfy_vram_args():
    comfy_config = _load_local_config().get("comfyui", {})
    explicit_args = comfy_config.get("launch_args")
    if isinstance(explicit_args, list):
        return [str(item) for item in explicit_args if str(item).strip()]
    mode = str(comfy_config.get("vram_mode", "auto")).strip().lower()
    if mode in ("high", "highvram"):
        return ["--highvram"]
    if mode in ("normal", "normalvram"):
        return []
    if mode in ("low", "lowvram"):
        return ["--lowvram"]
    if mode in ("none", "default", "auto-comfy"):
        return []
    # ComfyUI's DynamicVRAM is the safest common default.  In particular, the
    # bundled workflows still need dynamic offloading on lower-memory cards and
    # must not be forced into high-VRAM mode based on capacity alone. Advanced
    # users can still opt in through vram_mode or launch_args above.
    return []


def _is_supported_rtx_gpu(gpu_name: str) -> bool:
    """Return whether a GPU name is in the supported RTX 20+ family."""
    name = str(gpu_name or "").upper()
    numbered = re.search(r"\bRTX\s*(\d{4})\b", name)
    if numbered and int(numbered.group(1)) >= 2000:
        return True
    return any(
        marker in name
        for marker in ("TITAN RTX", "QUADRO RTX", "RTX A", "RTX PRO")
    )


def _platform_server_url_error(value: str) -> str:
    """Return a user-facing error for unsafe platform endpoints."""
    text = str(value or "").strip()
    if not text:
        return "请填写服务端地址。"
    if any(ord(char) < 32 for char in text):
        return "服务端地址包含无效字符。"
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError:
        return "服务端地址格式无效。"
    if parsed.username or parsed.password:
        return "服务端地址不能包含账号或密码。"
    if parsed.query or parsed.fragment:
        return "服务端地址不能包含查询参数或片段。"
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and hostname:
        return ""
    if parsed.scheme == "http" and hostname in _LOOPBACK_SERVER_HOSTS:
        return ""
    return "正式服务端必须使用 HTTPS；HTTP 仅允许本机开发地址。"


def _open_platform_request(request, timeout: float):
    """Open one platform request without allowing credential-bearing redirects."""
    import urllib.error as ue
    import urllib.request as ur

    original = urlsplit(request.full_url)
    error = _platform_server_url_error(request.full_url)
    if error:
        raise ValueError(error)

    def origin(parts):
        default_port = 443 if parts.scheme == "https" else 80
        return parts.scheme, (parts.hostname or "").lower(), parts.port or default_port

    original_origin = origin(original)

    class SameOriginRedirectHandler(ur.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            resolved = urljoin(req.full_url, newurl)
            target = urlsplit(resolved)
            if _platform_server_url_error(resolved) or origin(target) != original_origin:
                raise ue.URLError("已阻止服务端跨域或降级重定向")
            return super().redirect_request(req, fp, code, msg, headers, resolved)

    return ur.build_opener(SameOriginRedirectHandler()).open(request, timeout=timeout)


def _acquire_instance_lock() -> bool:
    """Use a Windows file lock so only one desktop gateway can run."""
    global _INSTANCE_LOCK_HANDLE
    lock_dir = BASE_DIR / "runtime"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_dir / "gateway.lock", "a+b")
    try:
        # msvcrt locks from the current file position.  Always lock byte 0;
        # locking at append/EOF lets a second instance lock a different byte.
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            lock_handle.close()
        except Exception:
            pass
        return False
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()).encode("ascii", errors="ignore"))
    lock_handle.flush()
    _INSTANCE_LOCK_HANDLE = lock_handle
    return True


def _release_instance_lock():
    global _INSTANCE_LOCK_HANDLE
    if not _INSTANCE_LOCK_HANDLE:
        return
    try:
        _INSTANCE_LOCK_HANDLE.seek(0)
        msvcrt.locking(_INSTANCE_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        _INSTANCE_LOCK_HANDLE.close()
    except Exception:
        pass
    _INSTANCE_LOCK_HANDLE = None


def _check_runtime_exists() -> bool:
    """检查 runtime 目录核心文件是否存在"""
    return not missing_runtime_paths(BASE_DIR)


def _runtime_has_package_files() -> bool:
    """Return whether this installation contains any managed runtime file."""
    return any((BASE_DIR / path).is_file() for path in REQUIRED_RUNTIME_PATHS)


def _model_file_ready(path: Path, expected_size: int | None = None) -> bool:
    """Compatibility wrapper around the shared model validator."""
    return model_file_ready(path, expected_size)


def _check_models_status() -> dict:
    """Check every model group used by the bundled workflows."""
    return check_model_groups(_models_dir(), MODEL_REQUIREMENTS)


def _model_download_active(control: dict) -> bool:
    return str(control.get("state") or "") in {
        "downloading",
        "resuming",
        "cancelling",
        "abandoning",
    }


_ACCOUNT_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def _normalize_account_email(value: str) -> str:
    """Normalize paste-friendly punctuation without changing a valid address."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    if "@" not in normalized:
        return normalized
    local, domain = normalized.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _account_email_error(value: str) -> str:
    email = _normalize_account_email(value)
    if not email:
        return "请填写账号邮箱。"
    if len(email) > 254 or not _ACCOUNT_EMAIL_RE.fullmatch(email):
        return "邮箱格式不正确，请检查 @ 和英文句点。"
    local = email.rsplit("@", 1)[0]
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return "邮箱格式不正确，请检查英文句点的位置。"
    return ""


def _check_torch(python_path: Path, run_command=subprocess.run) -> dict:
    """通过 runtime python 检查 PyTorch / CUDA"""
    check_script = """
import json
try:
    import torch
    r = {"torch_version": torch.__version__, "cuda_available": torch.cuda.is_available()}
    if torch.cuda.is_available():
        r["gpu_name"] = torch.cuda.get_device_name(0)
    print(json.dumps(r))
except ImportError:
    print(json.dumps({"error": "TORCH_IMPORT_FAILED"}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
    try:
        probe_env = os.environ.copy()
        probe_env["PYTHONNOUSERSITE"] = "1"
        probe_env["PYTHONDONTWRITEBYTECODE"] = "1"
        probe_env["PYTHONUTF8"] = "1"
        probe_env.pop("PYTHONHOME", None)
        probe_env.pop("PYTHONSTARTUP", None)
        probe_env.pop("PYTHONPATH", None)
        proc = run_command(
            [str(python_path), "-s", "-B", "-c", check_script],
            capture_output=True,
            text=True,
            timeout=TORCH_PROBE_TIMEOUT_SECONDS,
            cwd=str(BASE_DIR),
            env=probe_env,
        )
        if proc.returncode != 0:
            return {"success": False, "error": "PyTorch 调用失败"}
        data = json.loads(proc.stdout)
        if "error" in data:
            return {"success": False, "error": data["error"]}
        return {
            "success": data.get("cuda_available", False),
            "torch_version": data.get("torch_version", ""),
            "gpu_name": data.get("gpu_name", ""),
            "cuda_available": data.get("cuda_available", False),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PyTorch 检测超时"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _check_system_env(run_command=subprocess.run) -> dict:
    """检查系统环境（nvidia-smi, GPU）"""
    try:
        result = run_command(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        )
        if result.returncode != 0:
            return {"success": False, "error": "未检测到 NVIDIA 显卡驱动"}
        output = result.stdout
        lines = output.strip().split("\n")
        if not lines:
            return {"success": False, "error": "未检测到 NVIDIA 显卡"}
        gpus = []
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                gpus.append(
                    {
                        "gpu_name": parts[0],
                        "driver_version": parts[1],
                        "vram_mb": int(parts[2]),
                    }
                )
            except ValueError:
                continue
        if not gpus:
            return {"success": False, "error": "无法解析 GPU 信息"}
        supported = [gpu for gpu in gpus if _is_supported_rtx_gpu(gpu["gpu_name"])]
        if not supported:
            detected = max(gpus, key=lambda gpu: gpu["vram_mb"])
            return {
                "success": False,
                "error": "本版运行环境要求 NVIDIA RTX 20 系列或更高显卡",
                "gpu_name": detected["gpu_name"],
                "driver_version": detected["driver_version"],
                "vram_gb": detected["vram_mb"] // 1024,
            }
        selected = max(supported, key=lambda gpu: gpu["vram_mb"])
        gpu_name = selected["gpu_name"]
        driver = selected["driver_version"]
        vram_mb = selected["vram_mb"]
        if vram_mb < MIN_SUPPORTED_VRAM_MB:
            return {
                "success": False,
                "error": "本版运行环境要求至少 8GB 显存",
                "gpu_name": gpu_name,
                "driver_version": driver,
                "vram_gb": vram_mb // 1024,
            }
        match = re.match(r"^(\d+)", driver)
        driver_major = int(match.group(1)) if match else 0
        if driver_major < MIN_NVIDIA_DRIVER_MAJOR:
            return {
                "success": False,
                "error": (
                    f"NVIDIA 驱动 {driver} 过旧；CUDA 13 运行环境需要 "
                    f"R{MIN_NVIDIA_DRIVER_MAJOR} 或更高版本"
                ),
                "gpu_name": gpu_name,
                "driver_version": driver,
                "vram_gb": vram_mb // 1024,
            }
        return {
            "success": True,
            "gpu_name": gpu_name,
            "driver_version": driver,
            "vram_gb": vram_mb // 1024,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"success": False, "error": "未检测到 NVIDIA 显卡驱动"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _ensure_extra_model_paths():
    """确保 ComfyUI 的模型路径配置指向当前有效的模型目录。"""
    yaml_path = BASE_DIR / "runtime" / "ComfyUI" / "extra_model_paths.yaml"
    models_path = json.dumps(_models_dir().resolve().as_posix(), ensure_ascii=False)
    content = f"""# 灵境造片厂 — 自动生成的模型路径配置
comfyui:
    base_path: {models_path}
    checkpoints: checkpoints/
    loras: loras/
    vae: vae/
    text_encoders: |
         text_encoders/
         clip/
    diffusion_models: |
         unet/
         diffusion_models/
    clip_vision: clip_vision/
"""
    from app.core.model_mappings import extra_search_paths
    for index, (category, directory) in enumerate(extra_search_paths(_models_dir())):
        content += f"\nlingjing_local_{index}:\n    {category}: {json.dumps(directory, ensure_ascii=False)}\n"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(content)


# ══════════════════════════════════════════════════════
# GatewayApp
# ══════════════════════════════════════════════════════

class RoundedFrame(tk.Frame):
    def __init__(self, parent, radius=10, fill=None, outline=None, **kwargs):
        parent_bg = kwargs.pop("bg", None)
        if parent_bg is None:
            try:
                parent_bg = parent.cget("bg")
            except Exception:
                parent_bg = C["bg"]
        super().__init__(parent, bg=parent_bg, bd=0, highlightthickness=0, **kwargs)
        self._radius = radius
        self._fill = fill or C["card"]
        self._outline = outline or C["border2"]
        self._bg_canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        # Canvas.lower() lowers canvas items, not the widget itself.
        # Use the Tcl widget command so fallback mode also starts cleanly.
        self.tk.call("lower", self._bg_canvas._w)
        self.bind("<Configure>", self._draw_bg)

    def _draw_bg(self, _event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        r = min(self._radius, w // 2, h // 2)
        c = self._bg_canvas
        c.delete("all")
        self._round_rect(c, 1, 1, w - 2, h - 2, r, fill=self._fill, outline=self._outline, width=1)

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)


class SlimRoundedScrollbar(tk.Canvas):
    """A narrow, brand-coloured vertical scrollbar without arrow buttons."""

    def __init__(
        self,
        parent,
        command,
        *,
        width=6,
        bar_width=4,
        color=None,
        active_color=None,
        trough_color=None,
        **kwargs,
    ):
        self.bar_width = max(3, int(bar_width))
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._drag_offset = None
        self._thumb_bounds = (0.0, 0.0)
        self._color = color or C["primary"]
        self._active_color = active_color or C["primary2"]
        self._trough_color = trough_color or C["card"]
        super().__init__(
            parent,
            width=max(self.bar_width, int(width)),
            bg=self._trough_color,
            bd=0,
            relief="flat",
            highlightthickness=0,
            cursor="hand2",
            **kwargs,
        )
        self.bind("<Configure>", self._redraw)
        self.bind("<Button-1>", self._begin_drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Enter>", lambda _event: self._redraw(color=self._active_color))
        self.bind("<Leave>", lambda _event: self._redraw())

    def set(self, first, last):
        try:
            self._first = max(0.0, min(1.0, float(first)))
            self._last = max(self._first, min(1.0, float(last)))
        except (TypeError, ValueError):
            self._first, self._last = 0.0, 1.0
        self._redraw()

    def _redraw(self, _event=None, color=None):
        if not self.winfo_exists():
            return
        height = max(2, self.winfo_height())
        width = max(self.bar_width, self.winfo_width())
        span = max(0.0, self._last - self._first)
        thumb_h = min(height, max(18, int(round(height * span))))
        movable = max(0, height - thumb_h)
        denominator = max(0.000001, 1.0 - span)
        top = int(round(movable * min(1.0, self._first / denominator)))
        bottom = min(height, top + thumb_h)
        left = max(0, (width - self.bar_width) // 2)
        right = left + self.bar_width
        self._thumb_bounds = (float(top), float(bottom))
        self.delete("thumb")
        radius = min(self.bar_width / 2, max(1.0, (bottom - top) / 2))
        RoundedFrame._round_rect(
            self,
            left,
            top,
            right,
            bottom,
            radius,
            fill=color or self._color,
            outline="",
            tags=("thumb",),
        )

    def _begin_drag(self, event):
        top, bottom = self._thumb_bounds
        if top <= event.y <= bottom:
            self._drag_offset = event.y - top
        else:
            self._drag_offset = max(0.0, (bottom - top) / 2)
            self._move_to_pointer(event.y)
        self._redraw(color=self._active_color)

    def _drag(self, event):
        self._move_to_pointer(event.y)

    def _move_to_pointer(self, pointer_y):
        if not callable(self._command):
            return
        height = max(1.0, float(self.winfo_height()))
        top, bottom = self._thumb_bounds
        thumb_h = max(1.0, bottom - top)
        movable = max(1.0, height - thumb_h)
        offset = self._drag_offset if self._drag_offset is not None else thumb_h / 2
        fraction = max(0.0, min(1.0, (float(pointer_y) - offset) / movable))
        self._command("moveto", fraction)

WindowBase = ctk.CTk if CTK_AVAILABLE else tk.Tk


class GatewayApp(WindowBase):
    def after(self, ms, func=None, *args):
        """Ignore late worker callbacks once the Tk interpreter is closing."""
        if getattr(self, "_tk_destroyed", False):
            return ""
        try:
            return super().after(ms, func, *args)
        except (RuntimeError, tk.TclError):
            return ""

    def destroy(self):
        if getattr(self, "_tk_destroyed", False):
            return
        self._tk_destroyed = True
        try:
            pending = self.tk.call("after", "info")
            for callback_id in pending:
                try:
                    self.after_cancel(callback_id)
                except (RuntimeError, tk.TclError):
                    pass
        except (RuntimeError, tk.TclError):
            pass
        try:
            return super().destroy()
        except (RuntimeError, tk.TclError):
            return None

    def __init__(self):
        super().__init__()
        self._tk_destroyed = False
        self.title("灵境造片厂")
        self.geometry(f"{LAYOUT['window_w']}x{LAYOUT['window_h']}")
        self.minsize(LAYOUT["min_w"], LAYOUT["min_h"])
        if CTK_AVAILABLE:
            self.configure(fg_color=C["bg"])
        else:
            self.configure(bg=C["bg"])
        self._image_refs = []
        self._set_window_icon()

        # 子进程
        self._comfy_proc = None
        self._api_proc = None
        self._process_supervisor = ProcessSupervisor(BASE_DIR)
        self._backend_action_lock = threading.Lock()
        self._backend_action_name = ""
        self._runtime_maintenance_lock = threading.RLock()
        self._runtime_maintenance_in_progress = False
        self._runtime_update_helper_proc = None
        self._comfyui_update_worker_proc = None
        self._model_transfer_lock = threading.RLock()
        self._active_model_transfers = {}
        self._workflow_management_lock = threading.Lock()
        self._workflow_operation_name = ""
        self._tunnel_url = ""
        self._local_url = API_BASE
        self._api_key = ""
        self._available_release_version = ""
        self._release_update_check_started = False
        self._release_update_tooltip = None
        self._poll_run = False
        self._health_poll_thread = None
        self._health_poll_lock = threading.Lock()
        self._health_poll_generation = 0
        self._health_event_sequence = 0
        self._health_event_applied = 0
        self._health_needs_full_refresh = True
        self._shutting_down = False
        self._last_health = {}
        self._workflow_display_fingerprint_value = ""
        self._runtime_start_blocked = False
        self._capture_startup_update_results()
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_url_value = "https://ai.lol-lu.site"
        self._server_account_profile = {}
        # Local mode is the product default.  Platform login is optional and
        # must never block the local API gateway from starting.
        self._server_mode = "guest"
        self._server_sync_running = False
        self._server_sync_fail_count = 0
        self._heartbeat_run = True
        self._offline_notice_sent = False
        self._login_prompt_shown = False
        self._login_popup = None
        self._runtime_maintenance_popup = None
        self._model_import_in_progress = False
        self._account_status_text = ""
        self._initial_session_sync_done = False
        self._comfy_starting_until = 0
        self._client_instance_id = self._load_client_instance_id()
        self._last_completed_outputs = []
        self._last_completed_task_id = ""
        self._last_terminal_task_signature = ""
        self._task_history = []
        self._task_history_ids = set()
        self._workflow_mode = tk.StringVar(value="default")
        self._load_account_session_state()
        self._server_url_var = tk.StringVar(value=self._server_url_value or "https://ai.lol-lu.site")
        self._server_email_var = tk.StringVar(value=self._server_user_email)
        self._server_password_var = tk.StringVar(value="")

        # 系统托盘
        self._tray = None
        self._tray_thread = None

        # 状态缓存
        cleanup_incomplete_imports(_models_dir())
        cleanup_stale_workflow_imports(_workflows_dir(), BASE_DIR / "runtime")
        self._model_status = _check_models_status()
        self._environment_status = {}
        self._current_task_text = "无任务"

        self._setup_style()
        self._build_app_shell()
        self._build_sidebar()
        self._build_title_bar()
        self._build_page_host()
        self._build_bottom_lights()
        self._build_info_cards()
        self._build_main_area()
        self._build_workflow_model_panel()
        self._build_progress_panel()
        self._build_action_buttons()
        self._build_static_pages()
        self._build_footer()
        self._show_page("overview")
        if self._server_mode == "guest":
            self._set_light("server", "online", "本地模式")

        self.center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_minimize)

        # 异步启动序列
        if not self._runtime_start_blocked:
            self._request_backend_start("启动后台")
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self.after_idle(self._write_runtime_restart_ack)
        self.after(600, self._show_comfyui_recovery_result)
        self.after(800, self._show_runtime_update_result)
        self.after(1000, self._show_comfyui_update_result)
        self.after(1400, self._start_release_update_check)

    def center(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(
            f"{LAYOUT['window_w']}x{LAYOUT['window_h']}"
            f"+{(sw-LAYOUT['window_w'])//2}+{(sh-LAYOUT['window_h'])//2}"
        )

    def _account_session_path(self) -> Path:
        return BASE_DIR / "runtime" / "account_session.json"

    def _client_instance_path(self) -> Path:
        return BASE_DIR / "runtime" / "client_instance.json"

    def _load_client_instance_id(self) -> str:
        path = self._client_instance_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                value = str(data.get("instance_id") or "").strip()
                if value:
                    return value
        except Exception:
            pass
        value = f"desktop-{uuid.uuid4().hex}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"instance_id": value}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return value

    def _load_account_session_state(self):
        path = self._account_session_path()
        try:
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            mode = str(data.get("mode") or "unset").strip()
            if mode not in ("logged_in", "guest"):
                return
            self._server_mode = mode
            self._server_url_value = str(data.get("server_url") or self._server_url_value).strip() or self._server_url_value
            self._server_user_email = str(data.get("email") or "").strip()
            protected_token = str(data.get("session_token_protected") or "").strip()
            legacy_token = str(data.get("session_token") or "").strip()
            self._server_session_token = (
                unprotect_text(protected_token or legacy_token)
                if mode == "logged_in"
                else ""
            )
            if _platform_server_url_error(self._server_url_value):
                self._server_mode = "guest"
                self._server_session_token = ""
            profile = data.get("profile")
            self._server_account_profile = profile if isinstance(profile, dict) else {}
            if "session_token" in data:
                # Migrate legacy plaintext immediately.  Waiting for a later
                # successful server sync can leave the token exposed forever
                # on an offline machine.
                self._save_account_session()
        except Exception as ex:
            print(f"[Account] load session failed: {ex}")

    def _entry_text(self, entry_name: str, fallback: str = "") -> str:
        entry = getattr(self, entry_name, None)
        try:
            if entry is not None and entry.winfo_exists():
                return entry.get()
        except Exception:
            pass
        return fallback

    def _get_server_url(self) -> str:
        fallback = self._server_url_var.get() if hasattr(self, "_server_url_var") else self._server_url_value
        return (self._entry_text("_server_url_entry", fallback) or "https://ai.lol-lu.site").strip().rstrip("/")

    def _get_server_email(self) -> str:
        fallback = self._server_email_var.get() if hasattr(self, "_server_email_var") else self._server_user_email
        return _normalize_account_email(self._entry_text("_server_email_entry", fallback))

    def _get_server_password(self) -> str:
        fallback = self._server_password_var.get() if hasattr(self, "_server_password_var") else ""
        return self._entry_text("_server_password_entry", fallback)

    def _set_account_form_values(self, server_url=None, email=None, password=None):
        if server_url is not None:
            self._server_url_value = str(server_url).strip().rstrip("/") or self._server_url_value
        if email is not None:
            email = _normalize_account_email(email)
        if email is not None and self._server_mode != "logged_in":
            self._server_user_email = email
        values = {
            "_server_url_entry": (server_url, "_server_url_var"),
            "_server_email_entry": (email, "_server_email_var"),
            "_server_password_entry": (password, "_server_password_var"),
        }
        for entry_name, (value, var_name) in values.items():
            if value is None:
                continue
            value = str(value)
            var = getattr(self, var_name, None)
            if var is not None:
                var.set(value)
            entry = getattr(self, entry_name, None)
            try:
                if entry is not None and entry.winfo_exists():
                    entry.delete(0, tk.END)
                    entry.insert(0, value)
            except Exception:
                pass

    def _save_account_session(self):
        path = self._account_session_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "mode": self._server_mode,
                "server_url": self._server_url_value,
                "email": self._server_user_email,
                "session_token_protected": (
                    protect_text(self._server_session_token)
                    if self._server_mode == "logged_in" and self._server_session_token
                    else ""
                ),
                "profile": self._server_account_profile if isinstance(self._server_account_profile, dict) else {},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            print(f"[Account] save session failed: {ex}")

    def _clear_account_session(self):
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_account_profile = {}
        self._server_mode = "unset"
        if hasattr(self, "_server_email_var"):
            self._server_email_var.set("")
        try:
            path = self._account_session_path()
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _maybe_show_login_prompt(self):
        """Platform login is opt-in; startup always continues in local mode."""
        return

    def _setup_style(self):
        if CTK_AVAILABLE:
            ctk.set_appearance_mode("light")
            ctk.set_default_color_theme("blue")
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"])
        s.configure("Progress.Horizontal.TProgressbar",
                     background=C["primary"], troughcolor=C["progress_bg"],
                     borderwidth=0, lightcolor=C["primary"], darkcolor=C["primary"])
        s.layout(
            "LingJing.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            (
                                "Vertical.Scrollbar.thumb",
                                {"expand": "1", "sticky": "nswe"},
                            ),
                        ],
                    },
                ),
            ],
        )
        s.configure(
            "LingJing.Vertical.TScrollbar",
            background=C["primary"],
            troughcolor=C["soft_primary"],
            bordercolor=C["soft_primary"],
            lightcolor=C["primary"],
            darkcolor=C["primary"],
            relief="flat",
            borderwidth=0,
            gripcount=0,
            width=7,
        )
        s.map(
            "LingJing.Vertical.TScrollbar",
            background=[("pressed", C["primary2"]), ("active", C["primary2"])],
            lightcolor=[("pressed", C["primary2"]), ("active", C["primary2"])],
            darkcolor=[("pressed", C["primary2"]), ("active", C["primary2"])],
        )

    def _card(self, parent, **pack_kwargs):
        if CTK_AVAILABLE:
            frame = ctk.CTkFrame(
                parent,
                fg_color=C["card"],
                corner_radius=10,
                border_width=1,
                border_color=C["border2"],
            )
        else:
            frame = RoundedFrame(parent, radius=10, fill=C["card"], outline=C["border2"])
        if pack_kwargs:
            frame.pack(**pack_kwargs)
        return frame

    def _vertical_scrollbar(self, parent, command):
        """Create the single brand scrollbar used by every dashboard page."""
        return SlimRoundedScrollbar(
            parent,
            command,
            width=6,
            bar_width=4,
            color=C["primary"],
            active_color=C["primary2"],
            trough_color=C["card"],
        )

    def _set_window_icon(self):
        icon_png = BASE_DIR / "icon.png"
        icon_ico = GUI_ASSET_DIR / "app.ico"
        try:
            if icon_ico.exists():
                self.iconbitmap(str(icon_ico))
        except Exception as exc:
            print(f"[GUI] iconbitmap failed: {exc}")
        try:
            if icon_png.exists():
                from PIL import Image, ImageTk
                icon = Image.open(icon_png).convert("RGBA").resize((32, 32), Image.LANCZOS)
                self._window_icon_img = ImageTk.PhotoImage(icon)
                self._image_refs.append(self._window_icon_img)
                self.iconphoto(True, self._window_icon_img)
        except Exception as exc:
            print(f"[GUI] iconphoto failed: {exc}")

    def _button(self, parent, text, command, variant="plain", width=None):
        if variant == "primary":
            bg, fg, active = C["primary"], "#ffffff", C["primary2"]
        elif variant == "success":
            bg, fg, active = C["success"], "#ffffff", "#22c55e"
        elif variant == "warn":
            bg, fg, active = C["button"], C["warn"], C["hover"]
        else:
            bg, fg, active = C["button"], C["text"], C["hover"]
        if CTK_AVAILABLE:
            return ctk.CTkButton(
                parent,
                text=text,
                font=F["button"],
                width=width or 76,
                height=30,
                corner_radius=7,
                fg_color=bg,
                hover_color=active,
                text_color=fg,
                border_width=0 if variant in ("primary", "success") else 1,
                border_color=C["border2"],
                command=command,
            )
        return tk.Button(
            parent,
            text=text,
            font=F["button"],
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            bd=0,
            # CTk uses pixels while classic Tk uses character cells.
            width=max(4, int(round(width / 9))) if width else None,
            cursor="hand2",
            command=command,
            highlightthickness=1,
            highlightbackground=C["border2"],
            padx=8,
            pady=2,
        )

    def _entry_widget(self, parent, show=None, width=None, font=None, height=None):
        entry_font = font or F["small"]
        if CTK_AVAILABLE:
            entry = ctk.CTkEntry(
                parent,
                font=entry_font,
                width=width or 180,
                height=height or 34,
                corner_radius=8,
                fg_color=C["entry"],
                text_color=C["text"],
                border_color=C["border2"],
                border_width=1,
                show=show,
            )
            return entry
        return tk.Entry(
            parent,
            font=entry_font,
            bg=C["entry"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            width=width or 22,
            show=show,
            highlightthickness=1,
            highlightbackground=C["border2"],
        )

    def _section_title(self, parent, title, subtitle=""):
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x")
        tk.Label(row, text=title, font=F["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
        if subtitle:
            tk.Label(row, text=subtitle, font=F["small"], fg=C["muted"], bg=C["card"]).pack(side="right")
        return row

    # ══════════════════════════════════════════════════════
    # 启动序列
    # ══════════════════════════════════════════════════════
    def _run_runtime_probe(self, role: str, args, **kwargs):
        """Launch an environment probe only while runtime replacement is excluded."""
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            raise RuntimeError("运行环境正在维护")
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                raise RuntimeError("运行环境正在维护")
            return self._process_supervisor.run(role, args, **kwargs)
        finally:
            lock.release()

    def _run_ui_backend_step(self, callback, timeout: float = 20.0) -> bool:
        """Run one Tk-bound lifecycle step and keep its action reservation."""
        completed = threading.Event()
        errors = []

        def invoke():
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        try:
            self.after(0, invoke)
        except Exception:
            completed.set()
            raise

        deadline = time.monotonic() + max(1.0, float(timeout))
        while not completed.wait(0.05):
            if self._shutting_down:
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError("界面未能及时执行后台服务操作")
        if errors:
            raise errors[0]
        return not self._shutting_down

    def _post_to_ui(self, callback, delay: int = 0) -> bool:
        """Queue a short UI callback unless shutdown has already begun."""
        if self._shutting_down:
            return False

        def invoke():
            if not self._shutting_down:
                callback()

        try:
            self.after(max(0, int(delay)), invoke)
            return True
        except (RuntimeError, tk.TclError):
            return False

    def _request_backend_start(self, action_name: str = "启动后台") -> bool:
        """Start the full environment/backend sequence as one serialized action."""
        if self.__dict__.get("_runtime_start_blocked", False):
            self.after(
                0,
                lambda: self._footer_label.config(
                    text="  自动恢复未完成，请先修复运行环境"
                ),
            )
            return False
        if not self._begin_backend_action(action_name):
            return False
        try:
            threading.Thread(
                target=self._backend_start_worker,
                args=(action_name,),
                daemon=True,
            ).start()
        except Exception:
            self._end_backend_action()
            raise
        return True

    def _backend_start_worker(self, action_name: str):
        try:
            self._startup_sequence()
        except Exception as exc:
            self.after(
                0,
                lambda name=action_name, message=str(exc): self._report_backend_failure(
                    f"{name}失败：{message}"
                ),
            )
        finally:
            self._end_backend_action()

    def _startup_sequence(self):
        """主启动序列（后台线程）"""
        if self._shutting_down or self._runtime_maintenance_active():
            return

        # Check the package first so first-time users land in the single
        # environment maintenance center instead of the legacy install page.
        missing = missing_runtime_paths(BASE_DIR)
        if missing:
            result = {
                "package_ready": False,
                "ready": False,
                "missing": missing,
                "gpu_name": "",
                "message": f"运行环境不完整，缺少 {len(missing)} 项",
            }
            def mark_backend_waiting_for_install():
                if self._shutting_down:
                    return
                for key in ("comfyui", "api", "tunnel"):
                    self._set_light(key, "offline", "等待安装环境")
            self.after(0, lambda data=result: self._finish_runtime_recheck(data))
            self.after(0, mark_backend_waiting_for_install)
            self.after(0, self._open_runtime_maintenance)
            return

        self.after(0, lambda: self._set_light("env", "loading", "检查中"))
        system_info = _check_system_env(
            lambda args, **kwargs: self._run_runtime_probe(
                "environment-check", args, **kwargs
            )
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        python_exe = BASE_DIR / "runtime" / "python" / "python.exe"
        torch_info = _check_torch(
            python_exe,
            lambda args, **kwargs: self._run_runtime_probe(
                "torch-check", args, **kwargs
            ),
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        environment_ready = bool(system_info.get("success") and torch_info.get("success"))
        if not system_info.get("success"):
            message = str(system_info.get("error") or "NVIDIA 显卡环境检查未通过")
            print(f"[Startup] 系统环境异常: {message}")
        elif not torch_info.get("success"):
            message = str(torch_info.get("error") or "PyTorch / CUDA 检查未通过")
            print(f"[Startup] PyTorch 异常: {message}")
        else:
            message = ""
        environment_result = {
            "package_ready": True,
            "ready": environment_ready,
            "missing": [],
            "gpu_name": str(torch_info.get("gpu_name") or system_info.get("gpu_name") or ""),
            "message": message,
        }
        self.after(0, lambda data=environment_result: self._finish_runtime_recheck(data))

        if self._shutting_down or self._runtime_maintenance_active():
            return

        self.after(0, lambda: self._set_light("models", "loading"))
        self._model_status = _check_models_status()
        if self._model_status["all_ok"]:
            self.after(0, lambda: self._set_light("models", "online", "完整"))
        else:
            self.after(0, lambda: self._set_light("models", "offline", "缺失"))
        self.after(0, self._update_model_display)

        # 确保模型路径配置
        _ensure_extra_model_paths()

        if environment_ready and not self._runtime_maintenance_active():
            self._run_ui_backend_step(self._start_backend)
        elif not environment_ready:
            def mark_backend_blocked():
                if self._shutting_down:
                    return
                for key in ("comfyui", "api", "tunnel"):
                    self._set_light(key, "offline", "等待环境修复")
            self.after(0, mark_backend_blocked)

    # ══════════════════════════════════════════════════════
    # 应用外壳：侧栏 + 页面容器
    # ══════════════════════════════════════════════════════
    def _build_app_shell(self):
        self._app_shell = tk.Frame(self, bg=C["bg"])
        self._app_shell.pack(fill="both", expand=True)

        self._sidebar = tk.Frame(
            self._app_shell,
            bg=C["sidebar"],
            width=LAYOUT["sidebar_w"],
            highlightthickness=1,
            highlightbackground=C["sidebar_border"],
        )
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        self._content_root = tk.Frame(self._app_shell, bg=C["bg"])
        self._content_root.pack(side="left", fill="both", expand=True)

    def _build_sidebar(self):
        brand = tk.Frame(self._sidebar, bg=C["sidebar"])
        brand.pack(fill="x", padx=16, pady=(18, 15))

        icon_path = BASE_DIR / "icon.png"
        if icon_path.exists():
            try:
                from PIL import Image, ImageTk
                icon = Image.open(icon_path).convert("RGBA").resize((38, 38), Image.LANCZOS)
                self._sidebar_brand_icon = ImageTk.PhotoImage(icon)
                self._image_refs.append(self._sidebar_brand_icon)
                tk.Label(brand, image=self._sidebar_brand_icon, bg=C["sidebar"]).pack(side="left", padx=(0, 9))
            except Exception:
                tk.Label(brand, text="▷", font=("Microsoft YaHei UI", 20, "bold"), fg=C["primary"], bg=C["sidebar"]).pack(side="left", padx=(0, 10))

        brand_text = tk.Frame(brand, bg=C["sidebar"])
        brand_text.pack(side="left", fill="x", expand=True)
        tk.Label(brand_text, text="灵境造片厂", font=("Microsoft YaHei UI", 13, "bold"), fg=C["text"], bg=C["sidebar"]).pack(anchor="w")
        tk.Label(brand_text, text="LOCAL AI GATEWAY", font=F["tiny"], fg=C["muted"], bg=C["sidebar"]).pack(anchor="w", pady=(2, 0))

        tk.Frame(self._sidebar, bg=C["sidebar_border"], height=1).pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(self._sidebar, text="工作台", font=F["tiny"], fg=C["muted"], bg=C["sidebar"]).pack(anchor="w", padx=22, pady=(0, 7))

        nav_specs = [
            ("overview", "▦", "控制台"),
            ("resources", "▣", "模型与环境"),
            ("settings", "⚙", "设置"),
        ]
        self._nav_buttons = {}
        for page_id, icon, label in nav_specs:
            if CTK_AVAILABLE:
                button = ctk.CTkButton(
                    self._sidebar,
                    text=f"{icon}    {label}",
                    font=F["button"],
                    anchor="w",
                    height=39,
                    corner_radius=8,
                    fg_color="transparent",
                    hover_color=C["hover"],
                    text_color=C["text2"],
                    command=lambda key=page_id: self._show_page(key),
                )
            else:
                button = tk.Button(
                    self._sidebar,
                    text=f"{icon}    {label}",
                    font=F["button"],
                    anchor="w",
                    bg=C["sidebar"],
                    fg=C["text2"],
                    activebackground=C["hover"],
                    activeforeground=C["text"],
                    relief="flat",
                    bd=0,
                    cursor="hand2",
                    command=lambda key=page_id: self._show_page(key),
                )
            button.pack(fill="x", padx=12, pady=2)
            self._nav_buttons[page_id] = button

        tk.Frame(self._sidebar, bg=C["sidebar"]).pack(fill="both", expand=True)

        node = tk.Frame(self._sidebar, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        node.pack(fill="x", padx=12, pady=(8, 10))
        row = tk.Frame(node, bg=C["hover"])
        row.pack(fill="x", padx=10, pady=(9, 2))
        dot = tk.Canvas(row, width=10, height=10, bg=C["hover"], highlightthickness=0)
        dot.pack(side="left", padx=(0, 7), pady=(2, 0))
        dot.create_oval(1, 1, 9, 9, fill=C["success"], outline="")
        tk.Label(row, text="本机节点", font=F["bold"], fg=C["text"], bg=C["hover"]).pack(side="left")
        tk.Label(node, text="客户端正在运行", font=F["tiny"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", padx=27, pady=(0, 9))

        self._version_row = tk.Frame(self._sidebar, bg=C["sidebar"], cursor="")
        self._version_row.pack(fill="x", padx=18, pady=(0, 14))
        self._version_label = tk.Label(
            self._version_row,
            text=f"Desktop  {APP_VERSION}",
            font=F["tiny"],
            fg=C["muted"],
            bg=C["sidebar"],
        )
        self._version_label.pack(side="left")
        self._release_update_dot = tk.Canvas(
            self._version_row,
            width=10,
            height=10,
            bg=C["sidebar"],
            highlightthickness=0,
            cursor="hand2",
        )
        self._release_update_dot_item = self._release_update_dot.create_oval(
            1,
            1,
            9,
            9,
            fill=C["success"],
            outline="",
        )
        for widget in (self._version_row, self._version_label, self._release_update_dot):
            widget.bind("<Enter>", self._show_release_update_tooltip, add="+")
            widget.bind("<Leave>", self._hide_release_update_tooltip, add="+")
            widget.bind("<Button-1>", self._open_latest_release, add="+")

    def _start_release_update_check(self):
        if self._release_update_check_started or not _release_version_tuple(APP_VERSION):
            return
        self._release_update_check_started = True
        threading.Thread(target=self._release_update_worker, daemon=True).start()

    def _release_update_worker(self):
        try:
            latest = _fetch_latest_release_version()
        except Exception:
            return
        if self._shutting_down or not _is_newer_release(APP_VERSION, latest):
            return
        try:
            self.after(
                0,
                lambda version=latest: self._apply_available_release(version),
            )
        except (RuntimeError, tk.TclError):
            return

    def _apply_available_release(self, version: str):
        if not _is_newer_release(APP_VERSION, version):
            return
        self._available_release_version = str(version)
        if not self._release_update_dot.winfo_manager():
            self._release_update_dot.pack(side="left", padx=(7, 0), pady=(2, 0))
        for widget in (self._version_row, self._version_label, self._release_update_dot):
            widget.configure(cursor="hand2")

    def _release_update_message(self) -> str:
        if not self._available_release_version:
            return ""
        return (
            f"发现新版本 {self._available_release_version}，"
            "点击前往 GitHub Release 更新"
        )

    def _show_release_update_tooltip(self, _event=None):
        message = self._release_update_message()
        if not message or self._release_update_tooltip is not None:
            return
        tooltip = tk.Toplevel(self)
        tooltip.wm_overrideredirect(True)
        try:
            tooltip.attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(
            tooltip,
            text=message,
            font=F["small"],
            fg="#ffffff",
            bg=C["text"],
            relief="solid",
            bd=1,
            padx=10,
            pady=7,
        )
        label.pack()
        tooltip.update_idletasks()
        x = self._version_row.winfo_rootx()
        y = max(0, self._version_row.winfo_rooty() - tooltip.winfo_reqheight() - 6)
        tooltip.geometry(f"+{x}+{y}")
        self._release_update_tooltip = tooltip

    def _hide_release_update_tooltip(self, _event=None):
        tooltip = self._release_update_tooltip
        self._release_update_tooltip = None
        if tooltip is not None:
            try:
                tooltip.destroy()
            except tk.TclError:
                pass

    def _open_latest_release(self, _event=None):
        if self._available_release_version:
            webbrowser.open(PROJECT_RELEASES_URL)

    def _build_page_host(self):
        self._page_host = tk.Frame(self._content_root, bg=C["bg"])
        self._page_host.pack(fill="both", expand=True)
        self._pages = {}
        self._current_page_id = ""
        self._overview_page = tk.Frame(self._page_host, bg=C["bg"])
        self._pages["overview"] = self._overview_page

    def _build_static_pages(self):
        self._dashboard_pages = StaticDashboardPages(self, C, F)
        for page_id in ("resources", "settings"):
            self._pages[page_id] = self._dashboard_pages.build(self._page_host, page_id)

    def _show_page(self, page_id: str):
        if page_id == "workflows":
            page_id = "resources"
        page = self._pages.get(page_id)
        if page is None:
            return
        if self._current_page_id == page_id and page.winfo_ismapped():
            return

        current = self._pages.get(self._current_page_id)
        if current is not None and current.winfo_ismapped():
            current.pack_forget()
        page.pack(fill="both", expand=True)
        self._current_page_id = page_id

        page_meta = {
            "overview": ("控制台", "服务状态、调用信息与当前任务"),
            "resources": ("模型与环境", "管理工作流配置、模型与运行环境"),
            "settings": ("设置", "访问密钥、文件位置与软件基础配置"),
        }
        title, subtitle = page_meta[page_id]
        if hasattr(self, "_page_title_label"):
            self._page_title_label.config(text=title)
            self._page_subtitle_label.config(text=subtitle)

        for key, button in self._nav_buttons.items():
            active = key == page_id
            if CTK_AVAILABLE:
                button.configure(
                    fg_color=C["sidebar_active"] if active else "transparent",
                    text_color=C["primary"] if active else C["text2"],
                )
            else:
                button.configure(
                    bg=C["sidebar_active"] if active else C["sidebar"],
                    fg=C["primary"] if active else C["text2"],
                )

    def _open_runtime_maintenance(self):
        """Open the shared maintenance center without starting an install."""
        self._show_page("resources")
        dashboard = getattr(self, "_dashboard_pages", None)
        if dashboard is not None:
            self.after_idle(dashboard.focus_runtime_maintenance)

    # ══════════════════════════════════════════════════════
    # 顶栏：品牌 + 账号信息
    # ══════════════════════════════════════════════════════
    def _build_title_bar(self):
        bar = self._card(self._content_root)
        bar.configure(height=LAYOUT["top_h"])
        bar.pack(fill="x", padx=16, pady=(12, 8))
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=C["surface"])
        left.pack(side="left", padx=18, pady=12)
        tk.Label(left, text="LOCAL AI GATEWAY  /  DESKTOP", font=F["tiny"], fg=C["primary"], bg=C["surface"]).pack(anchor="w")
        title_row = tk.Frame(left, bg=C["surface"])
        title_row.pack(anchor="w", pady=(1, 0))
        self._page_title_label = tk.Label(
            title_row,
            text="控制台",
            font=("Microsoft YaHei UI", 16, "bold"),
            fg=C["text"],
            bg=C["surface"],
        )
        self._page_title_label.pack(side="left")
        self._page_subtitle_label = tk.Label(
            title_row,
            text="服务状态、调用信息与当前任务",
            font=F["small"],
            fg=C["muted"],
            bg=C["surface"],
        )
        self._page_subtitle_label.pack(side="left", padx=(12, 0), pady=(4, 0))

        tk.Frame(bar, bg=C["surface"]).pack(side="left", fill="x", expand=True)

        right = tk.Frame(bar, bg=C["surface"])
        right.pack(side="right", padx=16, pady=18)

        self._account_badge = tk.Frame(right, bg=C["surface"], cursor="hand2")
        self._account_badge.pack(side="left", padx=(0, 12))
        self._account_avatar_label = tk.Canvas(
            self._account_badge,
            bg=C["primary"],
            width=34,
            height=34,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._account_avatar_label.pack(side="left", padx=(0, 8))
        self._account_summary_label = tk.Label(
            self._account_badge,
            text="未登录 · 普通用户 · 积分 0",
            font=F["small"],
            fg=C["text"],
            bg=C["surface"],
            cursor="hand2",
        )
        self._account_summary_label.pack(side="left")
        for widget in (self._account_badge, self._account_avatar_label, self._account_summary_label):
            widget.bind("<Button-1>", lambda _event: self._show_account_popup())
        self._render_account_badge()

    # ══════════════════════════════════════════════════════
    # 底部状态灯条
    # ══════════════════════════════════════════════════════
    def _build_bottom_lights(self):
        bar = self._card(self._overview_page, fill="x", padx=LAYOUT["outer"], pady=(4, 8))
        bar.configure(height=LAYOUT["status_h"])
        bar.pack_propagate(False)

        lights = [
            ("server",  "服务器"),
            ("env",     "运行环境"),
            ("comfyui", "ComfyUI"),
            ("tunnel",  "公网连接"),
            ("api",     "API"),
        ]
        self._light_groups = {}
        self._light_states = {}

        for key, label_text in lights:
            group = tk.Frame(bar, bg=C["surface"])
            group.pack(side="left", fill="x", expand=True, padx=10, pady=9)
            dot = tk.Canvas(group, width=10, height=10, bg=C["surface"], highlightthickness=0)
            dot.pack(side="left", padx=(0, 8), pady=(2, 0))
            dot_id = dot.create_oval(1, 1, 9, 9, fill=C["muted"], outline="")
            lbl = tk.Label(group, text=label_text, font=F["normal"], fg=C["text"], bg=C["surface"])
            lbl.pack(side="left", padx=(0, 6))
            status_lbl = tk.Label(group, text="检测中", font=F["normal"], fg=C["warn"], bg=C["surface"])
            status_lbl.pack(side="left")
            retry_btn = tk.Button(
                group,
                text="重试",
                font=F["tiny"],
                bg=C["hover"],
                fg=C["primary"],
                activebackground=C["border"],
                relief="flat",
                bd=0,
                cursor="hand2",
                command=lambda k=key: self._retry_component(k),
            )

            self._light_groups[key] = (dot, dot_id, lbl, status_lbl, retry_btn)
            self._light_states[key] = "loading"

        # 加载动画在 footer 创建后启动，避免启动早于底部标签。
        self._anim_running = True
        self._anim_frame = 0

    def _animate_loading(self):
        if not self._anim_running:
            return
        self._anim_frame += 1
        dots_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spinner = dots_chars[self._anim_frame % len(dots_chars)]

        loading_count = sum(1 for s in self._light_states.values() if s == "loading")
        if loading_count > 0:
            self._loading_text.config(text="服务加载中", fg=C["warn"])
            self._loading_dots.config(text=spinner)
            flash_on = (self._anim_frame // 6) % 2 == 0
            for key, state in self._light_states.items():
                if state == "loading":
                    dot, dot_id, _, status_lbl, _ = self._light_groups[key]
                    color = C["warn"] if flash_on else C["border"]
                    dot.itemconfig(dot_id, fill=color)
                    status_lbl.config(text="加载中...", fg=C["warn"] if flash_on else C["text2"])
        else:
            self._loading_text.config(text="就绪", fg=C["success"])
            self._loading_dots.config(text="✓")

        self.after(150, self._animate_loading)

    # ══════════════════════════════════════════════════════
    # URL + Key 卡片
    # ══════════════════════════════════════════════════════
    def _build_info_cards(self):
        self._info_frame = tk.Frame(self._overview_page, bg=C["bg"])
        self._info_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(8, 10))

        self._url_stack = tk.Frame(
            self._info_frame,
            bg=C["bg"],
            height=LAYOUT["info_h"],
        )
        self._url_stack.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._url_stack.pack_propagate(False)

        def build_url_card(*, title, icon, value, color, command):
            card = self._card(self._url_stack)
            card.configure(height=LAYOUT["info_row_h"])
            card.pack_propagate(False)
            row = tk.Frame(card, bg=C["card"])
            row.pack(fill="both", expand=True, padx=12, pady=7)
            tk.Label(
                row,
                text=icon,
                font=("Microsoft YaHei UI", 14, "bold"),
                fg=color,
                bg=C["card"],
                width=2,
            ).pack(side="left", padx=(0, 7))
            text_box = tk.Frame(row, bg=C["card"])
            text_box.pack(side="left", fill="x", expand=True)
            tk.Label(
                text_box,
                text=title,
                font=F["bold"],
                fg=C["text"],
                bg=C["card"],
            ).pack(anchor="w")
            value_label = tk.Label(
                text_box,
                text=value,
                font=F["url"],
                fg=color,
                bg=C["card"],
                anchor="w",
            )
            value_label.pack(anchor="w", pady=(1, 0))
            copy_box = tk.Frame(row, bg=C["card"], width=62, height=28)
            copy_box.pack(side="right", padx=(8, 0))
            copy_box.pack_propagate(False)
            self._button(copy_box, "复制", command, "plain", width=60).pack(
                fill="both",
                expand=True,
            )
            return card, value_label

        self._public_url_card, self._url_label = build_url_card(
            title="公网 URL",
            icon="◎",
            value="正在建立公网连接...",
            color=C["primary"],
            command=self._copy_public_url,
        )
        self._public_url_card.pack(side="top", fill="x")
        self._local_url_card, self._local_url_label = build_url_card(
            title="本地 URL",
            icon="⌂",
            value=self._local_url,
            color=C["success"],
            command=self._copy_local_url,
        )
        self._local_url_card.pack(side="top", fill="x", pady=(8, 0))
        from app.core.single_user_assets import lan_urls
        addresses = lan_urls(urlsplit(API_BASE).port or 18188)
        self._lan_url = addresses[0] if addresses else ""
        self._lan_url_card, self._lan_url_label = build_url_card(
            title="局域网 URL", icon="⌁", value=self._lan_url or "未检测到局域网，请用公网 URL",
            color=C["success"], command=lambda: self._copy(self._lan_url) if self._lan_url else None,
        )
        self._lan_url_card.pack(side="top", fill="x", pady=(8, 0))

        # API Key
        self._api_key_card = self._card(self._info_frame)
        self._api_key_card.configure(height=LAYOUT["info_h"])
        self._api_key_card.pack(side="left", fill="both", expand=True)
        self._api_key_card.pack_propagate(False)
        key_row = tk.Frame(self._api_key_card, bg=C["card"])
        key_row.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(key_row, text="⚿", font=("Microsoft YaHei UI", 18, "bold"),
                 fg=C["primary"], bg=C["card"], width=2).pack(side="left", padx=(0, 8))
        key_text_box = tk.Frame(key_row, bg=C["card"])
        key_text_box.pack(side="left", fill="x", expand=True)
        tk.Label(key_text_box, text="API Key", font=F["bold"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        self._key_label = tk.Label(key_text_box, text="生成中...", font=F["mono"],
                                   fg=C["success"], bg=C["card"], anchor="w")
        self._key_label.pack(anchor="w", pady=(5, 0))
        key_copy_box = tk.Frame(key_row, bg=C["card"], width=68, height=30)
        key_copy_box.pack(side="right", padx=(10, 0))
        key_copy_box.pack_propagate(False)
        self._button(key_copy_box, "复制", self._copy_api_key, "plain", width=66).pack(fill="both", expand=True)

    def _account_summary_text(self) -> str:
        if self._server_mode == "guest":
            return "本地模式"
        email = self._server_user_email or "未登录"
        profile = self._server_account_profile if isinstance(self._server_account_profile, dict) else {}
        user = profile.get("user") if isinstance(profile.get("user"), dict) else profile
        account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
        vip_level = (
            account.get("vipLevel")
            or account.get("vip_level")
            or account.get("vip")
            or user.get("vipLevel")
            or user.get("vip_level")
            or user.get("vip")
        )
        membership_value = (
            account.get("membership")
            or account.get("membershipStatus")
            or account.get("memberLevel")
            or account.get("member_level")
            or user.get("membership")
            or user.get("membershipStatus")
            or user.get("memberLevel")
            or user.get("member_level")
            or "普通用户"
        )
        if isinstance(membership_value, dict):
            membership = str(
                membership_value.get("name")
                or membership_value.get("label")
                or membership_value.get("level")
                or membership_value.get("status")
                or "普通用户"
            )
        else:
            membership = str(membership_value)
        if isinstance(vip_level, bool):
            vip_text = "VIP" if vip_level else membership
        elif vip_level not in (None, ""):
            vip_text = f"VIP {vip_level}"
        else:
            vip_text = membership
        points = (
            account.get("points")
            if account.get("points") not in (None, "")
            else account.get("score", account.get("credits", account.get("remainingCredits", account.get("creditsRemaining", None))))
        )
        if points in (None, ""):
            points = user.get("points", user.get("score", user.get("credits", user.get("remainingCredits", 0))))
        return f"{email} · {vip_text} · 积分 {points}"

    def _draw_account_avatar(self, text: str, color: str):
        if not hasattr(self, "_account_avatar_label"):
            return
        canvas = self._account_avatar_label
        canvas.configure(bg=C["surface"])
        canvas.delete("all")
        RoundedFrame._round_rect(canvas, 1, 1, 33, 33, 8, fill=color, outline=color, width=1)
        canvas.create_text(
            17,
            17,
            text=(text or "登")[:1].upper(),
            fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def _render_account_badge(self):
        if not hasattr(self, "_account_summary_label"):
            return
        if self._server_mode == "logged_in":
            avatar = (self._server_user_email[:1] or "账").upper()
            self._draw_account_avatar(avatar, C["primary"])
            self._account_summary_label.config(text=self._account_summary_text(), fg=C["text"])
        elif self._server_mode == "guest":
            self._draw_account_avatar("本", C["success"])
            self._account_summary_label.config(text="本地模式", fg=C["text"])
        else:
            self._draw_account_avatar("登", C["text2"])
            self._account_summary_label.config(text="未登录", fg=C["text2"])

    def _show_account_popup(self):
        """Show account details when logged in; otherwise offer optional login."""
        if self._server_mode != "logged_in" or not self._server_session_token:
            self._show_login_prompt(force=True)
            return

        existing = getattr(self, "_account_popup", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            pass

        popup = tk.Toplevel(self)
        self._account_popup = popup
        popup.title("账号信息")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        self._center_popup(popup, 480, 380)

        def close_popup():
            self._account_popup = None
            try:
                popup.destroy()
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", close_popup)
        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)

        head = tk.Frame(panel, bg=C["card"])
        head.pack(fill="x", padx=24, pady=(22, 14))
        avatar = tk.Canvas(head, width=48, height=48, bg=C["card"], highlightthickness=0)
        avatar.pack(side="left", padx=(0, 12))
        RoundedFrame._round_rect(avatar, 1, 1, 47, 47, 12, fill=C["primary"], outline=C["primary"])
        avatar.create_text(24, 24, text=(self._server_user_email[:1] or "账").upper(), fill="#fff", font=("Microsoft YaHei UI", 14, "bold"))
        identity = tk.Frame(head, bg=C["card"])
        identity.pack(side="left", fill="x", expand=True)
        tk.Label(identity, text=self._server_user_email or "平台账号", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        account_summary = self._account_summary_text()
        email_prefix = f"{self._server_user_email} · "
        if account_summary.startswith(email_prefix):
            account_summary = account_summary[len(email_prefix):]
        tk.Label(identity, text=account_summary, font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", pady=(4, 0))
        self._badge_for_account_popup = tk.Label(head, text="  已同步  ", font=F["small"], fg=C["success"], bg=C["soft_success"], padx=4, pady=3)
        self._badge_for_account_popup.pack(side="right")

        info = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        info.pack(fill="x", padx=24, pady=(0, 16))
        rows = [
            ("平台同步", "已开启"),
            ("本地服务", "退出登录后仍可正常使用"),
            ("公网连接", "由客户端自动建立"),
        ]
        for label, value in rows:
            row = tk.Frame(info, bg=C["hover"])
            row.pack(fill="x", padx=12, pady=6)
            tk.Label(row, text=label, font=F["small"], fg=C["muted"], bg=C["hover"]).pack(side="left")
            tk.Label(row, text=value, font=F["small"], fg=C["text"], bg=C["hover"]).pack(side="right")

        tk.Label(
            panel,
            text=(
                "退出登录只会停止平台同步，不会自动更换已共享的访问密钥。"
                "如果不再信任该平台，请退出后到设置中重新生成访问密钥。"
            ),
            font=F["small"],
            fg=C["warn"],
            bg=C["card"],
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=24, pady=(0, 14))

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=24, pady=(0, 18))
        self._button(actions, "退出登录", lambda: self._logout_account(close_popup), "plain").pack(fill="x", expand=True)

    def _logout_account(self, close_callback=None):
        server_url = self._server_url_value
        token = self._server_session_token
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_account_profile = {}
        self._server_mode = "guest"
        self._initial_session_sync_done = False
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._set_account_status("本地模式：不会连接平台服务端", "success")
        self._set_light("server", "online", "本地模式")
        if hasattr(self, "_footer_label"):
            self._footer_label.config(text="  已退出平台；如需撤销旧调用权限，请重新生成访问密钥")
        if callable(close_callback):
            close_callback()
        if token:
            threading.Thread(
                target=self._notify_server_offline,
                kwargs={"server_url": server_url, "token": token},
                daemon=True,
            ).start()

    def _apply_account_visibility(self):
        if not hasattr(self, "_account_frame"):
            return
        if self._server_mode in ("logged_in", "guest"):
            if self._account_frame.winfo_ismapped():
                self._account_frame.pack_forget()
        else:
            if not self._account_frame.winfo_ismapped():
                self._account_frame.pack(fill="x", padx=16, pady=(4, 8), before=self._wf_frame)

    def _hide_account_panel(self):
        if self._account_frame.winfo_ismapped():
            self._account_frame.pack_forget()

    def _toggle_account_panel(self):
        if self._account_frame.winfo_ismapped():
            self._account_frame.pack_forget()
        else:
            self._account_frame.pack(fill="x", padx=16, pady=(4, 8), before=self._wf_frame)

    def _build_account_panel(self):
        self._account_frame = tk.Frame(self, bg=C["card"])
        self._account_frame.pack(fill="x", padx=16, pady=(4, 8))

        header = tk.Frame(self._account_frame, bg=C["card"])
        header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(header, text="服务端连接", font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._account_status_label = tk.Label(
            header,
            text="游客模式：可复制 URL / Key 给第三方调用",
            font=F["small"],
            fg=C["warn"],
            bg=C["card"],
            anchor="e",
        )
        self._account_status_label.pack(side="right")

        row = tk.Frame(self._account_frame, bg=C["card"])
        row.pack(fill="x", padx=12, pady=(0, 10))

        def field(parent, label, width=22, show=None):
            box = tk.Frame(parent, bg=C["card"])
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=label, font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w")
            entry = self._entry_widget(box, show=show, width=width * 8 if CTK_AVAILABLE else width)
            entry.pack(fill="x", ipady=4 if not CTK_AVAILABLE else 0)
            return entry

        self._server_url_entry = field(row, "服务端地址", 26)
        self._server_url_entry.insert(0, self._server_url_value or "https://ai.lol-lu.site")
        self._server_email_entry = field(row, "账号邮箱", 20)
        if self._server_user_email:
            self._server_email_entry.insert(0, self._server_user_email)
        self._server_password_entry = field(row, "密码", 18, show="*")

        btn_box = tk.Frame(row, bg=C["card"])
        btn_box.pack(side="left", padx=(0, 0), pady=(16, 0))
        tk.Button(
            btn_box,
            text="登录并同步",
            font=F["small"],
            bg=C["primary"],
            fg="#fff",
            activebackground=C["accent"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._login_and_sync,
        ).pack(side="left", ipadx=8, ipady=4, padx=(0, 6))
        tk.Button(
            btn_box,
            text="游客模式",
            font=F["small"],
            bg=C["surface"],
            fg=C["text"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._use_guest_mode,
        ).pack(side="left", ipadx=8, ipady=4)
        tk.Button(
            btn_box,
            text="注册",
            font=F["small"],
            bg=C["surface"],
            fg=C["primary"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_register,
        ).pack(side="left", ipadx=8, ipady=4, padx=(6, 0))
        tk.Button(
            btn_box,
            text="收起",
            font=F["small"],
            bg=C["surface"],
            fg=C["text2"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._hide_account_panel,
        ).pack(side="left", ipadx=8, ipady=4, padx=(6, 0))

    def _build_main_area(self):
        self._main_area = tk.Frame(self._overview_page, bg=C["bg"])
        self._main_area.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(0, 12))

        self._left_panel_parent = tk.Frame(self._main_area, bg=C["bg"], width=LAYOUT["left_w"])
        self._left_panel_parent.pack(side="left", fill="y", padx=(0, LAYOUT["gap"]))
        self._left_panel_parent.pack_propagate(False)

        self._right_panel_parent = tk.Frame(self._main_area, bg=C["bg"])
        self._right_panel_parent.pack(side="left", fill="both", expand=True)

    # ══════════════════════════════════════════════════════
    # 进度监控面板
    # ══════════════════════════════════════════════════════
    def _build_progress_panel(self):
        parent = getattr(self, "_right_panel_parent", self)
        self._prog_frame = self._card(parent)
        self._prog_frame.configure(height=300)
        self._prog_frame.pack(fill="both", expand=True)
        self._prog_frame.pack_propagate(False)

        # 任务状态文字行
        task_head = tk.Frame(self._prog_frame, bg=C["card"])
        task_head.pack(fill="x", padx=16, pady=(15, 8))
        self._task_status_label = tk.Label(
            task_head, text="当前任务", font=F["h2"],
            fg=C["text"], bg=C["card"], anchor="w")
        self._task_status_label.pack(side="left", fill="x", expand=True)
        self._preview_output_btn = tk.Button(
            task_head,
            text="预览结果",
            font=F["small"],
            bg=C["primary"],
            fg="#ffffff",
            activebackground=C["accent"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_last_output,
        )

        content_row = tk.Frame(self._prog_frame, bg=C["card"])
        content_row.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        progress_col = tk.Frame(content_row, bg=C["card"])
        progress_col.pack(side="left", fill="both", expand=True, padx=(0, 14))

        if CTK_AVAILABLE:
            preview_box = ctk.CTkFrame(
                content_row,
                width=126,
                height=126,
                corner_radius=10,
                fg_color=C["hover"],
                border_width=0,
            )
            preview_bg = C["hover"]
        else:
            preview_box = RoundedFrame(content_row, radius=10, fill=C["hover"], outline=C["hover"], width=126, height=126)
            preview_bg = C["hover"]
        preview_box.pack(side="right", anchor="n", pady=(6, 0))
        preview_box.pack_propagate(False)
        tk.Label(preview_box, text="▧", font=("Microsoft YaHei UI", 34),
                 fg=C["border"], bg=preview_bg).pack(expand=True)

        # 进度信息行
        self._prog_info = tk.Label(
            progress_col,
            text="暂无生成任务。网页端或第三方 API 发起任务后，这里会显示提示词、工作流和进度。",
            font=F["normal"],
            fg=C["text2"],
            bg=C["card"],
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self._prog_info.pack(fill="x", pady=(0, 10))

        self._workflow_info = tk.Label(
            progress_col,
            text="工作流：等待任务",
            font=F["normal"],
            fg=C["text"],
            bg=C["card"],
            anchor="w",
        )
        self._workflow_info.pack(fill="x", pady=(0, 18))

        # 进度条
        bar_row = tk.Frame(progress_col, bg=C["card"])
        bar_row.pack(fill="x", pady=(0, 12))
        self._prog_bar = tk.Canvas(
            bar_row,
            height=26,
            bg=C["card"],
            highlightthickness=0,
        )
        self._prog_bar.pack(fill="x")
        self._prog_bar.bind("<Configure>", lambda _event: self._redraw_progress_bar())
        self._progress_percent = 0
        self._progress_text = "等待任务"

        # 耗时详情
        self._prog_detail = tk.Label(
            progress_col, text="耗时：0s", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w")
        self._prog_detail.pack(fill="x")

        history_head = tk.Frame(progress_col, bg=C["card"])
        history_head.pack(fill="x", pady=(12, 5))
        tk.Label(history_head, text="历史记录", font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._history_hint = tk.Label(
            history_head, text="最近完成的任务", font=F["small"], fg=C["muted"], bg=C["card"])
        self._history_hint.pack(side="right")
        self._history_frame = tk.Frame(progress_col, bg=C["card"])
        self._history_frame.pack(fill="x")
        self._render_task_history()
        self._redraw_progress_bar()

    def _update_task_display(self, task: dict = None):
        """更新任务状态显示"""
        if task and task.get("status") in ("running", "pending", "submitted"):
            self._last_terminal_task_signature = ""
            context_text = self._task_context_text(task)
            self._current_task_text = f"正在生成：{context_text}"
            self._last_completed_outputs = []
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            # 显示进度面板
            self._show_progress_panel()

            wf_id = task.get("workflow_id", task.get("workflow_name", ""))
            phase = str(task.get("progress_label") or task.get("phase") or "").strip()
            prog = self._as_number(task.get("progress"), 0)
            prog_max = max(self._as_number(task.get("progress_max"), 1), 1)
            percent = self._task_progress_percent(task, prog, prog_max)
            elapsed = task.get("elapsed_seconds", task.get("elapsed", 0))

            self._task_status_label.config(
                text=f"正在生成 - {wf_id}", fg=C["primary"])
            self._prog_info.config(text=context_text)
            self._workflow_info.config(text=f"工作流：{wf_id or '默认工作流'}")
            progress_text = phase or "生成中"
            if prog_max > 1:
                progress_text = f"{progress_text} {int(prog)}/{int(prog_max)} · {int(percent)}%"
            else:
                progress_text = f"{progress_text} · {int(percent)}%"
            self._set_progress_bar(percent, progress_text)
            self._prog_detail.config(text=f"耗时：{elapsed}s")

        elif task and task.get("status") == "completed":
            terminal_signature = json.dumps(
                {
                    "status": "completed",
                    "task_id": task.get("task_id") or task.get("id"),
                    "outputs": task.get("outputs") or [],
                    "elapsed": task.get("elapsed_seconds", task.get("elapsed", 0)),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if terminal_signature == self.__dict__.get("_last_terminal_task_signature", ""):
                return
            self._last_terminal_task_signature = terminal_signature
            self._current_task_text = "已完成"
            self._show_progress_panel()
            self._last_completed_outputs = task.get("outputs") or []
            self._last_completed_task_id = str(task.get("task_id") or task.get("id") or "")
            self._task_status_label.config(text="当前任务：已完成", fg=C["success"])
            elapsed = task.get("elapsed_seconds", task.get("elapsed", 0))
            self._prog_info.config(text=self._task_context_text(task))
            self._workflow_info.config(text=f"工作流：{task.get('workflow_id', task.get('workflow_name', '默认工作流'))}")
            self._set_progress_bar(100, "已完成 · 100%")
            self._prog_detail.config(text=f"耗时：{elapsed}s")
            if self._last_completed_outputs and not self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack(side="right", padx=(8, 0), ipadx=8, ipady=3)
            self._remember_task_history(task)
            self.after(15000, self._hide_progress)

        elif task and task.get("status") == "failed":
            self._current_task_text = "失败"
            self._last_completed_outputs = []
            self._last_completed_task_id = ""
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            self._show_progress_panel()
            self._task_status_label.config(text="当前任务：失败", fg=C["error"])
            err = task.get("error", "未知错误")
            self._prog_info.config(text=self._task_context_text(task))
            self._workflow_info.config(text=f"工作流：{task.get('workflow_id', task.get('workflow_name', '默认工作流'))}")
            self._set_progress_bar(0, "失败")
            self._prog_detail.config(text=f"错误：{err}")
            self.after(15000, self._hide_progress)

        elif not task or not task.get("status"):
            self._current_task_text = "无任务"
            self._last_completed_outputs = []
            self._last_completed_task_id = ""
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            self._hide_progress()

    def _hide_progress(self):
        self._task_status_label.config(text="当前任务", fg=C["text"])
        self._prog_info.config(text="暂无生成任务。网页端或第三方 API 发起任务后，这里会显示提示词、工作流和进度。")
        self._workflow_info.config(text="工作流：等待任务")
        self._set_progress_bar(0, "等待任务")
        self._prog_detail.config(text="耗时：0s")

    def _remember_task_history(self, task: dict):
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id or task_id in self._task_history_ids:
            return
        outputs = task.get("outputs") or []
        entry = {
            "task_id": task_id,
            "workflow": str(task.get("workflow_id") or task.get("workflow_name") or "默认工作流"),
            "context": self._task_context_text(task),
            "outputs": outputs,
            "kind": self._task_output_kind(outputs),
            "text": self._task_output_text(outputs),
            "elapsed": task.get("elapsed_seconds", task.get("elapsed", 0)),
        }
        self._task_history_ids.add(task_id)
        self._task_history.insert(0, entry)
        self._task_history = self._task_history[:8]
        self._task_history_ids = {item["task_id"] for item in self._task_history}
        self._render_task_history()

    def _task_output_kind(self, outputs: list) -> str:
        for item in outputs or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            filename = str(item.get("filename") or item.get("file") or item.get("url") or "").lower()
            if item_type in ("image", "video", "text"):
                return item_type
            if filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return "image"
            if filename.endswith((".mp4", ".mov", ".webm", ".avi")):
                return "video"
            if item.get("text"):
                return "text"
        return "text"

    def _task_output_text(self, outputs: list) -> str:
        for item in outputs or []:
            if isinstance(item, dict) and item.get("text"):
                return " ".join(str(item.get("text") or "").replace("\r", " ").replace("\n", " ").split())
        return ""

    def _render_task_history(self):
        if not hasattr(self, "_history_frame"):
            return
        for child in self._history_frame.winfo_children():
            child.destroy()
        if not self._task_history:
            tk.Label(
                self._history_frame,
                text="暂无完成记录",
                font=F["small"],
                fg=C["muted"],
                bg=C["card"],
                anchor="w",
            ).pack(fill="x")
            return
        for entry in self._task_history:
            row = tk.Frame(self._history_frame, bg=C["card"])
            row.pack(fill="x", pady=2)
            kind = entry.get("kind") or "text"
            if kind in ("image", "video"):
                label = f"{'图片' if kind == 'image' else '视频'} · {entry.get('workflow')} · {entry.get('context')}"
                tk.Label(
                    row,
                    text=self._short_middle(label, 42, 18),
                    font=F["small"],
                    fg=C["text2"],
                    bg=C["card"],
                    anchor="w",
                ).pack(side="left", fill="x", expand=True)
                self._button(
                    row,
                    "预览",
                    lambda outputs=list(entry.get("outputs") or []), task_id=entry.get("task_id", ""): self._open_outputs_for_items(outputs, task_id),
                    "plain",
                ).pack(side="right", ipadx=8, ipady=2)
            else:
                snippet = entry.get("text") or entry.get("context") or "文本任务已完成"
                label = f"文本 · {entry.get('workflow')}：{snippet}"
                tk.Label(
                    row,
                    text=self._short_middle(label, 58, 20),
                    font=F["small"],
                    fg=C["text2"],
                    bg=C["card"],
                    anchor="w",
                    justify="left",
                ).pack(fill="x")

    def _show_progress_panel(self):
        if not self._prog_frame.winfo_ismapped():
            self._prog_frame.pack(fill="both", expand=True)

    def _as_number(self, value, default=0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _task_progress_percent(self, task: dict, progress: float, progress_max: float) -> float:
        raw = task.get("progress_percent", task.get("progressPercent", task.get("percent", "")))
        percent = self._as_number(raw, None)
        if percent is None:
            percent = (progress / progress_max) * 100 if progress_max else 0
        elif percent <= 1:
            percent *= 100
        return max(0, min(100, percent))

    def _task_context_text(self, task: dict) -> str:
        title = str(task.get("title") or task.get("scene_title") or task.get("workflow_name") or task.get("workflow_id") or "").strip()
        prompt = str(task.get("prompt_summary") or task.get("prompt") or task.get("promptText") or "").strip()
        prompt = " ".join(prompt.replace("\r", " ").replace("\n", " ").split())
        if title and prompt:
            return self._short_middle(f"{title}：{prompt}", 62, 28)
        if prompt:
            return self._short_middle(prompt, 72, 28)
        return title or "本地生成任务"

    def _set_progress_bar(self, percent: float, text: str):
        self._progress_percent = max(0, min(100, float(percent or 0)))
        self._progress_text = str(text or "")
        self._redraw_progress_bar()

    def _redraw_progress_bar(self):
        if not hasattr(self, "_prog_bar"):
            return
        width = max(1, self._prog_bar.winfo_width())
        height = max(1, self._prog_bar.winfo_height())
        fill_width = int(width * self._progress_percent / 100)
        self._prog_bar.delete("all")
        radius = max(8, height // 2)
        RoundedFrame._round_rect(
            self._prog_bar,
            0,
            0,
            width,
            height,
            radius,
            fill=C["progress_bg"],
            outline=C["border2"],
        )
        if fill_width > 0:
            RoundedFrame._round_rect(
                self._prog_bar,
                0,
                0,
                max(fill_width, radius * 2),
                height,
                radius,
                fill=C["primary"],
                outline="",
            )
        self._prog_bar.create_text(width / 2, height / 2, text=self._progress_text, fill="#ffffff" if fill_width > width * 0.45 else C["text"], font=F["small"])

    # ══════════════════════════════════════════════════════
    # 工作流 + 模型状态面板
    # ══════════════════════════════════════════════════════
    def _build_workflow_model_panel(self):
        parent = getattr(self, "_left_panel_parent", self)
        self._wf_frame = self._card(parent)
        self._wf_frame.pack(fill="both", expand=True)

        # 标题行
        hdr = tk.Frame(self._wf_frame, bg=C["card"], height=30)
        hdr.pack(fill="x", padx=16, pady=(14, 7))
        hdr.pack_propagate(False)

        tk.Label(hdr, text="工作流 / 模型", font=F["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._button(hdr, "上传工作流", self._show_workflow_upload_dialog, "plain", width=72).pack(side="right")
        self._model_hint_label = tk.Label(
            hdr,
            text="",
            font=F["small"],
            fg=C["muted"],
            bg=C["card"],
        )
        self._model_hint_label.pack_forget()

        self._wf_rows = {}
        self._wf_sections = {}

        scroll_host = tk.Frame(self._wf_frame, bg=C["card"])
        scroll_host.pack(fill="both", expand=True, padx=(2, 6), pady=(0, 10))
        self._workflow_canvas = tk.Canvas(
            scroll_host,
            bg=C["card"],
            highlightthickness=0,
            bd=0,
        )
        self._workflow_scrollbar = SlimRoundedScrollbar(
            scroll_host,
            command=self._workflow_canvas.yview,
            width=6,
            bar_width=4,
        )
        self._workflow_scroll_content = tk.Frame(
            self._workflow_canvas,
            bg=C["card"],
        )
        self._workflow_canvas_window = self._workflow_canvas.create_window(
            (0, 0),
            window=self._workflow_scroll_content,
            anchor="nw",
        )
        self._workflow_canvas.configure(
            yscrollcommand=self._workflow_scrollbar.set,
        )
        self._workflow_scrollbar.pack(side="right", fill="y", padx=(4, 1))
        self._workflow_canvas.pack(side="left", fill="both", expand=True)
        self._workflow_scroll_content.bind(
            "<Configure>",
            lambda _event: self._workflow_canvas.configure(
                scrollregion=self._workflow_canvas.bbox("all")
            ),
        )
        self._workflow_canvas.bind(
            "<Configure>",
            lambda event: self._workflow_canvas.itemconfigure(
                self._workflow_canvas_window,
                width=event.width,
            ),
        )

        def section(title):
            box = tk.Frame(self._workflow_scroll_content, bg=C["card"])
            box.pack(fill="x", padx=(14, 3), pady=(7, 4))
            head = tk.Frame(box, bg=C["card"])
            head.pack(fill="x", pady=(0, 3))
            tk.Label(head, text="⌄", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left", padx=(0, 6))
            tk.Label(head, text=title, font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
            return box

        self._wf_sections = {
            "image": section("图片模型"),
            "video": section("视频模型"),
            "text": section("文字模型"),
        }
        self._update_model_display()

    def _on_workflow_mousewheel(self, event):
        canvas = getattr(self, "_workflow_canvas", None)
        if canvas is None:
            return None
        # Tk uses "??" for event fields that do not apply on this platform.
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        try:
            button = int(getattr(event, "num", 0) or 0)
        except (TypeError, ValueError):
            button = 0
        if delta:
            units = max(1, abs(delta) // 120)
            direction = -1 if delta > 0 else 1
        elif button in (4, 5):
            units = 1
            direction = -1 if button == 4 else 1
        else:
            return None
        canvas.yview_scroll(direction * units, "units")
        return "break"

    def _bind_workflow_mousewheel_tree(self, widget):
        try:
            if not getattr(widget, "_lingjing_workflow_scroll_bound", False):
                widget.bind(
                    "<MouseWheel>",
                    self._on_workflow_mousewheel,
                    add="+",
                )
                widget.bind(
                    "<Button-4>",
                    self._on_workflow_mousewheel,
                    add="+",
                )
                widget.bind(
                    "<Button-5>",
                    self._on_workflow_mousewheel,
                    add="+",
                )
                widget._lingjing_workflow_scroll_bound = True
            children = widget.winfo_children()
        except Exception:
            children = []
        for child in children:
            self._bind_workflow_mousewheel_tree(child)

    def _update_model_display(self):
        """更新工作流面板中的模型状态。"""
        self._update_workflow_display(self._last_health or {})

    def _workflow_group_for_display(self, workflow: dict) -> str:
        output_type = str(workflow.get("output_type") or "").strip().lower()
        declared_type = str(
            workflow.get("workflow_type") or workflow.get("type") or ""
        ).strip().lower()
        if output_type in {"image", "video", "text"}:
            return output_type
        if declared_type.startswith("video."):
            return "video"
        if declared_type.startswith("image."):
            return "image"
        if declared_type.startswith("text."):
            return "text"
        text = " ".join([
            declared_type,
            str(workflow.get("id") or ""),
            str(workflow.get("name") or ""),
        ]).lower()
        if "video" in text or "flf2v" in text:
            return "video"
        if "chat" in text or "llm" in text or "文字模型" in text:
            return "text"
        return "image"

    def _workflow_model_key(self, workflow: dict) -> str:
        declared = str(
            workflow.get("model_group") or workflow.get("modelGroup") or ""
        ).strip()
        if declared:
            return declared
        text = " ".join([
            str(workflow.get("id") or ""),
            str(workflow.get("name") or ""),
            str(workflow.get("type") or ""),
            str(workflow.get("output_type") or ""),
        ]).lower()
        if "wan" in text or "wan2.1" in text:
            return "Wan2.1"
        if "flux" in text:
            return "Flux2"
        if "qwen" in text or "千问" in text:
            return "Qwen3.5"
        return ""

    def _workflow_capability_meta(self, workflow: dict) -> tuple[str, str, str]:
        capability = str(workflow.get("capability") or "").strip().lower()
        declared_type = str(
            workflow.get("workflow_type") or workflow.get("type") or ""
        ).strip().lower()
        if not capability:
            if declared_type in {"image.text_image_to_image", "image.image_to_image", "image_edit"}:
                capability = "text_image_to_image"
            elif "first_last" in declared_type or "flf2v" in declared_type:
                capability = "first_last_frame"
            elif declared_type.startswith("text") or "chat" in declared_type:
                capability = "text_model"
            else:
                capability = "text_to_image"
        styles = {
            "text_image_to_image": ("文/图生图", "#7c3aed"),
            "image_to_image": ("图生图", "#7c3aed"),
            "text_to_image": ("文生图", "#2563eb"),
            "first_last_frame": ("首尾帧", "#e8790c"),
            "text_model": ("文本模型", "#0f9f84"),
        }
        label, color = styles.get(capability, ("自定义", C["text2"]))
        return capability, label, color

    def _workflow_display_name(self, workflow: dict) -> str:
        return str(workflow.get("name") or workflow.get("label") or workflow.get("id") or "未命名工作流").strip()

    def _workflow_status_text(self, workflow: dict, available: bool) -> str:
        if not workflow.get("enabled", True):
            return "已停用"
        if available:
            return "可用"
        key = self._workflow_model_key(workflow)
        missing_count = len(self._missing_model_items(key)) if key else 0
        reported_missing_models = sum(
            len(workflow.get(field) or [])
            for field in ("missing_models", "missingModels")
            if isinstance(workflow.get(field), (list, tuple, set))
        )
        reported_missing_nodes = sum(
            len(workflow.get(field) or [])
            for field in ("missing_nodes", "missingNodes")
            if isinstance(workflow.get(field), (list, tuple, set))
        )
        if missing_count or reported_missing_models:
            return f"缺少 {missing_count or reported_missing_models} 个模型"
        if reported_missing_nodes:
            return f"缺少 {reported_missing_nodes} 个节点"
        dependency_status = str(
            workflow.get("dependency_status") or workflow.get("validation_status") or ""
        ).lower()
        if dependency_status in {"", "unknown", "unchecked", "unverified"} or workflow.get("nodes_verified") is False:
            return "加载中"
        return "需要检查"

    def _add_workflow_row(self, parent_box, workflow: dict, group: str):
        wf_id = str(workflow.get("id") or "").strip()
        title = self._workflow_display_name(workflow)
        _capability, capability_label, capability_color = self._workflow_capability_meta(workflow)
        available = workflow.get("enabled", True) and self._workflow_model_available(workflow)
        model_key = self._workflow_model_key(workflow)
        status_text = self._workflow_status_text(workflow, available)
        loading = status_text == "加载中"
        status_color = C["success"] if available else C["warn"] if loading else C["error"]

        row = tk.Frame(parent_box, bg=C["card"])
        row.pack(fill="x", pady=4)
        row.columnconfigure(0, weight=0)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=0)

        tk.Label(
            row,
            text=f"■ {capability_label}",
            font=F["tiny"],
            fg=capability_color,
            bg=C["card"],
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))

        if CTK_AVAILABLE:
            radio = ctk.CTkRadioButton(
                row,
                text=title,
                value=wf_id or "default",
                variable=self._workflow_mode,
                font=F["normal"],
                text_color=C["primary"] if available else C["muted"],
                fg_color=C["primary"],
                hover_color=C["primary2"],
                border_color=C["border"],
                state="normal" if available else "disabled",
                radiobutton_width=16,
                radiobutton_height=16,
                width=92,
            )
        else:
            radio = tk.Radiobutton(
                row,
                text=title,
                value=wf_id or "default",
                variable=self._workflow_mode,
                font=F["normal"],
                fg=C["primary"] if available else C["muted"],
                bg=C["card"],
                activeforeground=C["primary"],
                activebackground=C["card"],
                selectcolor=C["surface"],
                relief="flat",
                bd=0,
                cursor="hand2" if available else "arrow",
                disabledforeground=C["muted"],
                state="normal" if available else "disabled",
            )
        radio.grid(row=0, column=1, columnspan=2, sticky="w")

        status_box = tk.Frame(row, bg=C["card"])
        status_box.grid(row=1, column=1, sticky="w", pady=(2, 0))
        dot = tk.Canvas(status_box, width=9, height=9, bg=C["card"], highlightthickness=0)
        dot.pack(side="left", padx=(2, 5), pady=(2, 0))
        dot.create_oval(1, 1, 8, 8, fill=status_color, outline="")

        status_lbl = tk.Label(
            status_box,
            text=status_text,
            font=F["small"],
            fg=status_color,
            bg=C["card"],
            anchor="w",
            justify="left",
            wraplength=150,
        )
        status_lbl.pack(side="left")

        params_btn = self._button(
            row,
            "详情",
            lambda wf=dict(workflow): self._show_workflow_schema(wf),
            "plain",
            width=70,
        )
        params_btn.grid(row=1, column=2, padx=(4, 0), sticky="e")

        if wf_id:
            self._wf_rows[wf_id] = row

    def _mirror_model_url(self, url: str) -> str:
        sources = self._model_download_sources(url)
        return sources[0][1] if sources else ""

    @staticmethod
    def _model_download_sources(url: str) -> list[tuple[str, str]]:
        """Return the preferred domestic source plus the official fallback."""
        official = str(url or "").strip()
        if not official:
            return []
        sources: list[tuple[str, str]] = []
        if official.startswith("https://huggingface.co/"):
            sources.append((
                "国内镜像",
                official.replace("https://huggingface.co/", "https://hf-mirror.com/", 1),
            ))
        sources.append(("官方源", official))
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, source_url in sources:
            if source_url and source_url not in seen:
                seen.add(source_url)
                unique.append((label, source_url))
        return unique

    def _missing_model_items(self, model_key: str) -> list:
        spec = MODEL_REQUIREMENTS.get(model_key, {})
        items = []
        models_dir = _models_dir()
        for item in spec.get("items", []):
            rel_path = item.get("path", "")
            if rel_path and not _model_file_ready(models_dir / rel_path, item.get("size_bytes")):
                items.append(item)
        return items

    @staticmethod
    def _format_model_size(size_bytes: int) -> str:
        size = max(0, int(size_bytes or 0))
        if size >= 1_000_000_000:
            return f"{size / 1_000_000_000:.1f} GB"
        if size >= 1_000_000:
            return f"{size / 1_000_000:.1f} MB"
        return f"{size} B"

    def _workflow_performance_for_display(self, workflow: dict) -> dict:
        model_key = self._workflow_model_key(workflow)
        spec = MODEL_REQUIREMENTS.get(model_key, {})
        performance = spec.get("performance")
        if not isinstance(performance, dict):
            performance = {}
        total_size = sum(
            int(item.get("size_bytes") or 0)
            for item in spec.get("items", [])
            if isinstance(item, dict)
        )
        return {
            "model_key": model_key,
            "title": str(spec.get("title") or model_key or "未登记模型"),
            "performance": performance,
            "model_size_bytes": total_size,
        }

    def _format_model_performance_text(self, model_key: str) -> str:
        info = self._workflow_performance_for_display({"model_group": model_key})
        performance = info["performance"]
        if not performance:
            return "该模型尚未登记硬件要求；下载前请先确认显卡与磁盘空间。"
        return "\n".join([
            f"负载等级：{performance.get('level') or '未标注'}    模型文件：{self._format_model_size(info['model_size_bytes'])}",
            f"最低配置：{performance.get('minimum') or '未标注'}",
            f"推荐配置：{performance.get('recommended') or '未标注'}",
            f"推荐参数：{performance.get('preset') or '按工作流默认值'}",
            f"提示：{performance.get('notes') or '实际速度受分辨率和帧数影响。'}",
        ])

    def _format_workflow_detail_text(self, workflow: dict) -> str:
        _capability, capability_label, _color = self._workflow_capability_meta(workflow)
        info = self._workflow_performance_for_display(workflow)
        description = str(workflow.get("description") or "暂无简介").strip()
        lines = [
            "工作流简介：",
            f"  {description}",
            "",
            f"能力：{capability_label}",
            f"模型：{info['title']}",
            self._format_model_performance_text(info["model_key"]),
            "",
            "调用说明（高级）：",
            self._format_workflow_schema_text(workflow),
        ]
        return "\n".join(lines)

    def _show_workflow_model_help(self, workflow: dict):
        self._footer_label.config(text="  正在识别模型下载地址与存放位置...")

        def worker():
            try:
                from app.core.workflow_model_sources import workflow_model_items
                items = workflow_model_items(workflow, BASE_DIR)
                if not items:
                    key = self._workflow_model_key(workflow)
                    items = list(MODEL_REQUIREMENTS.get(key, {}).get("items", []))
                if not items:
                    raise ValueError("工作流未声明可识别的模型文件，请在配置中补充模型依赖。")
                self.after(0, lambda: self._show_model_install_help(
                    self._workflow_model_key(workflow), items=items,
                    title_override=str(workflow.get("name") or workflow.get("id"))))
                self.after(0, lambda: self._footer_label.config(text="  模型位置已识别；可下载缺失文件或选择本地模型"))
            except Exception as exc:
                self.after(0, lambda message=str(exc): messagebox.showerror("模型识别失败", message))
        threading.Thread(target=worker, daemon=True).start()

    def _show_model_install_help(self, model_key: str, *, items=None, title_override=None):
        missing = self._missing_model_items(model_key) if items is None else items
        popup_w = 840
        popup_h = 600 if len(missing) >= 4 else 520
        popup = tk.Toplevel(self)
        popup.title("下载模型")
        popup.geometry(f"{popup_w}x{popup_h}")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, popup_w, popup_h)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        title = title_override or MODEL_REQUIREMENTS.get(model_key, {}).get("title", model_key)
        tk.Label(panel, text=f"{title}：下载模型", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(panel, text="选择需要的模型文件。客户端优先使用国内镜像，连接失败时会自动切换到官方源。",
                 font=F["normal"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=18, pady=(0, 10))
        tk.Label(
            panel,
            text=self._format_model_performance_text(model_key),
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
            justify="left",
            anchor="w",
            wraplength=770,
        ).pack(fill="x", padx=18, pady=(0, 12))

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(side="bottom", fill="x", padx=18, pady=(4, 14))

        list_outer = tk.Frame(panel, bg=C["card"])
        list_outer.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        canvas = tk.Canvas(list_outer, bg=C["card"], highlightthickness=0, bd=0)
        scrollbar = SlimRoundedScrollbar(
            list_outer,
            command=canvas.yview,
            width=6,
            bar_width=4,
        )
        rows = tk.Frame(canvas, bg=C["card"])
        rows.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(4, 0))
        canvas.bind("<Configure>", lambda event: canvas.itemconfig(canvas_window, width=event.width))

        download_controls = []
        selection_session = {"scan_disabled": False, "controls": download_controls}
        if not missing:
            empty = self._card(rows)
            empty.pack(fill="x", pady=(0, 10))
            tk.Label(empty, text="模型文件已完整，无需下载。", font=F["body"], fg=C["success"], bg=C["card"]).pack(anchor="w", padx=16, pady=18)
        for index, item in enumerate(missing, 1):
            control = self._build_model_download_row(rows, model_key, item, index)
            control["selection_session"] = selection_session
            download_controls.append(control)

        def close_popup():
            owners = [self._model_download_owner(control) for control in download_controls]
            if any(
                owner.get("state") in {"downloading", "resuming", "paused", "cancelling"}
                for owner in owners
            ):
                self._footer_label.config(
                    text="  模型正在后台下载；再次打开下载页面可查看进度或取消"
                )
            self._detach_model_download_views(download_controls)
            try:
                popup.grab_release()
            except Exception:
                pass
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        def download_all():
            for control in download_controls:
                if control.get("state") != "done":
                    self._start_model_download(control)

        if download_controls:
            self._button(actions, "下载全部模型", download_all, "primary", width=118).pack(side="left", ipadx=12, ipady=6)
        self._button(actions, "打开模型文件夹", self._open_models, "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "关闭", close_popup, "plain").pack(side="right", ipadx=12, ipady=6)
        self._bind_model_download_mousewheel_tree(popup, canvas)

    def _map_local_model(self, control: dict):
        owner = self._model_download_owner(control)
        if owner.get("state") in {"downloading", "resuming", "paused", "cancelling"}:
            messagebox.showinfo("模型正在下载", "请先取消此文件的下载，再选择本地模型。")
            return
        item = control["item"]
        selected = filedialog.askopenfilename(
            title=f"选择 {Path(item['path']).name}",
            filetypes=[("模型文件", "*.safetensors *.gguf *.ckpt *.pt *.pth *.bin"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            self._apply_local_model_mapping(control, selected)
            additional = self._offer_related_model_mappings(control, selected)
            _ensure_extra_model_paths()
            self._start_background_model_recheck()
            self._footer_label.config(text=f"  已映射 {1 + additional} 个本地模型；请重启后台生效，原文件未复制或移动")
        except (OSError, ValueError) as exc:
            messagebox.showerror("映射失败", str(exc))

    def _apply_local_model_mapping(self, control, selected):
        from app.core.model_mappings import register_mapping
        from app.core.workflow_dependencies import clear_model_index_cache
        owner = self._model_download_owner(control)
        if owner.get("state") in {"downloading", "resuming", "paused", "cancelling"}:
            raise ValueError("文件正在下载，请先取消下载再关联本地模型")
        item = control["item"]
        register_mapping(_models_dir(), item["path"], selected, item.get("size_bytes"))
        clear_model_index_cache()
        owner.update(state="done", progress_percent=100.0, status_text="已映射本地模型；重启后台后生效")
        for view in owner.get("views", [control]):
            if view.get("path_label") is not None and self._model_download_view_exists(view):
                view["path_label"].configure(text=self._short_middle(str(Path(selected).parent), 22, 18))
        self._refresh_model_download_views(owner)

    def _offer_related_model_mappings(self, control, selected):
        session = control.get("selection_session")
        if not session or session["scan_disabled"]:
            return 0
        from app.core.model_mappings import find_related_models
        controls = {c["item"]["path"]: c for c in session["controls"]
                    if self._model_download_owner(c).get("state") not in {"done", "downloading", "resuming", "paused", "cancelling"}}
        matches = find_related_models(selected, [c["item"] for c in controls.values()], _models_dir())
        if not matches:
            return 0
        details = "\n\n".join(f"{match['item']['path']}\n← {match['source']}" for match in matches)
        use = messagebox.askyesno(
            "发现其他本地模型",
            f"在当前目录和上一级目录发现 {len(matches)} 个同名模型：\n\n{details}\n\n是否一起关联？不会复制或移动原文件。\n选择“否”后，本次工作流配置不再扫描或提示。",
        )
        if not use:
            session["scan_disabled"] = True
            return 0
        applied = 0
        for match in matches:
            try:
                self._apply_local_model_mapping(controls[match["item"]["path"]], match["source"])
                applied += 1
            except (OSError, ValueError) as exc:
                messagebox.showerror("部分模型未能关联", f"{match['item']['path']}：{exc}\n已成功关联的文件会保留。")
        return applied

    def _build_model_download_row(self, parent, model_key: str, item: dict, index: int) -> dict:
        rel_path = str(item.get("path") or "").strip()
        sources = self._model_download_sources(item.get("url", ""))
        url = sources[0][1] if sources else ""
        target = _models_dir() / rel_path
        filename = Path(rel_path).name or f"model_{index}"
        from app.core.model_mappings import resolve_model_file
        display_path = self._short_middle(str(resolve_model_file(target).parent), 22, 18)

        card = self._card(parent)
        card.pack(fill="x", pady=(0, 8))
        card.grid_columnconfigure(1, weight=3)
        card.grid_columnconfigure(2, weight=2)

        tk.Label(card, text=f"{index}", font=F["bold"], fg=C["primary"], bg=C["card"], width=3).grid(row=0, column=0, rowspan=4, padx=(12, 6), pady=10, sticky="n")
        tk.Label(card, text="模型文件", font=F["small"], fg=C["text2"], bg=C["card"]).grid(row=0, column=1, sticky="w", pady=(9, 0))
        tk.Label(card, text=filename, font=F["bold"], fg=C["text"], bg=C["card"], anchor="w").grid(row=1, column=1, sticky="ew", pady=(1, 0))

        url_row = tk.Frame(card, bg=C["card"])
        url_row.grid(row=2, column=1, sticky="ew", pady=(7, 9))
        for source_index, (source_name, source_url) in enumerate(sources):
            source_row = tk.Frame(url_row, bg=C["card"])
            source_row.pack(fill="x", pady=(0 if source_index == 0 else 3, 0))
            tk.Label(source_row, text=source_name, width=7, anchor="w", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left")
            url_label = tk.Label(
                source_row,
                text=self._short_middle(source_url, 34, 16),
                font=F["url"],
                fg=C["primary"],
                bg=C["card"],
                anchor="w",
                cursor="hand2",
            )
            url_label.pack(side="left", padx=(6, 0), fill="x", expand=True)
            url_label.bind("<Button-1>", lambda _event, u=source_url: webbrowser.open(u))

        right = tk.Frame(card, bg=C["card"])
        right.grid(row=0, column=2, rowspan=3, sticky="nsew", padx=(12, 12), pady=9)
        top_line = tk.Frame(right, bg=C["card"])
        top_line.pack(fill="x")
        tk.Label(top_line, text="存放位置", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left")
        path_label = tk.Label(top_line, text=display_path, font=F["small"], fg=C["text"], bg=C["card"], anchor="e")
        path_label.pack(side="right", fill="x", expand=True)

        progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(right, orient="horizontal", mode="determinate", maximum=100, variable=progress_var, style="Progress.Horizontal.TProgressbar")
        progress.pack(fill="x", pady=(8, 4))
        status_var = tk.StringVar(value="等待下载")
        action_line = tk.Frame(card, bg=C["card"])
        action_line.grid(
            row=3,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(0, 12),
            pady=(0, 9),
        )
        status = tk.Label(
            action_line,
            textvariable=status_var,
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
            anchor="w",
        )
        status.pack(side="left", fill="x", expand=True)

        control = {
            "model_key": model_key,
            "item": item,
            "url": url,
            "urls": [source_url for _source_name, source_url in sources],
            "target": target,
            "path_label": path_label,
            "card": card,
            "progress_var": progress_var,
            "status_var": status_var,
            "progress_percent": 0.0,
            "status_text": "等待下载",
            "state": "idle",
            "pause_event": threading.Event(),
            "stop_event": threading.Event(),
            "worker": None,
        }
        control["views"] = [control]
        button_row = tk.Frame(action_line, bg=C["card"])
        button_row.pack(side="right", padx=(10, 0))
        pause_button = self._button(button_row, "暂停", lambda c=control: self._pause_model_download(c), "plain", width=56)
        pause_button.pack(side="left", padx=(0, 6))
        pause_button.configure(state="disabled")
        cancel_button = self._button(button_row, "取消", lambda c=control: self._cancel_model_download(c), "plain", width=56)
        cancel_button.pack(side="left", padx=(0, 6))
        cancel_button.configure(state="disabled")
        button = self._button(button_row, "下载", lambda c=control: self._start_model_download(c), "primary", width=72)
        if not url:
            button.configure(state="disabled", text="无地址")
        button.pack(side="left")
        control["button"] = button
        control["pause_button"] = pause_button
        control["cancel_button"] = cancel_button
        self._button(button_row, "选择本地模型", lambda c=control: self._map_local_model(c), "plain", width=104).pack(side="left", padx=(6, 0))
        if _model_file_ready(target, item.get("size_bytes")):
            control.update(state="done", status_text="本地模型已就绪", progress_percent=100.0)
            self._refresh_model_download_views(control)
        elif not url:
            control["status_text"] = "未找到下载来源，可选择本地模型"
            status_var.set(control["status_text"])
        owner = self._model_transfer_for(target)
        if owner is not None and owner is not control:
            control["owner"] = owner
            owner.setdefault("views", []).append(control)
            self._refresh_model_download_views(owner)
        else:
            self._refresh_model_download_views(control)
        return control

    def _bind_model_download_mousewheel_tree(self, widget, canvas):
        def on_mousewheel(event):
            try:
                delta = int(getattr(event, "delta", 0) or 0)
            except (TypeError, ValueError):
                delta = 0
            try:
                button = int(getattr(event, "num", 0) or 0)
            except (TypeError, ValueError):
                button = 0
            if delta:
                units = max(1, abs(delta) // 120)
                direction = -1 if delta > 0 else 1
            elif button in (4, 5):
                units = 1
                direction = -1 if button == 4 else 1
            else:
                return None
            canvas.yview_scroll(direction * units, "units")
            return "break"

        def bind_tree(item):
            try:
                item.bind("<MouseWheel>", on_mousewheel, add="+")
                item.bind("<Button-4>", on_mousewheel, add="+")
                item.bind("<Button-5>", on_mousewheel, add="+")
                children = item.winfo_children()
            except Exception:
                children = []
            for child in children:
                bind_tree(child)

        bind_tree(canvas)
        bind_tree(widget)

    def _model_download_owner(self, control: dict) -> dict:
        owner = control.get("owner") if isinstance(control, dict) else None
        return owner if isinstance(owner, dict) else control

    def _model_transfer_for(self, target: Path):
        lock, transfers = self._model_transfer_state()
        with lock:
            return transfers.get(self._model_transfer_key(target))

    def _model_download_view_exists(self, view: dict) -> bool:
        card = view.get("card")
        if card is None:
            return True
        try:
            return bool(card.winfo_exists())
        except Exception:
            return False

    def _refresh_model_download_views(self, control: dict):
        task = self._model_download_owner(control)
        state = str(task.get("state") or "idle")
        status_text = str(task.get("status_text") or "等待下载")
        progress_percent = float(task.get("progress_percent") or 0.0)
        url_available = bool(str(task.get("url") or "").strip())
        action_state = {
            "idle": ("normal" if url_available else "disabled", "下载" if url_available else "无地址", "disabled", "暂停", "disabled"),
            "downloading": ("disabled", "下载中", "normal", "暂停", "normal"),
            "paused": ("normal", "继续", "disabled", "暂停", "normal"),
            "resuming": ("disabled", "恢复中", "disabled", "暂停", "normal"),
            "cancelling": ("disabled", "取消中", "disabled", "暂停", "disabled"),
            "abandoning": ("disabled", "结束中", "disabled", "暂停", "disabled"),
            "done": ("disabled", "已完成", "disabled", "暂停", "disabled"),
            "failed": ("normal" if url_available else "disabled", "重试", "disabled", "暂停", "disabled"),
            "cancelled": ("normal" if url_available else "disabled", "重新下载", "disabled", "暂停", "disabled"),
        }.get(state, ("disabled", "处理中", "disabled", "暂停", "disabled"))
        main_state, main_text, pause_state, pause_text, cancel_state = action_state
        active_views = []
        for view in list(task.setdefault("views", [task])):
            if not self._model_download_view_exists(view):
                continue
            active_views.append(view)
            try:
                view["status_var"].set(status_text)
                view["progress_var"].set(progress_percent)
            except Exception:
                pass
            try:
                view["button"].configure(state=main_state, text=main_text)
            except Exception:
                pass
            try:
                view["pause_button"].configure(state=pause_state, text=pause_text)
            except Exception:
                pass
            try:
                view["cancel_button"].configure(state=cancel_state, text="取消")
            except Exception:
                pass
        task["views"] = active_views

    def _set_model_download_status(self, control: dict, text: str):
        task = self._model_download_owner(control)
        task["status_text"] = str(text)
        self._refresh_model_download_views(task)

    def _detach_model_download_views(self, controls):
        for view in controls:
            task = self._model_download_owner(view)
            task["views"] = [item for item in task.get("views", []) if item is not view]

    def _model_transfer_key(self, target: Path) -> str:
        try:
            return os.path.normcase(str(Path(target).resolve()))
        except OSError:
            return os.path.normcase(str(Path(target).absolute()))

    def _model_transfer_state(self):
        lock = self.__dict__.get("_model_transfer_lock")
        if lock is None:
            lock = threading.RLock()
            self.__dict__["_model_transfer_lock"] = lock
        transfers = self.__dict__.setdefault("_active_model_transfers", {})
        return lock, transfers

    def _model_transfer_active(self, target: Path) -> bool:
        lock, transfers = self._model_transfer_state()
        with lock:
            return self._model_transfer_key(target) in transfers

    def _claim_model_transfer(self, control: dict) -> bool:
        control = self._model_download_owner(control)
        if self._runtime_maintenance_active():
            return False
        lock, transfers = self._model_transfer_state()
        key = self._model_transfer_key(control["target"])
        with lock:
            if (
                self._runtime_maintenance_active()
                or self.__dict__.get("_model_import_in_progress", False)
            ):
                return False
            owner = transfers.get(key)
            if owner is not None and owner is not control:
                return False
            transfers[key] = control
            return True

    def _release_model_transfer(self, control: dict):
        control = self._model_download_owner(control)
        target = control.get("target")
        if target is None:
            return
        lock, transfers = self._model_transfer_state()
        key = self._model_transfer_key(target)
        with lock:
            if transfers.get(key) is control:
                transfers.pop(key, None)

    def _abandon_paused_model_download(self, control: dict, on_done=None):
        """Release a paused target only after its writer thread has exited."""
        control = self._model_download_owner(control)
        if control.get("state") != "paused":
            if control.get("state") != "abandoning" and on_done:
                on_done()
            return
        control["state"] = "abandoning"
        self._set_model_download_status(control, "正在安全结束暂停任务...")
        worker = control.get("worker")

        def wait_for_writer():
            try:
                if worker and worker.is_alive():
                    worker.join()
            finally:
                def finish_abandon():
                    self._release_model_transfer(control)
                    if control.get("state") == "abandoning":
                        control["state"] = "abandoned"
                    if on_done:
                        on_done()

                if self._shutting_down:
                    self._release_model_transfer(control)
                else:
                    self.after(0, finish_abandon)

        threading.Thread(target=wait_for_writer, daemon=True).start()

    def _has_active_model_transfers(self) -> bool:
        lock, transfers = self._model_transfer_state()
        with lock:
            return bool(transfers)

    def _start_model_download(self, control: dict):
        control = self._model_download_owner(control)
        if control.get("state") in {"downloading", "resuming", "cancelling", "abandoning"}:
            return
        if not self._claim_model_transfer(control):
            self._set_model_download_status(control, "另一个模型维护任务正在运行，请稍候")
            return
        if control.get("state") == "paused":
            control["state"] = "resuming"
            self._set_model_download_status(control, "正在安全恢复下载...")
            previous_worker = control.get("worker")

            def wait_then_resume():
                if previous_worker and previous_worker.is_alive():
                    previous_worker.join()
                if not self._shutting_down:
                    self.after(0, lambda: self._resume_model_download(control))

            threading.Thread(target=wait_then_resume, daemon=True).start()
            return
        previous_state = control.get("state")
        control["state"] = "downloading"
        if control.get("progress_percent") is None or previous_state == "cancelled":
            control["progress_percent"] = 0.0
        pause_event = control.get("pause_event")
        stop_event = control.get("stop_event")
        if pause_event:
            pause_event.clear()
        if stop_event:
            stop_event.clear()
        self._set_model_download_status(control, "准备下载...")
        worker = threading.Thread(target=lambda: self._download_model_file(control), daemon=True)
        control["worker"] = worker
        worker.start()

    def _resume_model_download(self, control: dict):
        """Start a fresh worker only after the paused worker has fully exited."""
        control = self._model_download_owner(control)
        if self._shutting_down or control.get("state") != "resuming":
            return
        pause_event = control.get("pause_event")
        if pause_event:
            pause_event.clear()
        control["state"] = "downloading"
        self._set_model_download_status(control, "继续下载...")
        worker = threading.Thread(target=lambda: self._download_model_file(control), daemon=True)
        control["worker"] = worker
        worker.start()

    def _pause_model_download(self, control: dict):
        control = self._model_download_owner(control)
        if control.get("state") != "downloading":
            return
        pause_event = control.get("pause_event")
        if pause_event:
            pause_event.set()
        control["state"] = "paused"
        self._set_model_download_status(control, "已暂停，点击继续可断点续传")

    def _cancel_model_download(self, control: dict):
        control = self._model_download_owner(control)
        if control.get("state") not in {"downloading", "paused", "resuming"}:
            return
        control["state"] = "cancelling"
        stop_event = control.setdefault("stop_event", threading.Event())
        stop_event.set()
        pause_event = control.get("pause_event")
        if pause_event:
            pause_event.set()
        self._set_model_download_status(control, "正在取消并清理未完成文件...")
        worker = control.get("worker")

        def wait_for_writer():
            if worker and worker.is_alive():
                worker.join()
            if getattr(self, "_shutting_down", False):
                self._complete_model_download_cancel(control)
            else:
                self.after(0, lambda: self._complete_model_download_cancel(control))

        threading.Thread(target=wait_for_writer, daemon=True).start()

    def _complete_model_download_cancel(self, control: dict):
        control = self._model_download_owner(control)
        if control.get("state") == "done":
            return
        target = Path(control["target"])
        part_path = target.with_suffix(target.suffix + ".part")
        meta_path = part_path.with_suffix(part_path.suffix + ".json")
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        self._release_model_transfer(control)
        control["state"] = "cancelled"
        control["worker"] = None
        control["progress_percent"] = 0.0
        self._set_model_download_status(control, "已取消，未完成文件已删除")
        try:
            self._footer_label.config(text="  模型下载已取消，未完成文件已清理")
        except Exception:
            pass

    def _download_model_file(self, control: dict):
        control = self._model_download_owner(control)
        import urllib.request as ur
        urls = [str(item).strip() for item in control.get("urls", []) if str(item).strip()]
        if not urls and control.get("url"):
            urls = [str(control["url"])]
        if not urls:
            self.after(0, lambda: self._fail_model_download(control, "没有可用的下载地址"))
            return
        source_index = 0
        url = urls[source_index]
        control["url"] = url
        target: Path = control["target"]
        part_path = target.with_suffix(target.suffix + ".part")
        meta_path = part_path.with_suffix(part_path.suffix + ".json")
        expected_size = int((control.get("item") or {}).get("size_bytes") or 0)
        expected_sha256 = str((control.get("item") or {}).get("sha256") or "").strip()
        max_retries = 3

        if expected_size <= 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            self.after(
                0,
                lambda: self._fail_model_download(
                    control,
                    "模型下载清单缺少有效的文件大小或 SHA256，已停止下载",
                ),
            )
            return

        def cancelled() -> bool:
            stop_event = control.get("stop_event")
            return bool(stop_event and stop_event.is_set())

        def discard_partial():
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        def load_resume_metadata() -> dict:
            if not part_path.is_file() or not meta_path.is_file():
                return {}
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            if not isinstance(data, dict):
                return {}
            try:
                valid = (
                    str(data.get("url") or "") == url
                    and int(data.get("expected_size") or 0) == expected_size
                    and str(data.get("expected_sha256") or "") == expected_sha256
                    and bool(str(data.get("validator") or "").strip())
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                return {}
            return data

        def response_validator(headers) -> str:
            etag = str(headers.get("ETag") or "").strip()
            if etag and not etag.startswith("W/"):
                return etag
            return str(headers.get("Last-Modified") or "").strip()

        def save_resume_metadata(validator: str):
            if not validator:
                meta_path.unlink(missing_ok=True)
                return
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "expected_size": expected_size,
                        "expected_sha256": expected_sha256,
                        "validator": validator,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        try:
            if cancelled():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            resume_metadata = load_resume_metadata()
            if part_path.exists() and not resume_metadata:
                discard_partial()
            if expected_size and resume_metadata and model_file_ready(part_path, expected_size):
                self.after(0, lambda: self._set_model_download_status(control, "下载完成，正在校验 SHA256..."))
                if not expected_sha256 or model_file_sha256_matches(part_path, expected_sha256):
                    if cancelled():
                        return
                    os.replace(part_path, target)
                    meta_path.unlink(missing_ok=True)
                    self.after(0, lambda: self._finish_model_download(control))
                    return
                discard_partial()
            if expected_size and part_path.exists() and part_path.stat().st_size > expected_size:
                discard_partial()
            attempt = 0
            while True:
                if cancelled():
                    return
                pause_event = control.get("pause_event")
                if pause_event and pause_event.is_set():
                    self.after(0, lambda: self._set_model_download_status(control, "已暂停，点击继续可断点续传"))
                    return

                resume_metadata = load_resume_metadata()
                if part_path.exists() and not resume_metadata:
                    discard_partial()
                resume_from = part_path.stat().st_size if part_path.exists() else 0
                headers = {"User-Agent": "lingjing-model-downloader/1.0"}
                if resume_from:
                    headers["Range"] = f"bytes={resume_from}-"
                    headers["If-Range"] = str(resume_metadata["validator"])
                req = ur.Request(url, headers=headers)
                try:
                    with _open_download_request(
                        req,
                        timeout=30,
                        allowed_suffixes=MODEL_DOWNLOAD_REDIRECT_SUFFIXES,
                    ) as resp:
                        _validate_download_response_url(
                            url,
                            resp,
                            allowed_suffixes=MODEL_DOWNLOAD_REDIRECT_SUFFIXES,
                        )
                        if cancelled():
                            return
                        status_code = int(getattr(resp, "status", 200) or 200)
                        validator = response_validator(resp.headers)
                        if resume_from and status_code == 200:
                            discard_partial()
                            resume_from = 0
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if status_code == 206:
                            content_range = str(resp.headers.get("Content-Range") or "").strip()
                            match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
                            if not resume_from or not match:
                                discard_partial()
                                raise IOError("服务器返回了无效的断点续传范围")
                            start, end = int(match.group(1)), int(match.group(2))
                            total_value = match.group(3)
                            if start != resume_from or end < start:
                                discard_partial()
                                raise IOError("服务器返回的断点位置与本地文件不一致")
                            if content_length and content_length != end - start + 1:
                                discard_partial()
                                raise IOError("服务器返回的分段长度无效")
                            if expected_size and (
                                total_value == "*" or int(total_value) != expected_size
                            ):
                                discard_partial()
                                raise IOError("服务器返回的模型总大小与清单不一致")
                            if validator and validator != str(resume_metadata.get("validator") or ""):
                                discard_partial()
                                raise IOError("下载源文件已发生变化，正在重新下载")
                            total = int(total_value) if total_value != "*" else 0
                        else:
                            if expected_size and content_length and content_length != expected_size:
                                discard_partial()
                                raise IOError(
                                    f"模型文件大小不匹配（应为 {expected_size} 字节，服务器返回 {content_length} 字节）"
                                )
                            total = content_length
                            save_resume_metadata(validator)
                        done = resume_from
                        mode = "ab" if resume_from else "wb"
                        with open(part_path, mode) as f:
                            while True:
                                if cancelled():
                                    return
                                pause_event = control.get("pause_event")
                                if pause_event and pause_event.is_set():
                                    self.after(0, lambda: self._set_model_download_status(control, "已暂停，点击继续可断点续传"))
                                    return
                                chunk = resp.read(1024 * 1024)
                                if cancelled():
                                    return
                                if not chunk:
                                    break
                                if expected_size and done + len(chunk) > expected_size:
                                    raise IOError("下载数据超过模型清单声明的大小，已停止下载")
                                f.write(chunk)
                                done += len(chunk)
                                if total:
                                    percent = min(100, done * 100 / total)
                                    self.after(0, lambda p=percent, d=done, t=total: self._update_model_download_progress(control, p, d, t))
                                else:
                                    self.after(0, lambda d=done: self._set_model_download_status(control, f"已下载 {d / 1024 / 1024:.1f} MB"))
                        if total and done != total:
                            raise IOError(f"下载不完整（应接收 {total} 字节，实际 {done} 字节）")
                        if expected_size and done != expected_size:
                            if status_code == 200 or done > expected_size:
                                discard_partial()
                            raise IOError(
                                f"模型文件大小不匹配（应为 {expected_size} 字节，实际 {done} 字节）"
                            )
                    break
                except Exception:
                    if cancelled():
                        return
                    attempt += 1
                    if attempt > max_retries:
                        source_index += 1
                        if source_index >= len(urls):
                            raise
                        url = urls[source_index]
                        control["url"] = url
                        discard_partial()
                        attempt = 0
                        self.after(
                            0,
                            lambda: self._set_model_download_status(
                                control,
                                "国内镜像连接失败，正在切换官方源...",
                            ),
                        )
                        continue
                    self.after(0, lambda n=attempt: self._set_model_download_status(control, f"网络中断，正在重连 {n}/{max_retries}..."))
                    stop_event = control.get("stop_event")
                    if stop_event and stop_event.wait(min(2 * attempt, 6)):
                        return
                    if not stop_event:
                        time.sleep(min(2 * attempt, 6))
            if cancelled():
                return
            if not model_file_ready(part_path, expected_size or None):
                raise IOError("模型文件校验未通过，已保留断点文件")
            if expected_sha256:
                self.after(0, lambda: self._set_model_download_status(control, "下载完成，正在校验 SHA256..."))
                if not model_file_sha256_matches(part_path, expected_sha256):
                    discard_partial()
                    raise IOError("模型 SHA256 与可信清单不一致，已删除无效下载")
            if cancelled():
                return
            os.replace(part_path, target)
            meta_path.unlink(missing_ok=True)
            self.after(0, lambda: self._finish_model_download(control))
        except Exception as exc:
            if not cancelled():
                self.after(0, lambda e=str(exc): self._fail_model_download(control, e))

    def _update_model_download_progress(self, control: dict, percent: float, done: int, total: int):
        control = self._model_download_owner(control)
        control["progress_percent"] = float(percent)
        self._set_model_download_status(
            control,
            f"{percent:.0f}%  {done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB",
        )

    def _finish_model_download(self, control: dict):
        control = self._model_download_owner(control)
        self._release_model_transfer(control)
        control["state"] = "done"
        control["worker"] = None
        control["progress_percent"] = 100.0
        self._set_model_download_status(control, "下载完成，已放入模型目录")
        try:
            self._footer_label.config(text="  模型下载完成，正在重新检测...")
        except Exception:
            pass
        threading.Thread(target=self._recheck_models, daemon=True).start()

    def _fail_model_download(self, control: dict, error: str):
        control = self._model_download_owner(control)
        if control.get("state") == "cancelling":
            return
        self._release_model_transfer(control)
        control["state"] = "failed"
        control["worker"] = None
        self._set_model_download_status(control, f"下载失败，已保留断点：{error}")
        try:
            self._footer_label.config(text="  模型下载失败，请检查网络或稍后重试")
        except Exception:
            pass

    def _open_workflows_dir(self):
        workflows_dir = _workflows_dir()
        workflows_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(workflows_dir))

    def _workflow_slug(self, value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9._-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("._-")
        if not text:
            text = f"workflow_{uuid.uuid4().hex[:8]}"
        return text[:64]

    def _unique_workflow_id(self, base_id: str) -> str:
        workflows_dir = _workflows_dir()
        base_id = self._workflow_slug(base_id)
        candidate = base_id
        index = 2
        while (workflows_dir / candidate).exists():
            candidate = f"{base_id}_{index}"
            index += 1
        return candidate

    def _workflow_manifest_type(self, output_type: str) -> str:
        output_type = str(output_type or "").lower()
        if output_type == "video":
            return "video.first_last_to_video"
        if output_type == "text":
            return "text.chat"
        return "image.text_to_image"

    def _infer_workflow_output_type(self, name: str, data: dict, manifest: dict = None) -> str:
        manifest = manifest or {}
        return infer_output_type(data, manifest.get("type") or manifest.get("output_type") or "")

    def _workflow_description_for_type(self, output_type: str) -> str:
        if output_type == "video":
            return "首帧+尾帧转视频工作流，输入 prompt、start_image、end_image 后输出分镜视频"
        if output_type == "text":
            return "文字生成工作流，输入 prompt 后输出脚本、分镜或结构化文本"
        return "文生图工作流，输入 prompt 后输出分镜图片"

    def _workflow_input_schema_for_type(self, output_type: str) -> dict:
        output_type = str(output_type or "").lower()
        if output_type == "video":
            return {
                "summary": "输入文字动作提示词、首帧图片、尾帧图片，生成分镜视频。",
                "required": ["prompt", "start_image", "end_image"],
                "optional": ["duration", "fps", "seed"],
                "response": {"type": "video", "format": "url"},
                "inputs": [
                    {"name": "prompt", "type": "text", "label": "动作/镜头提示词", "required": True},
                    {"name": "start_image", "type": "image", "label": "首帧图片", "required": True},
                    {"name": "end_image", "type": "image", "label": "尾帧图片", "required": True},
                    {"name": "duration", "type": "number", "label": "时长秒", "required": False},
                    {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
                ],
            }
        if output_type == "text":
            return {
                "summary": "输入文字需求，生成脚本、角色或分镜结构化 JSON。",
                "required": ["prompt"],
                "optional": ["messages", "response_format"],
                "response": {"type": "json", "format": "json_object"},
                "inputs": [
                    {"name": "prompt", "type": "text", "label": "文字需求", "required": True},
                    {"name": "messages", "type": "messages", "label": "聊天消息", "required": False},
                    {"name": "response_format", "type": "object", "label": "响应格式", "required": False, "default": {"type": "json_object"}},
                ],
            }
        return {
            "summary": "输入文字提示词，生成分镜图片。",
            "required": ["prompt"],
            "optional": ["negative_prompt", "size", "width", "height", "steps", "seed"],
            "response": {"type": "image", "format": "url"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "提示词", "required": True},
                {"name": "negative_prompt", "type": "text", "label": "反向提示词", "required": False},
                {"name": "size", "type": "string", "label": "尺寸", "required": False},
                {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
            ],
        }

    def _workflow_schema_for_display(self, workflow: dict) -> dict:
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        schema = workflow.get("input_schema") if isinstance(workflow.get("input_schema"), dict) else {}
        if not schema:
            schema = self._workflow_input_schema_for_type(output_type)
        inputs = schema.get("inputs") if isinstance(schema.get("inputs"), list) else workflow.get("inputs")
        if not isinstance(inputs, list):
            inputs = self._workflow_input_schema_for_type(output_type).get("inputs", [])
        merged = dict(schema)
        merged["inputs"] = inputs
        if "summary" not in merged:
            merged["summary"] = self._workflow_description_for_type(output_type)
        if "response" not in merged:
            merged["response"] = self._workflow_input_schema_for_type(output_type).get("response", {})
        return merged

    def _workflow_output_contract(self, workflow: dict, schema: dict) -> dict:
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        if output_type == "text":
            return {
                "status": "completed",
                "text": "{...可解析的结构化 JSON 字符串...}",
                "output": {"text": "{...同上...}"},
                "notes": "服务端会把 text/output.text 解析为脚本、角色或分镜 JSON；纯文字工作流必须返回 JSON 对象或 JSON 字符串。",
            }
        if output_type == "video":
            return {
                "status": "completed",
                "outputs": [
                    {
                        "filename": "task_xxx.mp4",
                        "type": "video",
                        "url": "https://客户端公网URL/v1/files/{task_id}/task_xxx.mp4",
                    }
                ],
                "notes": "服务端拿 outputs[].url 下载并填充到对应分镜视频位置。",
            }
        return {
            "created": int(time.time()),
            "data": [
                {
                    "url": "https://客户端公网URL/v1/files/{task_id}/task_xxx.png"
                }
            ],
            "notes": "图片生成优先兼容火山方舟 Images API；旧任务接口也会归一化为 outputs[].url。",
        }

    def _format_workflow_schema_text(self, workflow: dict) -> str:
        schema = self._workflow_schema_for_display(workflow)
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        dependency_report = workflow_dependency_report(
            workflow.get("dependencies") or {},
            _models_dir(),
        )
        required_models = list(workflow.get("required_models") or dependency_report["required_models"])
        missing_models = set(workflow.get("missing_models") or dependency_report["missing_models"])
        unverified_models = set(
            workflow.get("unverified_models") or dependency_report.get("unverified_models") or []
        )
        required_nodes = list(workflow.get("required_nodes") or dependency_report["required_nodes"])
        missing_nodes = set(workflow.get("missing_nodes") or [])
        lines = [
            f"工作流：{self._workflow_display_name(workflow)}",
            f"ID：{workflow.get('id') or '-'}",
            f"类型：{output_type}",
            f"说明：{workflow.get('description') or schema.get('summary') or '-'}",
            "",
            "入参结构：",
        ]
        inputs = schema.get("inputs") or []
        if not inputs:
            lines.append("  - 无公开输入参数")
        else:
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("key") or "-"
                typ = item.get("type") or "string"
                label = item.get("label") or item.get("description") or ""
                required = "必填" if item.get("required") else "可选"
                default = item.get("default", None)
                suffix = f"，默认 {json.dumps(default, ensure_ascii=False)}" if default not in (None, "") else ""
                lines.append(f"  - {name} ({typ}, {required})：{label or name}{suffix}")
        lines.extend(["", "模型与节点："])
        if required_models:
            for name in required_models:
                marker = "缺少" if name in missing_models else ("待校验" if name in unverified_models else "已找到")
                lines.append(f"  - 模型 [{marker}] {name}")
        else:
            lines.append("  - 模型：工作流未声明模型文件")
        if required_nodes:
            for name in required_nodes:
                marker = "缺少" if name in missing_nodes else (
                    "已找到" if workflow.get("nodes_verified") else "启动 ComfyUI 后校验"
                )
                lines.append(f"  - 节点 [{marker}] {name}")
        else:
            lines.append("  - 节点：工作流未声明节点")
        lines.extend([
            "",
            "通用请求字段：",
            "  - model：工作流 ID 或名称，服务端会按客户端同步的工作流列表选择。",
            "  - prompt：仅在该工作流的输入参数列表包含此字段时填写。",
            "  - task_id：任务轮询 ID，由客户端生成任务后返回。",
            "",
            "出参结构：",
            json.dumps(self._workflow_output_contract(workflow, schema), ensure_ascii=False, indent=2),
        ])
        return "\n".join(lines)

    def _show_workflow_schema(self, workflow: dict):
        text = self._format_workflow_detail_text(workflow)
        title = self._workflow_display_name(workflow)
        model_key = self._workflow_model_key(workflow)
        missing_models = self._missing_model_items(model_key) if model_key else []
        missing_models = missing_models or workflow.get("missing_models") or []
        popup = tk.Toplevel(self)
        popup.title(f"工作流详情 - {title}")
        popup.geometry("760x560")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, 760, 560)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        heading = tk.Frame(panel, bg=C["card"])
        heading.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(heading, text=f"{title}：用途与性能要求", font=F["title"], fg=C["text"], bg=C["card"]).pack(side="left")

        def show_downloads():
            popup.destroy()
            self._show_workflow_model_help(workflow)

        if missing_models:
            self._button(heading, "安装模型", show_downloads, "primary", width=88).pack(side="right")
        tk.Label(panel, text="这里可以查看用途、电脑配置要求，以及需要对接时使用的参数说明。",
                 font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=20, pady=(0, 12))

        box = tk.Text(
            panel,
            font=F["body"],
            bg=C["entry"],
            fg=C["text"],
            relief="flat",
            wrap="word",
            height=20,
            highlightthickness=1,
            highlightbackground=C["border2"],
            padx=10,
            pady=10,
            spacing1=2,
            spacing3=4,
        )
        box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        box.insert("1.0", text)
        box.config(state="disabled")

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=20, pady=(0, 16))

        def copy_text():
            self.clipboard_clear()
            self.clipboard_append(text)
            self._footer_label.config(text="  已复制工作流详情")

        self._button(actions, "复制详情", copy_text, "primary").pack(side="left", ipadx=12, ipady=5)
        self._button(actions, "手动填写参数", lambda: (popup.destroy(), self._show_workflow_mapping_editor(workflow)), "plain").pack(side="left", padx=8)
        self._button(actions, "关闭", popup.destroy, "plain").pack(side="right", ipadx=12, ipady=5)

    def _load_json_file(self, path: Path, max_bytes: int = 64 * 1024 * 1024) -> dict:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"无法读取 JSON 文件：{path.name}") from exc
        if size <= 0:
            raise ValueError(f"JSON 文件为空：{path.name}")
        if size > max_bytes:
            raise ValueError(f"JSON 文件过大：{path.name}（最大 {max_bytes // 1024 // 1024} MB）")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        return data

    def _validate_workflow_manifest(self, manifest: dict) -> dict:
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 顶层必须是对象")
        for key, limit in (("id", 128), ("name", 200), ("type", 100), ("engine", 50)):
            value = manifest.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"manifest.json 的 {key} 必须是文本")
            if len(value.strip()) > limit:
                raise ValueError(f"manifest.json 的 {key} 过长")
        engine = str(manifest.get("engine") or "comfyui").strip().lower()
        if engine != "comfyui":
            raise ValueError("当前客户端只支持 engine=comfyui 的本地工作流")
        for key in ("input_schema", "inputSchema"):
            if key in manifest and not isinstance(manifest.get(key), dict):
                raise ValueError(f"manifest.json 的 {key} 必须是对象")
        if "inputs" in manifest and not isinstance(manifest.get("inputs"), list):
            raise ValueError("manifest.json 的 inputs 必须是列表")
        if "dependencies" in manifest and not isinstance(manifest.get("dependencies"), dict):
            raise ValueError("manifest.json 的 dependencies 必须是对象")
        if "enabled" in manifest and not isinstance(manifest.get("enabled"), bool):
            raise ValueError("manifest.json 的 enabled 必须是 true 或 false")
        for key, limit in (("version", 50), ("description", 500)):
            if key in manifest and not isinstance(manifest.get(key), str):
                raise ValueError(f"manifest.json 的 {key} 必须是文本")
            if len(str(manifest.get(key) or "").strip()) > limit:
                raise ValueError(f"manifest.json 的 {key} 过长")
        return dict(manifest)

    def _is_comfy_api_workflow(self, data: dict) -> bool:
        try:
            validate_graph(data)
            return True
        except (ValueError, TypeError):
            return False

    def _workflow_link_map(self, data: dict) -> dict:
        links = data.get("links") or []
        mapping = {}
        for link in links:
            try:
                if isinstance(link, (list, tuple)) and len(link) >= 6:
                    link_id, origin_id, origin_slot, target_id, target_slot = link[:5]
                    mapping[link_id] = [str(origin_id), int(origin_slot or 0)]
                elif isinstance(link, dict):
                    link_id = link.get("id")
                    origin_id = link.get("origin_id", link.get("originId", link.get("from_node_id", link.get("fromNode"))))
                    origin_slot = link.get("origin_slot", link.get("originSlot", link.get("from_socket", link.get("fromSlot", 0))))
                    if link_id is not None and origin_id is not None:
                        mapping[link_id] = [str(origin_id), int(origin_slot or 0)]
            except Exception:
                continue
        if mapping:
            return mapping

        for node in data.get("nodes") or []:
            node_id = node.get("id")
            if node_id is None:
                continue
            for index, output in enumerate(node.get("outputs") or []):
                for link_id in output.get("links") or []:
                    mapping[link_id] = [str(node_id), int(output.get("slot_index", index) or 0)]
        return mapping

    def _front_workflow_widget_inputs(self, node: dict) -> list:
        inputs = node.get("inputs") or []
        widget_inputs = []
        for item in inputs:
            if not isinstance(item, dict) or item.get("link") is not None:
                continue
            widget = item.get("widget") if isinstance(item.get("widget"), dict) else {}
            name = widget.get("name") or item.get("name")
            if name:
                widget_inputs.append(str(name))
        return widget_inputs

    def _convert_front_workflow_to_api(self, data: dict) -> dict:
        try:
            return convert_editor_workflow(data)
        except ValueError:
            install_frontend_bridge(BASE_DIR / 'runtime' / 'ComfyUI')
            return convert_with_comfyui(
                data, COMFY_BASE, progress=self._report_workflow_import_progress,
                cancelled=lambda: self._shutting_down,
            )

    def _find_workflow_json_in_dir(self, folder: Path) -> Path:
        preferred = [
            folder / "workflow.json",
            *sorted(folder.glob("*_api.json")),
            *sorted(folder.glob("*api*.json")),
            *sorted(folder.glob("*.json")),
        ]
        for path in preferred:
            if not path.exists() or path.name.lower() == "manifest.json":
                continue
            try:
                data = self._load_json_file(path)
            except Exception:
                continue
            if self._is_comfy_api_workflow(data) or ("nodes" in data and "links" in data):
                return path
        raise ValueError("没有找到 ComfyUI API 格式的 workflow JSON。请在 ComfyUI 里使用“Save (API Format)”导出。")

    def _prepare_workflow_source(self, source_path: Path) -> tuple[Path | None, Path, dict, dict, Path | None]:
        source_path = Path(source_path)
        cleanup_root = None
        try:
            if not source_path.exists():
                raise ValueError("选择的工作流文件或目录不存在")
            if source_path.is_symlink():
                raise ValueError("工作流来源不能是符号链接")

            source_root = None
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                cleanup_root = create_import_workspace(BASE_DIR / "runtime")
                extract_zip_safely(source_path, cleanup_root)
                source_root = effective_workflow_root(cleanup_root)
            elif source_path.is_dir():
                source_root = source_path
            elif source_path.is_file() and source_path.suffix.lower() == ".json":
                source_root = None
            else:
                raise ValueError("请选择 ComfyUI 工作流 JSON、ZIP 包或工作流文件夹")

            manifest = {}
            if source_root is not None:
                manifest_path = source_root / "manifest.json"
                if manifest_path.exists():
                    manifest = self._validate_workflow_manifest(
                        self._load_json_file(manifest_path, max_bytes=2 * 1024 * 1024)
                    )
                workflow_json = self._find_workflow_json_in_dir(source_root)
            else:
                workflow_json = source_path

            data = self._load_json_file(workflow_json)
            if len(data) > 10_000:
                raise ValueError("工作流节点过多（最多 10000 个）")
            if not self._is_comfy_api_workflow(data):
                if "nodes" in data and "links" in data:
                    data = self._convert_front_workflow_to_api(data)
                    print(f"[Workflow] Converted frontend workflow to API format: {workflow_json}")
                else:
                    raise ValueError("这个 JSON 看起来不是 ComfyUI API 工作流。")
            if not self._is_comfy_api_workflow(data):
                raise ValueError("工作流自动转换失败：转换结果不是 API 模式。")
            return source_root, workflow_json, data, manifest, cleanup_root
        except Exception:
            if cleanup_root is not None:
                shutil.rmtree(cleanup_root, ignore_errors=True)
            raise

    def _workflow_registry(self) -> WorkflowRegistry:
        return WorkflowRegistry(
            config_path=BASE_DIR / "runtime" / "workflow_config.json",
            workflows_dir=_workflows_dir(),
        )

    def _workflow_records_from_registry(self, registry: WorkflowRegistry) -> list[dict]:
        return [
            {
                **workflow.to_dict(),
                "is_default": workflow.id == registry.default_workflow_id,
            }
            for workflow in registry.workflows
        ]

    def _write_workflow_config_from_manifests(self, default_workflow_id: str = "") -> tuple[list[dict], str]:
        registry = self._workflow_registry()
        registry.scan_folder()
        if default_workflow_id and not registry.default_workflow_id:
            registry.set_default(default_workflow_id)
        return self._workflow_records_from_registry(registry), str(registry.default_workflow_id or "")

    def _install_workflow_from_path(self, source_path: Path) -> dict:
        source_root = None
        cleanup_root = None
        staging_dir = None
        target_dir = None
        target_committed = False
        try:
            source_root, workflow_json, data, manifest, cleanup_root = self._prepare_workflow_source(source_path)
            source_stem = workflow_json.stem
            if source_stem.lower() in ("workflow", "api", "workflow_api") and workflow_json.parent.name:
                source_stem = workflow_json.parent.name
            base_name = str(manifest.get("id") or source_stem or Path(source_path).stem)
            workflow_id = self._unique_workflow_id(base_name)
            output_type = self._infer_workflow_output_type(workflow_json.stem, data, manifest)
            if manifest.get("api_mapping_status") == "ready":
                adaptation = make_mapping(data, mapping_fields(manifest), output_type)
            else:
                adaptation = self._recognize_workflow(data, manifest.get("type", ""))
            output_type = adaptation["output_type"]
            workflow_name = str(manifest.get("name") or source_stem or workflow_id).strip()[:200]
            if not workflow_name:
                workflow_name = workflow_id

            workflows_dir = ensure_safe_workflows_root(
                _workflows_dir(),
                create=True,
            )
            workflows_root = workflows_dir.resolve(strict=True)
            target_dir = workflows_dir / workflow_id
            staging_dir = Path(tempfile.mkdtemp(prefix=".importing_", dir=workflows_dir))
            if staging_dir.resolve(strict=True).parent != workflows_root:
                raise ValueError("工作流暂存目录越出项目范围")
            if source_root is not None:
                copy_workflow_assets(
                    source_root,
                    staging_dir,
                    exclude_names={"manifest.json"},
                )
            if self._shutting_down:
                raise RuntimeError("客户端正在退出，已取消工作流导入")

            with open(staging_dir / "workflow.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            input_schema = adaptation["input_schema"]
            dependencies = normalize_workflow_dependencies(
                manifest.get("dependencies") or {},
                data,
            )
            manifest_data = {
                **manifest,
                **adaptation,
                "id": workflow_id,
                "name": workflow_name,
                "type": self._workflow_manifest_type(output_type),
                "engine": "comfyui",
                "enabled": bool(manifest.get("enabled", True)),
                "version": str(manifest.get("version") or "1.0.0")[:50],
                "description": str(
                    manifest.get("description") or self._workflow_description_for_type(output_type)
                )[:500],
                "input_schema": input_schema,
                "dependencies": dependencies,
            }
            with open(staging_dir / "manifest.json", "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, ensure_ascii=False, indent=2)

            registry = self._workflow_registry()
            with registry.locked_mutation():
                try:
                    ensure_safe_workflows_root(workflows_dir)
                    workflows_root = workflows_dir.resolve(strict=True)
                    if staging_dir.resolve(strict=True).parent != workflows_root:
                        raise ValueError("工作流暂存目录越出项目范围")
                    if target_dir.resolve(strict=False).parent != workflows_root:
                        raise ValueError("工作流目标目录越出项目范围")
                    if target_dir.exists():
                        raise FileExistsError(f"工作流 ID 已存在：{workflow_id}")
                    os.replace(staging_dir, target_dir)
                    staging_dir = None
                    target_committed = True

                    # Prepare every in-memory value before the atomic config
                    # replace. If any preparation fails, the previous config
                    # is still current and the committed directory is removed
                    # while the same cross-process lock is held.
                    registry._scan_folder_unlocked(save=False)
                    workflows = self._workflow_records_from_registry(registry)
                    default_workflow_id = str(registry.default_workflow_id or "")
                    result = {
                        "adaptation": adaptation,
                        "id": workflow_id,
                        "name": workflow_name,
                        "output_type": output_type,
                        "target": str(target_dir),
                        "workflows": workflows,
                        "default_workflow_id": default_workflow_id,
                        "dependencies": dependencies,
                    }
                    registry._save_unlocked()
                except Exception:
                    if target_committed and target_dir is not None:
                        shutil.rmtree(target_dir, ignore_errors=False)
                        target_committed = False
                    registry._load_unlocked()
                    raise
            return result
        except Exception:
            if target_committed and target_dir is not None:
                shutil.rmtree(target_dir, ignore_errors=True)
            raise
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)
            if cleanup_root is not None:
                shutil.rmtree(cleanup_root, ignore_errors=True)

    def _reload_workflows_and_sync(self):
        import urllib.request as ur
        if self._shutting_down:
            return
        try:
            req = ur.Request(
                f"{API_BASE}/v1/workflows/reload",
                data=b"{}",
                method="POST",
                headers=self._local_admin_headers(),
            )
            ur.urlopen(req, timeout=8).read()
        except Exception as reload_error:
            print(f"[Workflow] reload skipped/failed: {reload_error}")

        if self._shutting_down:
            return
        health_event = self._reserve_health_event()
        try:
            req = ur.Request(
                f"{API_BASE}/v1/status",
                method="GET",
                headers=self._local_api_headers(),
            )
            resp = ur.urlopen(req, timeout=8)
            try:
                data = json.loads(resp.read().decode("utf-8"))
            finally:
                closer = getattr(resp, "close", None)
                if callable(closer):
                    closer()
            if health_event is not None:
                generation, sequence = health_event
                self._post_to_ui(
                    lambda g=generation, d=data, s=sequence:
                    self._deliver_health_snapshot(g, d, sequence=s)
                )
            else:
                self._post_to_ui(lambda d=data: self._on_health_update(d))
        except Exception as health_error:
            print(f"[Workflow] health refresh failed: {health_error}")

        if self._shutting_down:
            return
        if self._server_session_token:
            try:
                self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES)
                self._post_to_ui(lambda: self._set_light("server", "online", "已同步"))
            except Exception as sync_error:
                print(f"[Workflow] sync after import failed: {sync_error}")
                self._post_to_ui(lambda: self._set_light("server", "offline", "同步失败"))

    def _show_workflow_import_result(self, result: dict):
        adaptation = result.get("adaptation") or {}
        if adaptation.get("api_mapping_status") == "needs_review":
            messagebox.showwarning("需要手动填写参数", adaptation.get("api_mapping_error", "识别未完成"), parent=self)
            self._show_workflow_mapping_editor(result)
            return
        messagebox.showinfo(
            "工作流已导入",
            "工作流已安全放入客户端目录并完成本地注册。\n\n"
            f"名称：{result.get('name')}\n"
            f"ID：{result.get('id')}\n"
            f"类型：{result.get('output_type')}\n\n"
            "客户端正在后台加载最新列表；如果 API 暂未运行，会在下次启动时自动生效。\n"
            "首次运行不可信工作流前，请先查看节点与模型清单。",
            parent=self,
        )
        self._footer_label.config(text=f"  工作流已导入：{result.get('name')}")

    def _check_workflow_recognition_ready(self, workflow_id: str):
        """Check deployment without loading weights or submitting inference."""
        self._report_workflow_import_progress("checking_recognition")
        models_dir = _models_dir()
        missing = [Path(item["path"]).name for item in MODEL_REQUIREMENTS["Qwen3.5"]["items"]
                   if not _model_file_ready(models_dir / item["path"], item.get("size_bytes"))]
        if missing:
            raise ValueError("Qwen3.5 4B 模型未部署完整：" + "、".join(missing) + "；请手动填写参数")
        if missing_runtime_paths(BASE_DIR):
            raise ValueError("本机运行环境未安装完整；请修复运行环境，或手动填写参数")
        request = ur.Request(f"{API_BASE}/v1/status", headers=self._local_api_headers(), method="GET")
        try:
            with ur.urlopen(request, timeout=5) as response:
                status = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
            if not isinstance(status, dict):
                raise ValueError("状态格式无效")
        except Exception as exc:
            raise ValueError("无法确认本机服务状态（离线、鉴权失败或检查超时）；请手动填写参数") from exc
        if (status.get("runtime") or {}).get("status") != "installed":
            raise ValueError("本机运行环境未就绪；请修复运行环境，或手动填写参数")
        if (status.get("comfyui") or {}).get("status") != "online":
            raise ValueError("ComfyUI 未启动或无法连接；请启动后台，或手动填写参数")
        if self._comfyui_update_task_active(status):
            raise ValueError("后台正在执行任务，本次跳过 4B 识别；请手动填写参数")
        workflow = next((item for item in (status.get("workflows") or [])
                         if isinstance(item, dict) and item.get("id") == workflow_id), None)
        if not workflow:
            raise ValueError("后台尚未加载 Qwen3.5 4B 工作流；请刷新工作流，或手动填写参数")
        if workflow.get("missing_models"):
            raise ValueError("Qwen3.5 4B 缺少模型：" + "、".join(map(str, workflow["missing_models"])) + "；请手动填写参数")
        if workflow.get("missing_nodes"):
            raise ValueError("Qwen3.5 4B 缺少节点：" + "、".join(map(str, workflow["missing_nodes"])) + "；请手动填写参数")
        if not workflow.get("available"):
            raise ValueError("Qwen3.5 4B 工作流不可用或依赖尚未验证；请手动填写参数")

    def _call_workflow_recognition_llm(self, prompt: str) -> str:
        """One bounded call through the existing local text workflow."""
        registry = self._workflow_registry()
        workflow = registry.resolve("llm_qwen3_text_gen")
        if workflow is None:
            raise ValueError("Qwen3.5 4B 工作流未安装或未启用，请先准备现有文字模型")
        self._check_workflow_recognition_ready(workflow.id)
        self._report_workflow_import_progress("recognition_inference")
        request = ur.Request(
            f"{API_BASE}/v1/chat/completions",
            data=json.dumps({"model": workflow.id, "prompt": prompt,
                             "response_format": {"type": "json_object"}, "timeout": 120}).encode("utf-8"),
            headers={**self._local_api_headers(), "Content-Type": "application/json"}, method="POST",
        )
        try:
            with ur.urlopen(request, timeout=130) as response:
                payload = json.loads(response.read(128000).decode("utf-8"))
        except Exception as exc:
            raise ValueError("本机 4B 不可用、繁忙或超时；请检查模型环境，或手动填写。超时任务可能仍在运行") from exc
        if payload.get("workflow") != workflow.id:
            raise ValueError("文字接口返回了其他工作流，已停止识别")
        return payload["choices"][0]["message"]["content"]

    def _recognize_workflow(self, graph: dict, declared: str = "") -> dict:
        self._report_workflow_import_progress('recognizing')
        result = analyze_workflow(graph, declared)
        if result["api_mapping_status"] == "ready":
            return result
        object_info = None
        try:
            with ur.urlopen(f"{COMFY_BASE}/object_info", timeout=2) as response:
                object_info = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
            if not isinstance(object_info, dict):
                object_info = None
        except Exception:
            pass
        return analyze_workflow(graph, declared, object_info, self._call_workflow_recognition_llm)

    def _show_workflow_mapping_editor(self, workflow: dict):
        """Manual escape hatch after a rule/model failure, using the same validator."""
        try:
            registry = self._workflow_registry()
            wf = registry.get(str(workflow.get("id") or ""))
            if wf is None or wf.folder is None:
                raise ValueError("工作流不存在，请刷新列表")
            graph = self._load_json_file(wf.folder / "workflow.json")
            validate_graph(graph)
            expected_hash = graph_hash(graph)
            mapping = {**wf.api_mapping, "input_schema": wf.input_schema}
            draft = {"output_type": infer_output_type(graph, wf.workflow_type),
                     "fields": mapping_fields(mapping) if wf.api_mapping else mapping_fields(analyze_workflow(graph, wf.workflow_type))}
        except Exception as exc:
            messagebox.showerror("无法设置参数", str(exc), parent=self)
            return
        popup = tk.Toplevel(self)
        popup.title(f"手动填写参数 - {wf.name}")
        popup.geometry("820x680")
        popup.configure(bg=C["bg"])
        tk.Label(popup, text="填写公开参数：name 为调用名，type 为类型，targets 为节点位置。保存前会校验。\n"
                 "示例：{\"name\":\"prompt\",\"type\":\"text\",\"targets\":[{\"node_id\":\"6\",\"input\":\"text\"}]}",
                 bg=C["bg"], fg=C["text"], justify="left", wraplength=780).pack(fill="x", padx=16, pady=12)
        editor = tk.Text(popup, height=18, wrap="none", bg=C["entry"], fg=C["text"], insertbackground=C["text"])
        editor.pack(fill="both", expand=True, padx=16)
        editor.insert("1.0", json.dumps(draft, ensure_ascii=False, indent=2))
        tk.Label(popup, text="可填写的节点字段（只读参考）", bg=C["bg"], fg=C["text"]).pack(anchor="w", padx=16, pady=6)
        reference = tk.Text(popup, height=7, wrap="word", bg=C["entry"], fg=C["text"])
        reference.pack(fill="x", padx=16)
        reference.insert("1.0", json.dumps(candidates(graph), ensure_ascii=False, indent=2))
        reference.config(state="disabled")
        status = tk.Label(popup, text="", bg=C["bg"], fg=C["text"], wraplength=780)
        status.pack(fill="x", padx=16)

        def save():
            try:
                raw = editor.get("1.0", "end").strip()
                if len(raw) > 250000:
                    raise ValueError("参数配置过大")
                data = json.loads(raw)
                registry.save_mapping(wf.id, data.get("fields"), data.get("output_type"), expected_hash)
                self._publish_local_workflows(self._workflow_records_from_registry(registry), str(registry.default_workflow_id or ""))
                threading.Thread(target=self._reload_workflows_and_sync, daemon=True).start()
                popup.destroy()
            except Exception as exc:
                status.config(text=f"保存失败：{exc}")

        self._button(popup, "校验并保存", save, "primary").pack(pady=12)

    def _begin_workflow_operation(self, name: str) -> bool:
        if self._shutting_down or self._runtime_maintenance_active():
            return False
        if not self._workflow_management_lock.acquire(blocking=False):
            current = self._workflow_operation_name or "工作流操作"
            self._footer_label.config(text=f"  {current}正在进行，请稍候")
            return False
        if self._shutting_down or self._runtime_maintenance_active():
            self._workflow_management_lock.release()
            return False
        self._workflow_operation_name = str(name or "工作流操作")
        return True

    def _end_workflow_operation(self):
        self._workflow_operation_name = ""
        try:
            self._workflow_management_lock.release()
        except RuntimeError:
            pass

    def _wait_for_workflow_idle(self, timeout: float = 10.0) -> bool:
        lock = getattr(self, "_workflow_management_lock", None)
        if lock is None:
            return True
        acquired = lock.acquire(timeout=max(0.0, float(timeout)))
        if acquired:
            lock.release()
        return acquired

    def _publish_local_workflows(self, workflows: list[dict], default_workflow_id: str = ""):
        if self._shutting_down:
            return
        data = dict(self._last_health or {})
        data["workflows"] = [dict(item) for item in workflows if isinstance(item, dict)]
        data["workflow_count"] = len(data["workflows"])
        data["default_workflow"] = default_workflow_id
        data["default_workflow_id"] = default_workflow_id
        self._last_health = data
        self._update_workflow_display(data)
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(data)

    def _finish_workflow_import(self, result: dict, popup=None):
        self._clear_workflow_import_progress()
        if self._shutting_down:
            return
        try:
            if popup is not None and popup.winfo_exists():
                popup.destroy()
        except Exception:
            pass
        self._publish_local_workflows(
            result.get("workflows") or [],
            str(result.get("default_workflow_id") or ""),
        )
        threading.Thread(target=self._reload_workflows_and_sync, daemon=True).start()
        self._footer_label.config(text=f"  工作流已导入：{result.get('name')}")
        self._post_to_ui(lambda data=dict(result): self._show_workflow_import_result(data), delay=50)

    def _fail_workflow_operation(self, title: str, error: str, popup=None):
        self._clear_workflow_import_progress()
        if self._shutting_down:
            return
        parent = self
        try:
            if popup is not None and popup.winfo_exists():
                parent = popup
        except Exception:
            parent = self
        messagebox.showerror(title, str(error), parent=parent)
        self._footer_label.config(text=f"  {title}：{error}")

    def _select_and_install_workflow_file(self, popup=None):
        path = filedialog.askopenfilename(
            title="选择 ComfyUI API 工作流 JSON 或 ZIP",
            filetypes=[
                ("工作流文件", "*.json *.zip"),
                ("ComfyUI API 工作流", "*.json"),
                ("ZIP 工作流包", "*.zip"),
                ("所有文件", "*.*"),
            ],
            parent=popup or self,
        )
        if not path:
            return
        self._run_workflow_import(Path(path), popup)

    def _select_and_install_workflow_folder(self, popup=None):
        path = filedialog.askdirectory(
            title="选择工作流文件夹",
            parent=popup or self,
        )
        if not path:
            return
        self._run_workflow_import(Path(path), popup)

    def _clear_workflow_import_progress(self):
        bar = self.__dict__.pop('_workflow_import_progress', None)
        if bar is not None:
            bar.stop()
            bar.destroy()

    def _report_workflow_import_progress(self, stage):
        if self.__dict__.get('_workflow_import_progress') is None or self._shutting_down:
            return
        labels = {
            'starting': '正在启动后台转换', 'loading': '正在加载工作流和节点',
            'serializing': '正在转换为 API 结构', 'converted': '转换完成，正在保存',
            'recognizing': '正在识别输入参数，必要时调用本地模型',
            'checking_recognition': '正在检查 4B 模型与后台状态',
            'recognition_inference': '正在调用本地 4B 识别输入参数',
        }
        def update():
            if self.__dict__.get('_workflow_import_progress') is not None:
                self._footer_label.config(text='  ' + labels.get(stage, '正在导入工作流'))
        self._post_to_ui(update)

    def _run_workflow_import(self, source_path: Path, popup=None):
        if not self._begin_workflow_operation("工作流导入"):
            return
        try:
            if popup is not None and popup.winfo_exists():
                popup.grab_release()
                popup.destroy()
        except Exception:
            pass
        popup = None
        self._footer_label.config(text="  正在安全检查并导入工作流...")
        self._clear_workflow_import_progress()
        if isinstance(self._footer_label, tk.Widget):
            self._workflow_import_progress = ttk.Progressbar(
                self._footer_label.master, mode='indeterminate', length=120,
                style='Progress.Horizontal.TProgressbar')
            self._workflow_import_progress.pack(side='left', padx=8)
            self._workflow_import_progress.start(12)

        def worker():
            try:
                result = self._install_workflow_from_path(source_path)
            except Exception as error:
                if not self._shutting_down:
                    self._post_to_ui(
                        lambda message=str(error): self._fail_workflow_operation(
                            "工作流导入失败", message, popup
                        ),
                    )
            else:
                if not self._shutting_down:
                    try:
                        self._run_ui_backend_step(
                            lambda data=result: self._finish_workflow_import(data, popup)
                        )
                    except Exception as error:
                        print(f"[Workflow] import UI completion failed: {error}")
            finally:
                self._end_workflow_operation()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._clear_workflow_import_progress()
            self._end_workflow_operation()
            raise

    def _run_workflow_setting_change(self, name: str, change, success_text: str):
        if not self._begin_workflow_operation(name):
            return
        self._footer_label.config(text=f"  正在{name}...")

        def worker():
            try:
                registry = self._workflow_registry()
                registry.scan_folder()
                if not change(registry):
                    raise ValueError("工作流不存在、已停用或当前操作不允许")
                workflows = self._workflow_records_from_registry(registry)
                default_id = str(registry.default_workflow_id or "")
            except Exception as error:
                if not self._shutting_down:
                    self._post_to_ui(
                        lambda message=str(error): self._fail_workflow_operation(
                            f"{name}失败", message
                        ),
                    )
            else:
                if not self._shutting_down:
                    def finish():
                        self._publish_local_workflows(workflows, default_id)
                        self._footer_label.config(text=f"  {success_text}")
                        threading.Thread(target=self._reload_workflows_and_sync, daemon=True).start()
                    try:
                        self._run_ui_backend_step(finish)
                    except Exception as error:
                        print(f"[Workflow] setting UI completion failed: {error}")
            finally:
                self._end_workflow_operation()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._end_workflow_operation()
            raise

    def _set_workflow_enabled(self, workflow_id: str, enabled: bool):
        workflow_id = str(workflow_id or "").strip()

        def change(registry: WorkflowRegistry):
            if not enabled and len(registry.enabled_workflows) <= 1:
                raise ValueError("至少需要保留一个已启用的工作流")
            return registry.set_enabled(workflow_id, enabled)

        action = "启用工作流" if enabled else "停用工作流"
        self._run_workflow_setting_change(action, change, f"已{action[:2]}：{workflow_id}")

    def _set_default_workflow(self, workflow_id: str):
        workflow_id = str(workflow_id or "").strip()

        def change(registry: WorkflowRegistry):
            workflow = registry.get(workflow_id)
            if not workflow or not workflow.enabled:
                return False
            workflow_path = workflow.folder / "workflow.json" if workflow.folder else None
            if not workflow_path or not workflow_path.is_file():
                raise ValueError("工作流文件不完整，暂时不能设为默认")
            try:
                workflow_data = self._load_json_file(workflow_path)
            except Exception as error:
                raise ValueError("工作流文件无法读取，暂时不能设为默认") from error
            if not self._is_comfy_api_workflow(workflow_data):
                raise ValueError("工作流不是可调用的 ComfyUI API 格式")
            dependency = workflow_dependency_report(workflow.dependencies, _models_dir())
            if dependency["missing_models"]:
                raise ValueError("请先安装这个工作流需要的模型，再设为默认工作流")
            health_workflows = (self._last_health or {}).get("workflows") or []
            health_item = next(
                (
                    item
                    for item in health_workflows
                    if isinstance(item, dict) and str(item.get("id") or "") == workflow_id
                ),
                {},
            )
            if health_item.get("missing_nodes"):
                raise ValueError("请先安装缺失节点，再设为默认工作流")
            return registry.set_default(workflow_id)

        self._run_workflow_setting_change(
            "设置默认工作流",
            change,
            f"默认工作流已设为：{workflow_id}",
        )

    def _show_workflow_upload_dialog(self):
        text = (
            "最快的添加方式\n\n"
            "1. 在 ComfyUI 中把工作流导出为 API Format JSON。\n"
            "2. 点击下方“选择 JSON / ZIP”，其余注册步骤由客户端自动完成。\n"
            "3. 回到“模型与环境”查看工作流状态，配置输入与输出，并补齐缺少的模型或节点。\n\n"
            "客户端会根据工作流内容识别文字、图片或视频输出，并生成稳定的英文工作流 ID。"
            "调用方只需使用客户端提供的 URL + Key，并把该 ID 放在 model 参数中。\n\n"
            "也可以选择一个完整工作流文件夹。文件夹只应包含工作流 JSON、manifest 和少量说明/预览资源，"
            "不要把大型模型放进工作流包。\n\n"
            "安全提醒\n"
            "来自互联网的工作流可能调用本机已经安装的 ComfyUI 自定义节点。客户端只会导入和检查，"
            "不会自动运行；首次调用前请在“查看参数”中确认完整模型与节点清单。"
        )
        popup = tk.Toplevel(self)
        popup.title("添加工作流")
        popup.geometry("700x500")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, 700, 500)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        tk.Label(panel, text="添加工作流", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(panel, text="选择一次文件，客户端会完成检查、复制、注册和状态刷新。",
                 font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=20, pady=(0, 12))
        box = tk.Text(panel, font=F["small"], bg=C["entry"], fg=C["text"], relief="flat", wrap="word", height=18,
                      highlightthickness=1, highlightbackground=C["border2"])
        box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        box.insert("1.0", text)
        box.config(state="disabled")
        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=20, pady=(0, 16))
        self._button(actions, "选择 JSON / ZIP", lambda: self._select_and_install_workflow_file(popup), "primary").pack(side="left", ipadx=12, ipady=6)
        self._button(actions, "选择工作流文件夹", lambda: self._select_and_install_workflow_folder(popup), "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "打开目录", self._open_workflows_dir, "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "关闭", popup.destroy, "plain").pack(side="right", ipadx=12, ipady=6)

    # ══════════════════════════════════════════════════════
    # 操作按钮区
    # ══════════════════════════════════════════════════════
    def _load_icon_image(self, name: str, size: int = 24):
        path = GUI_ASSET_DIR / f"{name}_icon.png"
        if not path.exists():
            return None
        try:
            from PIL import Image, ImageTk
            image = Image.open(path).convert("RGBA")
            image.thumbnail((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self._image_refs.append(photo)
            return photo
        except Exception as exc:
            print(f"[GUI] icon load failed: {path} {exc}")
            return None

    def _action_icon(self, parent, kind: str):
        icon_name = {
            "folder": "folder",
            "cube": "model",
            "gear": "ops",
            "refresh": "restart",
        }.get(kind, kind)
        image = self._load_icon_image(icon_name, 24)
        if image is not None:
            label = tk.Label(parent, image=image, bg=C["card"], width=28, height=28)
            label.image = image
            return label

        canvas = tk.Canvas(parent, width=26, height=26, bg=C["card"], highlightthickness=0, bd=0)
        color = "#58708f"
        w = 1.8
        if kind == "folder":
            canvas.create_line(5, 9, 10, 9, 12, 12, 21, 12, 21, 20, 5, 20, 5, 9, fill=color, width=w)
        elif kind == "cube":
            canvas.create_polygon(13, 4, 21, 8, 13, 12, 5, 8, outline=color, fill="", width=w)
            canvas.create_line(5, 8, 5, 17, 13, 22, 21, 17, 21, 8, fill=color, width=w)
            canvas.create_line(13, 12, 13, 22, fill=color, width=w)
        elif kind == "gear":
            canvas.create_oval(8, 8, 18, 18, outline=color, width=w)
            for x1, y1, x2, y2 in [(13, 3, 13, 7), (13, 19, 13, 23), (3, 13, 7, 13), (19, 13, 23, 13),
                                   (6, 6, 8, 8), (18, 18, 20, 20), (18, 8, 20, 6), (6, 20, 8, 18)]:
                canvas.create_line(x1, y1, x2, y2, fill=color, width=w)
        elif kind == "refresh":
            canvas.create_arc(6, 6, 21, 21, start=35, extent=270, style="arc", outline=color, width=w)
            canvas.create_line(19, 7, 21, 12, 16, 11, fill=color, width=w)
        else:
            canvas.create_rectangle(8, 8, 18, 18, outline=color, width=w)
        return canvas

    def _build_action_buttons(self):
        btn_frame = tk.Frame(self._overview_page, bg=C["bg"])
        self._btn_frame = btn_frame
        btn_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(0, 12))

        row = tk.Frame(btn_frame, bg=C["bg"])
        row.pack(fill="x")
        for col in range(4):
            row.columnconfigure(col, weight=1, uniform="action_buttons")

        def action(col, text, icon, command, warn=False):
            card = self._card(row)
            card.configure(height=LAYOUT["actions_h"])
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 3 else 6))
            card.pack_propagate(False)
            self._action_icon(card, icon).pack(side="left", padx=(14, 8), pady=8)
            btn = tk.Button(
                card,
                text=text,
                font=F["bold"],
                bg=C["card"],
                fg=C["warn"] if warn else C["text"],
                activebackground=C["hover"],
                activeforeground=C["warn"] if warn else C["text"],
                relief="flat",
                bd=0,
                cursor="hand2",
                command=command,
            )
            btn.pack(side="left", fill="x", expand=True, pady=8)
            return card

        action(0, "打开输出目录", "folder", self._open_outputs)
        action(1, "打开模型目录", "cube", self._open_models)
        action(2, "安装运行环境", "gear", self._open_runtime_maintenance, warn=True)
        action(3, "重启后台", "refresh", self._restart_backend, warn=True)

    # ══════════════════════════════════════════════════════
    # runtime 缺失面板
    # ══════════════════════════════════════════════════════
    def _show_runtime_missing(self, allow_back: bool = False):
        """显示运行环境未安装面板（替换主内容区）"""
        # 隐藏正常内容
        for w in [self._info_frame, getattr(self, "_main_area", None), self._btn_frame]:
            if not w:
                continue
            if w.winfo_ismapped():
                w.pack_forget()
        if self._prog_frame.winfo_ismapped():
            pass

        if hasattr(self, '_runtime_missing_frame') and self._runtime_missing_frame:
            try:
                self._runtime_missing_frame.destroy()
            except Exception:
                pass
            self._runtime_missing_frame = None

        frame = tk.Frame(self._overview_page, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(8, 12))

        content = self._card(frame, fill="both", expand=True)

        tk.Label(content, text="◇", font=("Microsoft YaHei UI", 26),
                 fg=C["text2"], bg=C["card"]).pack(pady=(46, 8))
        tk.Label(content, text="运行环境未安装", font=("Microsoft YaHei UI", 18, "bold"),
                 fg=C["text"], bg=C["card"]).pack()

        tk.Label(
            content,
            text=f"需要安装 {RUNTIME_PACKAGE_NAME}。点击一键修复后，客户端会自动拉取、校验并安装；仅在网络失败时提示手动下载。",
            font=F["normal"],
            fg=C["text2"],
            bg=C["card"],
            justify="center",
            wraplength=620,
        ).pack(pady=(8, 30))

        primary_row = tk.Frame(content, bg=C["card"])
        primary_row.pack(fill="x", padx=28, pady=(0, 24))

        def install_card(index, icon, title, desc, button, command):
            card = self._card(primary_row)
            card.pack(side="left", fill="both", expand=True, padx=8)
            tk.Label(card, text=icon, font=("Microsoft YaHei UI", 34), fg=C["text2"], bg=C["card"]).pack(pady=(20, 8))
            tk.Label(card, text=f"{index}  {title}", font=F["bold"], fg=C["primary"], bg=C["card"]).pack()
            tk.Label(card, text=desc, font=F["small"], fg=C["text2"], bg=C["card"],
                     justify="center", wraplength=190).pack(padx=14, pady=(8, 16))
            self._button(card, button, command, "plain").pack(pady=(0, 20), ipadx=22, ipady=5)

        install_card("1", "□", "选择 7z 环境包", "从本地选择已经下载好的 7z 运行环境包进行安装。", "选择文件", self._select_runtime)
        install_card("2", "☁", "在线一键修复", "自动拉取、校验并安装官方运行环境；网络失败时提供手动下载地址。", "一键修复", self._install_runtime_from_mirror)
        install_card("3", "▣", "安装同目录环境包", "自动查找客户端同目录下的 7z 环境包并安装。", "开始安装", self._install_local_runtime)

        secondary_row = tk.Frame(content, bg=C["card"])
        secondary_row.pack(fill="x", padx=26, pady=(0, 16))

        self._button(secondary_row, "打开安装说明", self._open_install_guide, "plain").pack(side="left", ipadx=12, ipady=5)

        self._button(secondary_row, "返回主界面", self._show_main_content, "plain").pack(side="right", ipadx=18, ipady=5)

        self._runtime_missing_frame = frame

    def _hide_runtime_missing(self):
        if hasattr(self, '_runtime_missing_frame') and self._runtime_missing_frame:
            self._runtime_missing_frame.pack_forget()

    def _show_main_content(self):
        self._hide_runtime_missing()
        if not self._info_frame.winfo_ismapped():
            self._info_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(8, 10))
        if hasattr(self, "_main_area") and not self._main_area.winfo_ismapped():
            self._main_area.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(0, 12))
        if not self._btn_frame.winfo_ismapped():
            self._btn_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(0, 12))

    def _install_local_runtime(self):
        """安装同目录下的 runtime 包"""
        # 查找同目录的 7z 包
        preferred = BASE_DIR / RUNTIME_PACKAGE_NAME
        candidates = [preferred] if preferred.exists() else list(BASE_DIR.glob("runtime-*.7z"))
        if not candidates:
            r = messagebox.askyesno(
                "未找到环境包",
                "当前目录未找到 runtime-*.7z 文件。\n\n是否打开文件选择对话框？")
            if r:
                self._select_runtime()
            return

        pkg = candidates[0]
        self._extract_runtime(pkg)

    def _runtime_mirror_url(self) -> str:
        return resolve_runtime_download_url(_load_local_config())

    def _install_runtime_from_mirror(self):
        """Download the configured runtime package, falling back to manual recovery."""
        repairing = _runtime_has_package_files()
        if repairing and not messagebox.askyesno(
            "安装或修复运行环境",
            "安装过程中会暂时停止 ComfyUI、API 和公网连接。完成准备后客户端将退出，"
            "请手动重新打开，重启后生效。\n\n"
            "模型、工作流和生成结果不会被删除。是否继续？",
            parent=self,
        ):
            return

        try:
            url = self._runtime_mirror_url()
        except ValueError as exc:
            self._show_runtime_download_fallback(f"下载地址配置无效：{exc}")
            return
        if url:
            self._download_runtime(url, repair_confirmed=repairing)
            return

        self._show_runtime_download_fallback("当前版本没有配置运行环境包下载地址。")

    def _show_runtime_download_fallback(self, error_message: str):
        """Offer browser download alternatives after automatic acquisition fails."""
        reason = " ".join(str(error_message or "网络连接异常").split())
        if len(reason) > 180:
            reason = f"{reason[:177]}..."

        popup = tk.Toplevel(self)
        popup.title("自动修复失败")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        self._center_popup(popup, 620, 460)

        panel = self._card(popup, fill="both", expand=True, padx=16, pady=16)
        tk.Label(
            panel,
            text="自动拉取失败，请手动下载",
            font=F["title"],
            fg=C["text"],
            bg=C["card"],
        ).pack(anchor="w", padx=22, pady=(22, 8))
        tk.Label(
            panel,
            text=(
                "自动下载线路不可用。可点击下方网盘按钮，在浏览器中下载环境包；"
                f"下载 {RUNTIME_PACKAGE_NAME}。\n"
                "下载完成后回到客户端，选择本地环境包继续安装或修复。"
            ),
            font=F["normal"],
            fg=C["text2"],
            bg=C["card"],
            justify="left",
            wraplength=550,
        ).pack(anchor="w", padx=22, pady=(0, 14))

        tk.Label(
            panel,
            text=f"失败原因：{reason}",
            font=F["small"],
            fg=C["error"],
            bg=C["card"],
            justify="left",
            wraplength=550,
        ).pack(anchor="w", padx=22, pady=(0, 10))

        for name, url, code in (
            ("123 云盘", "https://1817692300.share.123pan.cn/123pan/lZtTjv-jOG4h?pwd=KkJt#", "KkJt"),
            ("百度网盘", "https://pan.baidu.com/s/1Tt5khCdRs13nymP9hHCn7Q", "wdmt"),
        ):
            row = tk.Frame(panel, bg=C["card"])
            row.pack(fill="x", padx=22, pady=(0, 10))
            self._button(
                row, f"打开 {name}",
                lambda u=url: webbrowser.open(u), "primary", width=130,
            ).pack(side="left")
            tk.Label(
                row, text=f"提取码：{code}", font=F["normal"],
                fg=C["text"], bg=C["card"],
            ).pack(side="left", padx=12)
            self._button(
                row, "复制提取码", lambda c=code: self._copy(c),
                "plain", width=100,
            ).pack(side="right")

        url_row = tk.Frame(panel, bg=C["card"])
        url_row.pack(fill="x", padx=22)
        url_value = tk.StringVar(value=PROJECT_HOMEPAGE_URL)
        url_entry = tk.Entry(
            url_row,
            textvariable=url_value,
            state="readonly",
            readonlybackground=C["entry"],
            fg=C["text"],
            font=("Consolas", 10),
            relief="solid",
            bd=1,
        )
        url_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self._button(
            url_row,
            "复制地址",
            lambda: self._copy(PROJECT_HOMEPAGE_URL),
            "primary",
            width=88,
        ).pack(side="left", padx=(10, 0))

        tk.Label(
            panel,
            text="提示：环境包体积较大，浏览器下载完成后不要改名。",
            font=F["small"],
            fg=C["warn"],
            bg=C["card"],
        ).pack(anchor="w", padx=22, pady=(12, 16))

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=22, pady=(0, 20))
        self._button(
            actions,
            "打开 GitHub",
            lambda: webbrowser.open(PROJECT_HOMEPAGE_URL),
            "primary",
            width=102,
        ).pack(side="left")
        self._button(
            actions,
            "选择本地环境包",
            lambda: (popup.destroy(), self._select_runtime()),
            "plain",
            width=126,
        ).pack(side="left", padx=(8, 0))
        self._button(actions, "关闭", popup.destroy, "plain", width=72).pack(side="right")

    def _create_runtime_progress_dialog(
        self,
        *,
        title: str,
        heading: str,
        stage: str,
        detail: str,
        background_action_text: str = "后台修复",
    ) -> dict:
        """Build a branded maintenance dialog that can collapse into the app."""
        popup = tk.Toplevel(self)
        popup.title(title)
        popup.geometry("540x270")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        self._center_popup(popup, 540, 270)

        panel = RoundedFrame(
            popup,
            radius=14,
            fill=C["card"],
            outline=C["border2"],
        )
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        content = tk.Frame(panel, bg=C["card"])
        content.pack(fill="both", expand=True, padx=24, pady=20)

        tk.Label(
            content,
            text=heading,
            font=F["title"],
            fg=C["text"],
            bg=C["card"],
        ).pack(anchor="w")

        stage_var = tk.StringVar(value=stage)
        stage_label = tk.Label(
            content,
            textvariable=stage_var,
            font=F["bold"],
            fg=C["text2"],
            bg=C["card"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        stage_label.pack(fill="x", pady=(15, 8))

        progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(
            content,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=progress_var,
            style="Progress.Horizontal.TProgressbar",
        )
        progress.pack(fill="x")

        detail_var = tk.StringVar(value=detail)
        detail_label = tk.Label(
            content,
            textvariable=detail_var,
            font=F["small"],
            fg=C["muted"],
            bg=C["card"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        detail_label.pack(fill="x", pady=(9, 0))

        dialog = {
            "popup": popup,
            "panel": panel,
            "content": content,
            "heading": heading,
            "progress": progress,
            "progress_var": progress_var,
            "stage_var": stage_var,
            "stage_label": stage_label,
            "detail_var": detail_var,
            "detail_label": detail_label,
            "background_action_text": background_action_text,
            "background_card": None,
        }
        self._button(
            content,
            background_action_text,
            lambda: self._minimize_maintenance_dialog(dialog),
            "plain",
            width=96,
        ).pack(anchor="e", pady=(12, 0))
        popup.protocol(
            "WM_DELETE_WINDOW",
            lambda: self._minimize_maintenance_dialog(dialog),
        )
        return dialog

    def _minimize_maintenance_dialog(self, dialog: dict):
        """Collapse a long-running maintenance dialog into the app's lower-right corner."""
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                return
            popup.grab_release()
            popup.withdraw()
        except Exception:
            return
        existing = dialog.get("background_card")
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                return
        except Exception:
            pass
        host = getattr(self, "_content_root", self)
        card = RoundedFrame(
            host,
            radius=12,
            fill=C["card"],
            outline=C["primary"],
        )
        card.place(relx=1.0, rely=1.0, x=-18, y=-(LAYOUT["footer_h"] + 14), anchor="se", width=390, height=82)
        card.lift()
        inner = tk.Frame(card, bg=C["card"])
        inner.pack(fill="both", expand=True, padx=14, pady=10)
        top = tk.Frame(inner, bg=C["card"])
        top.pack(fill="x")
        tk.Label(
            top,
            textvariable=dialog["stage_var"],
            font=F["bold"],
            fg=C["text"],
            bg=C["card"],
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self._button(
            top,
            "查看",
            lambda: self._restore_maintenance_dialog(dialog),
            "plain",
            width=58,
        ).pack(side="right")
        ttk.Progressbar(
            inner,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=dialog["progress_var"],
            style="Progress.Horizontal.TProgressbar",
        ).pack(fill="x", pady=(9, 0))
        dialog["background_card"] = card
        try:
            self._footer_label.config(text="  维护任务正在后台继续，点击右下角可查看进度")
        except Exception:
            pass

    def _restore_maintenance_dialog(self, dialog: dict):
        card = dialog.get("background_card")
        try:
            if card is not None and card.winfo_exists():
                card.destroy()
        except Exception:
            pass
        dialog["background_card"] = None
        notice = dialog.get("restart_notice")
        try:
            if notice is not None and notice.winfo_exists():
                notice.destroy()
                content = dialog.get("content")
                if content is not None:
                    content.pack(fill="both", expand=True, padx=24, pady=20)
        except Exception:
            pass
        dialog["restart_notice"] = None
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                return
            popup.deiconify()
            popup.lift()
            popup.focus_force()
            popup.grab_set()
        except Exception:
            pass

    def _close_maintenance_dialog(self, dialog: dict):
        self._stop_runtime_progress_activity(dialog)
        card = dialog.get("background_card")
        popup = dialog.get("popup")
        for widget in (card, popup):
            try:
                if widget is not None and widget.winfo_exists():
                    if widget is popup:
                        widget.grab_release()
                    widget.destroy()
            except Exception:
                pass
        dialog["background_card"] = None

    def _set_runtime_progress(
        self,
        dialog: dict,
        percent: float,
        stage: str,
        detail: str,
        *,
        error: bool = False,
    ):
        """Update runtime setup progress without exposing command output."""
        if error:
            self._stop_runtime_progress_activity(dialog)
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                return
        except Exception:
            return

        value = max(0.0, min(100.0, float(percent or 0)))
        dialog["progress_var"].set(value)
        dialog["stage_var"].set(str(stage or ""))
        dialog["detail_var"].set(str(detail or ""))
        dialog["stage_label"].config(fg=C["error"] if error else C["text2"])
        dialog["detail_label"].config(fg=C["text2"] if error else C["muted"])

    @staticmethod
    def _format_runtime_elapsed(seconds: float) -> str:
        total = max(0, int(seconds or 0))
        minutes, remaining_seconds = divmod(total, 60)
        return f"{minutes:02d}:{remaining_seconds:02d}"

    def _stop_runtime_progress_activity(self, dialog: dict):
        dialog["_activity_token"] = None
        callback_id = dialog.pop("_activity_after_id", None)
        if callback_id is not None:
            try:
                self.after_cancel(callback_id)
            except Exception:
                pass
        progress = dialog.get("progress")
        if progress is not None:
            try:
                progress.stop()
                progress.configure(mode="determinate")
            except Exception:
                pass
        for key in (
            "_activity_started_at",
            "_activity_base_detail",
            "_activity_status",
            "_activity_progress_queue",
            "_activity_percent",
            "_activity_last_progress_at",
        ):
            dialog.pop(key, None)

    def _refresh_runtime_stage_activity(self, dialog: dict, token: object) -> bool:
        if dialog.get("_activity_token") != token:
            return False
        if self._shutting_down or self._runtime_maintenance_active():
            return False
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                self._stop_runtime_progress_activity(dialog)
                return False
        except Exception:
            self._stop_runtime_progress_activity(dialog)
            return False

        now = time.monotonic()
        started_at = float(dialog.get("_activity_started_at", now))
        elapsed = self._format_runtime_elapsed(now - started_at)
        base_detail = str(dialog.get("_activity_base_detail") or "")
        status = str(dialog.get("_activity_status") or "处理中")
        dialog["detail_var"].set(f"{base_detail}\n{status} · 已用时 {elapsed}")
        dialog["detail_label"].config(fg=C["muted"])
        return True

    def _start_runtime_stage_activity(
        self,
        dialog: dict,
        stage_name: str,
        *,
        token: object | None = None,
    ) -> object:
        activity_token = token or uuid.uuid4().hex
        dialog["_activity_token"] = activity_token
        dialog["_activity_started_at"] = time.monotonic()
        dialog["_activity_base_detail"] = RUNTIME_INSTALL_STAGES[stage_name][2]
        dialog["_activity_status"] = RUNTIME_LONG_STAGE_STATUS[stage_name]
        progress = dialog.get("progress")
        if progress is not None:
            progress.configure(mode="indeterminate")
            progress.start(12)

        if not self._refresh_runtime_stage_activity(dialog, activity_token):
            return activity_token

        def tick():
            if dialog.get("_activity_token") != activity_token:
                return
            dialog["_activity_after_id"] = None
            if not self._refresh_runtime_stage_activity(dialog, activity_token):
                return
            if dialog.get("_activity_token") != activity_token:
                return
            try:
                dialog["_activity_after_id"] = self.after(1000, tick)
            except Exception:
                dialog["_activity_after_id"] = None

        try:
            dialog["_activity_after_id"] = self.after(1000, tick)
        except Exception:
            dialog["_activity_after_id"] = None
        return activity_token

    def _refresh_runtime_extract_activity(self, dialog: dict, token: object) -> bool:
        if dialog.get("_activity_token") != token:
            return False
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                self._stop_runtime_progress_activity(dialog)
                return False
        except Exception:
            self._stop_runtime_progress_activity(dialog)
            return False

        progress_updates = dialog.get("_activity_progress_queue")
        latest_percent = None
        if progress_updates is not None:
            while True:
                try:
                    latest_percent = progress_updates.get_nowait()
                except queue.Empty:
                    break
            if latest_percent is not None:
                self._report_runtime_extract_progress(
                    dialog,
                    token,
                    latest_percent,
                    refresh=False,
                )
                if dialog.get("_activity_token") != token:
                    return False

        now = time.monotonic()
        started_at = float(dialog.get("_activity_started_at", now))
        elapsed = self._format_runtime_elapsed(now - started_at)
        percent = dialog.get("_activity_percent")
        try:
            process_running = self._process_supervisor.is_running("runtime-install")
        except Exception:
            process_running = False

        if percent is None:
            status = "解压进程运行中" if process_running else "正在启动解压进程"
        else:
            status = f"解压进度 {int(percent)}%"
            last_progress_at = float(dialog.get("_activity_last_progress_at", now))
            if process_running and now - last_progress_at >= 90:
                status += " · 仍在处理，磁盘较慢时需要更久"
            elif not process_running and int(percent) < 100:
                status += " · 正在完成文件写入"

        base_detail = str(dialog.get("_activity_base_detail") or "")
        dialog["detail_var"].set(f"{base_detail}\n{status} · 已用时 {elapsed}")
        dialog["detail_label"].config(fg=C["muted"])
        return True

    def _start_runtime_extract_activity(
        self,
        dialog: dict,
        *,
        token: object | None = None,
        progress_queue=None,
    ) -> object:
        activity_token = token or uuid.uuid4().hex
        dialog["_activity_token"] = activity_token
        dialog["_activity_started_at"] = time.monotonic()
        dialog["_activity_base_detail"] = RUNTIME_INSTALL_STAGES["extract"][2]
        dialog["_activity_progress_queue"] = progress_queue or queue.SimpleQueue()
        dialog["_activity_percent"] = None
        dialog["_activity_last_progress_at"] = dialog["_activity_started_at"]
        progress = dialog.get("progress")
        if progress is not None:
            progress.configure(mode="indeterminate")
            progress.start(12)

        if not self._refresh_runtime_extract_activity(dialog, activity_token):
            return activity_token

        def tick():
            if dialog.get("_activity_token") != activity_token:
                return
            dialog["_activity_after_id"] = None
            if not self._refresh_runtime_extract_activity(dialog, activity_token):
                return
            if dialog.get("_activity_token") != activity_token:
                return
            try:
                dialog["_activity_after_id"] = self.after(1000, tick)
            except Exception:
                dialog["_activity_after_id"] = None

        try:
            dialog["_activity_after_id"] = self.after(1000, tick)
        except Exception:
            dialog["_activity_after_id"] = None
        return activity_token

    def _report_runtime_extract_progress(
        self,
        dialog: dict,
        token: object,
        percent: int,
        *,
        refresh: bool = True,
    ):
        if dialog.get("_activity_token") != token:
            return
        popup = dialog.get("popup")
        try:
            if popup is None or not popup.winfo_exists():
                self._stop_runtime_progress_activity(dialog)
                return
        except Exception:
            self._stop_runtime_progress_activity(dialog)
            return
        value = max(0, min(100, int(percent)))
        previous = dialog.get("_activity_percent")
        dialog["_activity_percent"] = value
        if previous != value:
            dialog["_activity_last_progress_at"] = time.monotonic()
        progress = dialog.get("progress")
        if progress is not None:
            progress.stop()
            progress.configure(mode="determinate")
        extract_start = RUNTIME_INSTALL_STAGES["extract"][0]
        validate_start = RUNTIME_INSTALL_STAGES["validate"][0]
        overall = extract_start + ((validate_start - extract_start) * value / 100)
        dialog["progress_var"].set(overall)
        if refresh:
            self._refresh_runtime_extract_activity(dialog, token)

    def _set_runtime_install_stage(
        self,
        dialog: dict,
        stage_name: str,
        *,
        activity_token: object | None = None,
        activity_queue=None,
    ):
        self._stop_runtime_progress_activity(dialog)
        percent, stage, detail = RUNTIME_INSTALL_STAGES[stage_name]
        self._set_runtime_progress(dialog, percent, stage, detail)
        if stage_name == "extract":
            return self._start_runtime_extract_activity(
                dialog,
                token=activity_token,
                progress_queue=activity_queue,
            )
        if stage_name in RUNTIME_LONG_STAGE_STATUS:
            return self._start_runtime_stage_activity(
                dialog,
                stage_name,
                token=activity_token,
            )
        return None

    def _download_runtime(self, url: str, repair_confirmed: bool = False):
        dialog = self._create_runtime_progress_dialog(
            title="安装运行环境",
            heading="正在准备运行环境",
            stage="连接下载源",
            detail="正在获取环境包信息",
        )
        popup = dialog["popup"]

        def show_download_error(message: str):
            self._close_maintenance_dialog(dialog)
            self._show_runtime_download_fallback(message)

        def _do_download():
            try:
                cache_dir = BASE_DIR / "cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                target = cache_dir / RUNTIME_PACKAGE_NAME
                sidecar = Path(f"{target}.sha256")

                if target.is_file():
                    self.after(
                        0,
                        lambda: self._set_runtime_progress(
                            dialog,
                            0,
                            "校验本地缓存",
                            "检测到已下载的环境包，正在校验完整性",
                        ),
                    )
                    cached_valid, _expected, _actual = verify_runtime_package(
                        target,
                        sidecar if sidecar.is_file() else None,
                    )
                    if cached_valid:
                        self.after(
                            0,
                            lambda: self._set_runtime_progress(
                                dialog,
                                100,
                                "缓存校验完成",
                                "环境包完整，跳过重复下载",
                            ),
                        )
                        self.after(0, lambda: self._close_maintenance_dialog(dialog))
                        self.after(
                            100,
                            lambda: self._extract_runtime(
                                target,
                                repair_confirmed=repair_confirmed,
                            ),
                        )
                        return

                def report_progress(percent, stage, detail):
                    if not self._shutting_down:
                        self.after(0, lambda p=percent, s=stage, d=detail:
                                   self._set_runtime_progress(dialog, p, s, d))

                download_runtime_package(
                    url, target,
                    size=RUNTIME_PACKAGE_SIZE,
                    sha256=RUNTIME_PACKAGE_SHA256,
                    open_response=lambda request, timeout: _open_download_request(
                        request, timeout=timeout,
                        allowed_suffixes=RUNTIME_DOWNLOAD_REDIRECT_SUFFIXES,
                    ),
                    progress=report_progress,
                )
                sidecar.unlink(missing_ok=True)
                self.after(
                    0,
                    lambda: self._set_runtime_progress(
                        dialog,
                        100,
                        "下载完成",
                        "正在准备校验并安装环境包",
                    ),
                )

                self.after(0, lambda: self._close_maintenance_dialog(dialog))
                self.after(100, lambda: self._extract_runtime(target, repair_confirmed=repair_confirmed))
            except Exception as ex:
                if not self._shutting_down:
                    self.after(0, lambda e=str(ex): show_download_error(e))

        threading.Thread(target=_do_download, daemon=True).start()

    def _select_runtime(self):
        """选择环境包"""
        path = filedialog.askopenfilename(
            title="选择运行环境包",
            filetypes=[("7z 压缩包", "*.7z"), ("所有文件", "*.*")])
        if path:
            self._extract_runtime(Path(path))

    def _extract_runtime(self, pkg_path: Path, repair_confirmed: bool = False):
        """解压 runtime 包"""
        if _runtime_has_package_files() and not repair_confirmed:
            if not messagebox.askyesno(
                "安装或修复运行环境",
                "安装过程中会暂时停止 ComfyUI、API 和公网连接。完成准备后客户端将退出，"
                "请手动重新打开，重启后生效。\n\n"
                "模型、工作流和生成结果不会被删除。是否继续？",
                parent=self,
            ):
                return
        dialog = self._create_runtime_progress_dialog(
            title="安装运行环境",
            heading="正在安装运行环境",
            stage=RUNTIME_INSTALL_STAGES["prepare"][1],
            detail=RUNTIME_INSTALL_STAGES["prepare"][2],
        )
        popup = dialog["popup"]
        self._set_runtime_install_stage(dialog, "prepare")

        def show_install_error(message: str):
            try:
                self._restore_maintenance_dialog(dialog)
                self._set_runtime_progress(
                    dialog,
                    dialog["progress_var"].get(),
                    "安装未完成",
                    str(message),
                    error=True,
                )
                popup.protocol(
                    "WM_DELETE_WINDOW",
                    lambda: self._close_maintenance_dialog(dialog),
                )
            except Exception:
                pass
            self._environment_status = {
                "package_ready": _check_runtime_exists(),
                "ready": False,
                "message": str(message),
            }
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.refresh(self._last_health)

        def set_install_stage(
            stage_name: str,
            activity_token: object | None = None,
            activity_queue=None,
        ):
            self.after(
                0,
                lambda name=stage_name, token=activity_token, updates=activity_queue: self._set_runtime_install_stage(
                    dialog,
                    name,
                    activity_token=token,
                    activity_queue=updates,
                ),
            )

        def _do_extract():
            maintenance_started = False
            handoff_started = False
            running_before: set[str] = set()
            operation_id = uuid.uuid4().hex
            staging_dir = BASE_DIR / f".runtime-install-staging-{operation_id}"
            try:
                package = Path(pkg_path)
                if not package.is_file():
                    raise FileNotFoundError(f"环境包不存在: {package}")

                sidecar = Path(f"{package}.sha256")
                set_install_stage("verify")
                valid, expected, actual = verify_runtime_package(
                    package,
                    sidecar if sidecar.is_file() else None,
                )
                if not valid:
                    raise RuntimeError(
                        f"SHA256 校验失败（期望 {expected[:12]}...，实际 {actual[:12]}...）"
                    )

                extractor = find_extractor(BASE_DIR)
                if not extractor:
                    raise RuntimeError("未找到可用的 7-Zip 或 Windows tar.exe 解压工具")

                set_install_stage("inspect")
                list_options = {
                    "cwd": str(BASE_DIR),
                    "capture_output": True,
                    "text": True,
                    "timeout": 180,
                }
                if extractor[0] == "7z":
                    list_options.update(encoding="utf-8", errors="strict")
                list_result = self._process_supervisor.run(
                    "runtime-install",
                    archive_list_command(extractor, package),
                    **list_options,
                )
                if list_result.returncode != 0:
                    raise RuntimeError(list_result.stderr.strip() or "无法读取环境包目录")
                members = parse_archive_members(extractor, list_result.stdout)
                missing = missing_archive_entries(members)
                invalid = invalid_archive_entries(members)
                if missing:
                    raise RuntimeError(f"环境包目录结构不完整，缺少: {', '.join(missing)}")
                if invalid:
                    raise RuntimeError(f"环境包包含不允许的路径: {invalid[0]}")
                if self._shutting_down:
                    return

                staging_dir.mkdir(parents=True, exist_ok=False)
                extract_activity_token = uuid.uuid4().hex
                extract_progress_updates = queue.SimpleQueue()
                set_install_stage(
                    "extract",
                    extract_activity_token,
                    extract_progress_updates,
                )
                extract_command = archive_extract_command(extractor, package, staging_dir)
                try:
                    if extractor[0] == "7z":
                        progress_parser = SevenZipProgressParser()

                        def report_extract_output(chunk: bytes):
                            for progress_percent in progress_parser.feed(chunk):
                                if self._shutting_down:
                                    return
                                extract_progress_updates.put(progress_percent)

                        result = self._process_supervisor.run_observed(
                            "runtime-install",
                            extract_command,
                            cwd=str(BASE_DIR),
                            timeout=3600,
                            on_stdout=report_extract_output,
                        )
                        for progress_percent in progress_parser.finish():
                            extract_progress_updates.put(progress_percent)
                    else:
                        result = self._process_supervisor.run(
                            "runtime-install",
                            extract_command,
                            cwd=str(BASE_DIR),
                            capture_output=True,
                            text=True,
                            timeout=3600,
                        )
                except subprocess.TimeoutExpired as exc:
                    try:
                        extractor_still_running = self._process_supervisor.is_running(
                            "runtime-install"
                        )
                    except Exception:
                        extractor_still_running = True
                    if extractor_still_running:
                        raise RuntimeError(
                            "环境包解压超时，但解压进程无法安全停止；"
                            "客户端已保留暂存文件，请退出客户端后重试。"
                        ) from exc
                    raise RuntimeError(
                        "环境包解压超过 60 分钟，已安全停止；"
                        "请检查磁盘空间、压缩包完整性或杀毒软件扫描。"
                    ) from exc
                if result.returncode != 0:
                    stderr = result.stderr
                    stdout = result.stdout
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(
                            "utf-8",
                            errors="replace",
                        )
                    if isinstance(stdout, bytes):
                        stdout = stdout.decode(
                            "utf-8",
                            errors="replace",
                        )
                    raise RuntimeError(
                        str(stderr).strip()
                        or str(stdout).strip()
                        or "环境包解压失败"
                    )
                set_install_stage("validate")
                validate_staged_runtime(staging_dir)

                if not self._show_runtime_manual_restart_notice(dialog):
                    if self._shutting_down:
                        return
                    raise RuntimeError("未能显示手动重启提示，运行环境尚未切换")

                set_install_stage("stop_services")
                maintenance_started, running_before, stop_error = self._begin_runtime_maintenance()
                if not maintenance_started:
                    raise RuntimeError(stop_error or "无法开始运行环境维护")
                if stop_error:
                    raise RuntimeError(f"后台服务未能安全停止：{stop_error}")

                set_install_stage("apply")
                self._runtime_update_helper_proc = launch_runtime_update(
                    BASE_DIR,
                    staging_dir,
                    parent_pid=os.getpid(),
                )
                handoff_started = True
                maintenance_started = False
                self.after(0, lambda: self._exit_for_runtime_update(popup))
            except Exception as e:
                if maintenance_started:
                    can_restore = bool(running_before) and _check_runtime_exists()
                    self._end_runtime_maintenance(restart=can_restore)
                    maintenance_started = False
                if not self._shutting_down:
                    self.after(0, lambda error=str(e): show_install_error(error))
            finally:
                if maintenance_started:
                    can_restore = bool(running_before) and _check_runtime_exists()
                    self._end_runtime_maintenance(restart=can_restore)
                if not handoff_started and staging_dir.exists():
                    try:
                        extractor_still_running = self._process_supervisor.is_running(
                            "runtime-install"
                        )
                    except Exception:
                        extractor_still_running = True
                    if not extractor_still_running:
                        shutil.rmtree(staging_dir, ignore_errors=True)

        threading.Thread(target=_do_extract, daemon=True).start()

    def _show_manual_restart_notice(self, dialog: dict, *, component: str) -> bool:
        """Show a branded, blocking handoff notice without using a system message box."""
        completed = threading.Event()
        state = {"shown": False}

        def show_notice():
            try:
                if self._shutting_down:
                    return
                self._restore_maintenance_dialog(dialog)
                popup = dialog.get("popup")
                content = dialog.get("content")
                if popup is None or content is None:
                    return
                content.pack_forget()
                old_notice = dialog.get("restart_notice")
                try:
                    if old_notice is not None and old_notice.winfo_exists():
                        old_notice.destroy()
                except Exception:
                    pass
                notice = tk.Frame(dialog["panel"], bg=C["card"])
                notice.pack(fill="both", expand=True, padx=28, pady=24)
                dialog["restart_notice"] = notice
                action = "安装" if component == "运行环境" else "更新"
                popup.title(f"退出后完成{component}{action}")
                tk.Label(
                    notice,
                    text=f"退出后完成{component}{action}",
                    font=F["title"],
                    fg=C["text"],
                    bg=C["card"],
                ).pack(anchor="w")
                tk.Label(
                    notice,
                    text=(
                        f"{component}{action}文件已经下载、校验并准备就绪。\n"
                        "最后的文件切换必须在客户端关闭后进行。\n"
                        "程序不会自动重新打开；稍等片刻后，请由你手动打开客户端。\n"
                        "重新打开后会直接检查环境，不会重复显示完成提示。"
                    ),
                    font=F["body"],
                    fg=C["text2"],
                    bg=C["card"],
                    justify="left",
                    anchor="w",
                    wraplength=450,
                ).pack(fill="x", pady=(18, 20))
                status_label = tk.Label(
                    notice,
                    text="点击下方按钮后，客户端会先关闭后台服务，再安全退出。",
                    font=F["small"],
                    fg=C["muted"],
                    bg=C["card"],
                    justify="left",
                    anchor="w",
                    wraplength=450,
                )
                status_label.pack(fill="x", pady=(0, 12))

                def accept():
                    if completed.is_set():
                        return
                    state["shown"] = True
                    action_button.config(
                        state="disabled",
                        text=f"正在退出并完成{action}…",
                    )
                    status_label.config(
                        text="正在关闭后台服务并启动安装助手，请稍候…",
                        fg=C["primary"],
                    )
                    try:
                        popup.protocol("WM_DELETE_WINDOW", lambda: None)
                        popup.update_idletasks()
                    except Exception:
                        pass
                    completed.set()

                action_button = self._button(
                    notice,
                    f"退出并完成{action}",
                    accept,
                    "primary",
                    width=142,
                )
                action_button.pack(anchor="e")
                # This handoff may appear above the model download window.
                # Release the application-wide grab so sibling dialogs cannot
                # leave the confirmation inaccessible on Windows.
                grabbed = popup.grab_current()
                if grabbed is not None:
                    grabbed.grab_release()
                # The progress window is only 270px high. Measure the complete
                # notice (including its action) before reusing that window.
                popup.update_idletasks()
                self._center_popup(
                    popup,
                    max(540, notice.winfo_reqwidth() + 92),
                    max(360, notice.winfo_reqheight() + 84),
                )
                popup.resizable(True, True)
                popup.protocol("WM_DELETE_WINDOW", accept)
                popup.lift()
                popup.focus_force()
            except Exception:
                state["shown"] = False
                completed.set()
            finally:
                if self._shutting_down:
                    completed.set()

        try:
            self.after(0, show_notice)
        except Exception:
            return False
        while not completed.wait(0.1):
            if self._shutting_down:
                return False
        return bool(state["shown"])

    def _show_runtime_manual_restart_notice(self, dialog: dict) -> bool:
        return self._show_manual_restart_notice(dialog, component="运行环境")

    def _write_runtime_restart_ack(self) -> bool:
        """Confirm that the replacement GUI reached its first Tk event-loop turn."""
        operation_id = str(
            os.environ.get("LINGJING_RUNTIME_UPDATE_OPERATION_ID") or ""
        ).strip().lower()
        if not _RUNTIME_UPDATE_OPERATION_ID_RE.fullmatch(operation_id):
            return False
        result = self.__dict__.get("_pending_runtime_update_result")
        if not isinstance(result, dict):
            return False
        if str(result.get("operation_id") or "").strip().lower() != operation_id:
            return False

        runtime_dir = BASE_DIR / "runtime"
        ack_path = runtime_dir / f"runtime-update-ready-{operation_id}.json"
        temporary = runtime_dir / f"runtime-update-ready-{operation_id}.tmp"
        payload = {
            "schema_version": 1,
            "operation_id": operation_id,
            "pid": os.getpid(),
            "ready_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(ack_path)
            os.environ.pop("LINGJING_RUNTIME_UPDATE_OPERATION_ID", None)
            return True
        except OSError as exc:
            print(f"[RuntimeUpdate] failed to write restart acknowledgement: {exc}")
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _capture_startup_update_results(self):
        """Consume updater results before any backend can touch a damaged runtime."""
        try:
            recovery_result = recover_interrupted_update(BASE_DIR)
        except Exception as exc:
            recovery_result = {
                "status": "recovery_incomplete",
                "success": False,
                "message": f"无法检查上次中断的 ComfyUI 更新：{exc}",
            }
        try:
            runtime_result = consume_runtime_update_result(BASE_DIR)
        except Exception:
            runtime_result = None
        try:
            comfyui_result = consume_comfyui_update_result(BASE_DIR)
        except Exception:
            comfyui_result = None
        self._pending_comfyui_recovery_result = recovery_result
        self._pending_runtime_update_result = runtime_result
        self._pending_comfyui_update_result = comfyui_result
        self._startup_update_results_preconsumed = True
        runtime_record = runtime_result if isinstance(runtime_result, dict) else {}
        comfyui_record = comfyui_result if isinstance(comfyui_result, dict) else {}
        recovery_record = recovery_result if isinstance(recovery_result, dict) else {}
        runtime_code = str(runtime_record.get("result_code") or "")
        comfyui_status = str(comfyui_record.get("status") or "")
        recovery_status = str(recovery_record.get("status") or "")
        self._runtime_start_blocked = bool(
            runtime_code == "install_failed_rollback_incomplete"
            or comfyui_status == "rollback_incomplete"
            or recovery_status in {"recovery_incomplete", "recovery_in_progress"}
        )

    def _close_for_active_comfyui_update(self):
        """Release this temporary GUI without touching the detached updater."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._anim_running = False
        self._heartbeat_run = False
        self._poll_run = False
        _release_instance_lock()
        self._complete_destroy()

    def _show_active_comfyui_update_notice(self):
        """Explain the active detached update and let the user close explicitly."""
        popup = tk.Toplevel(self)
        popup.title("ComfyUI 正在更新")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        self._center_popup(popup, 520, 260)
        panel = RoundedFrame(popup, radius=14, fill=C["card"], outline=C["border2"])
        panel.pack(fill="both", expand=True, padx=18, pady=18)
        body = tk.Frame(panel, bg=C["card"])
        body.pack(fill="both", expand=True, padx=26, pady=22)
        tk.Label(
            body,
            text="ComfyUI 正在后台完成更新",
            font=F["title"],
            fg=C["text"],
            bg=C["card"],
        ).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "上一次更新仍在完成文件切换。请先退出当前客户端，"
                "稍等片刻后再手动打开；程序不会自动重新启动。"
            ),
            font=F["body"],
            fg=C["text2"],
            bg=C["card"],
            wraplength=430,
            justify="left",
        ).pack(fill="x", pady=(18, 22))
        self._button(
            body,
            "退出程序",
            self._close_for_active_comfyui_update,
            "primary",
            width=110,
        ).pack(anchor="e")
        popup.protocol("WM_DELETE_WINDOW", self._close_for_active_comfyui_update)

    def _show_comfyui_recovery_result(self):
        if not self.__dict__.get("_startup_update_results_preconsumed", False):
            return
        result = self.__dict__.pop("_pending_comfyui_recovery_result", None)
        if not isinstance(result, dict):
            return
        status = str(result.get("status") or "")
        if status == "no_recovery_needed":
            return
        if status == "recovery_in_progress":
            self._show_active_comfyui_update_notice()
            return
        message = str(result.get("message") or "").strip().replace("\x00", "")[:1200]
        if status in {"recovered", "completed"}:
            try:
                self._footer_label.config(text="  ComfyUI 已更新，环境检查正常")
            except Exception:
                pass
            return
        errors = result.get("errors") or []
        if not isinstance(errors, (list, tuple)):
            errors = [errors]
        details = "\n".join(str(item or "")[:320] for item in errors[:4] if item)
        text = message or "上次中断的 ComfyUI 更新未能完整恢复。"
        if details:
            text += f"\n\n详细信息：\n{details[:1000]}"
        text += "\n\n为避免启动损坏环境，后台服务未自动启动，请先修复运行环境。"
        messagebox.showerror("ComfyUI 自动恢复未完成", text, parent=self)

    def _take_startup_update_result(self, name: str):
        if self.__dict__.get("_startup_update_results_preconsumed", False):
            return self.__dict__.pop(f"_pending_{name}_update_result", None)
        consumer = (
            consume_runtime_update_result
            if name == "runtime"
            else consume_comfyui_update_result
        )
        return consumer(BASE_DIR)

    def _show_runtime_update_result(self):
        """Report the detached updater result once the restarted GUI is ready."""
        result = self._take_startup_update_result("runtime")
        if not result:
            return
        code = str(result.get("result_code") or "")
        detail = str(result.get("message") or "").strip()
        messages = {
            "installed": "运行环境安装完成。",
            "installed_restart_failed": "运行环境已安装；本次为手动启动，环境已经生效。",
            "preflight_failed": "运行环境安装未开始，更新助手检查失败。",
            "preflight_failed_restart_failed": (
                "运行环境安装未开始；请手动重新打开客户端。"
            ),
            "install_failed_rolled_back": "运行环境安装失败，旧环境已恢复。",
            "install_failed_rollback_incomplete": (
                "运行环境安装失败，自动回滚也未完成；环境备份已保留。"
                "为避免启动损坏环境，后台服务未自动启动，请先修复运行环境。"
            ),
        }
        message = messages.get(code, "运行环境更新已完成。")
        if detail and code != "installed":
            message = f"{message}\n\n详细信息：{detail}"
        if result.get("success") is True:
            try:
                self._footer_label.config(text="  运行环境已安装并通过检查")
            except Exception:
                pass
        else:
            messagebox.showerror("运行环境更新失败", message, parent=self)

    @staticmethod
    def _comfyui_update_result_warnings(result: dict) -> str:
        """Return a bounded, readable summary of post-transaction cleanup issues."""
        entries: list[str] = []
        fields = (
            ("清理提示", result.get("cleanup_warnings")),
            ("回滚提示", result.get("rollback_errors")),
            ("清单清理", result.get("manifest_cleanup_error")),
        )
        for label, value in fields:
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values[:4]:
                text = str(item or "").strip().replace("\x00", "")
                if text:
                    entries.append(f"{label}：{text[:360]}")
                if len(entries) >= 8:
                    break
            if len(entries) >= 8:
                break
        if not entries:
            return ""
        return "\n".join(entries)[:1800]

    def _show_comfyui_update_result(self):
        """Report the detached ComfyUI transaction after the client restarts."""
        result = self._take_startup_update_result("comfyui")
        if not result:
            return
        status = str(result.get("status") or "")
        detail = (
            str(result.get("error") or result.get("message") or "")
            .strip()
            .replace("\x00", "")[:1200]
        )
        metadata = result.get("release_metadata")
        version = ""
        if isinstance(metadata, dict):
            version = str(metadata.get("version") or "").strip()
        restart_error = (
            str(result.get("restart_error") or "").strip().replace("\x00", "")[:600]
        )
        warning_summary = self._comfyui_update_result_warnings(result)
        if status == "installed":
            status_text = "  ComfyUI 已更新"
            if version:
                status_text += f"至 v{version}"
            if warning_summary:
                status_text += "；部分临时文件将在稍后清理"
            try:
                self._footer_label.config(text=status_text)
            except Exception:
                pass
            return
        if status == "failed_rolled_back":
            message = "ComfyUI 更新未完成，旧版本已自动恢复。"
        elif status == "rollback_incomplete":
            message = (
                "ComfyUI 更新失败，且自动恢复未完整完成。请不要继续生成，"
                "先使用“修复运行环境”。"
            )
        else:
            message = "ComfyUI 更新未完成，本地环境未被替换。"
        if detail:
            message += f"\n\n详细信息：{detail}"
        if warning_summary:
            message += f"\n\n清理与回滚提示：\n{warning_summary}"
        messagebox.showerror("ComfyUI 更新失败", message, parent=self)

    def _exit_for_runtime_update(self, popup=None):
        """Close this process so the external helper can replace mapped DLLs."""
        self._shutting_down = True
        self._anim_running = False
        self._heartbeat_run = False
        self._poll_run = False
        self._complete_destroy(popup)

    def _runtime_maintenance_active(self) -> bool:
        lock = self.__dict__.get("_runtime_maintenance_lock")
        if lock is None:
            return bool(self.__dict__.get("_runtime_maintenance_in_progress", False))
        if not lock.acquire(blocking=False):
            return True
        try:
            return bool(self._runtime_maintenance_in_progress)
        finally:
            lock.release()

    def _reserve_runtime_maintenance(self) -> tuple[bool, set[str], str]:
        """Reserve one maintenance transaction without blocking the Tk thread."""
        with self._runtime_maintenance_lock:
            if self._shutting_down:
                return False, set(), "客户端正在退出"
            if self._runtime_maintenance_in_progress:
                return False, set(), "已有运行环境维护任务正在进行"
            self._runtime_maintenance_in_progress = True
            running_before = {
                role
                for role in ("comfyui", "api")
                if self._process_supervisor.is_running(role)
            }
        return True, running_before, ""

    def _begin_runtime_maintenance(self) -> tuple[bool, set[str], str]:
        """Reserve runtime replacement and stop services without a launch race."""
        reserved, running_before, reserve_error = self._reserve_runtime_maintenance()
        if not reserved:
            return False, running_before, reserve_error
        try:
            error = self._stop_runtime_for_maintenance()
        except Exception as exc:
            with self._runtime_maintenance_lock:
                self._runtime_maintenance_in_progress = False
            return False, running_before, f"停止后台服务失败：{exc}"
        return True, running_before, error

    def _end_runtime_maintenance(self, restart: bool):
        """Release the maintenance guard and optionally restore backend services."""
        with self._runtime_maintenance_lock:
            self._runtime_maintenance_in_progress = False
        if restart and not self._shutting_down:
            self.after(
                500,
                lambda: self._request_backend_start("恢复后台"),
            )

    def _stop_runtime_for_maintenance(self) -> str:
        """Stop only processes that may lock runtime files before replacement."""
        self._poll_run = False
        errors = []
        for role in ("comfyui", "api", "torch-check", "environment-check"):
            error = self._process_supervisor.terminate(role, timeout=8)
            if error:
                errors.append(f"{role}: {error}")
        if not self._process_supervisor.is_running("comfyui"):
            self._comfy_proc = None
        if not self._process_supervisor.is_running("api"):
            self._api_proc = None
        for name, port in (("ComfyUI", COMFY_PORT), ("API", API_PORT)):
            port_ready, port_error = self._process_supervisor.prepare_port(port)
            if not port_ready:
                errors.append(
                    f"{name} 仍被其他程序占用：{port_error}。请先手动关闭该程序"
                )
        return "；".join(errors)

    def _open_install_guide(self):
        """打开安装说明"""
        pdf_path = BASE_DIR / "灵境造片厂使用教学.pdf"
        if pdf_path.exists():
            os.startfile(str(pdf_path))
            return
        guide_path = BASE_DIR / "README.md"
        if guide_path.exists():
            os.startfile(str(guide_path))
        else:
            webbrowser.open(PROJECT_HOMEPAGE_URL)

    # ══════════════════════════════════════════════════════
    # 底部状态栏
    # ══════════════════════════════════════════════════════
    def _build_footer(self):
        bar = tk.Frame(self._content_root, bg=C["surface"], height=LAYOUT["footer_h"])
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._loading_bar = tk.Frame(bar, bg=C["surface"])
        self._loading_bar.pack(side="left", padx=(12, 8))
        self._loading_text = tk.Label(
            self._loading_bar, text="", font=F["normal"], fg=C["warn"], bg=C["surface"])
        self._loading_text.pack(side="left")
        self._loading_dots = tk.Label(
            self._loading_bar, text="", font=F["normal"], fg=C["warn"], bg=C["surface"], width=3, anchor="w")
        self._loading_dots.pack(side="left")
        self._footer_label = tk.Label(
            bar, text="", font=F["normal"], fg=C["text2"], bg=C["surface"], anchor="w")
        self._footer_label.pack(side="left")
        tk.Button(bar, text="退出", font=F["small"], bg=C["surface"], fg=C["error"],
                  activebackground=C["hover"], relief="flat", bd=0, cursor="hand2",
                  command=self._on_close).pack(side="right", padx=(0, 12))
        self._animate_loading()

    # ══════════════════════════════════════════════════════
    # 辅助动作
    # ══════════════════════════════════════════════════════
    def _short_middle(self, text: str, left: int = 30, right: int = 16) -> str:
        text = str(text or "").strip()
        if len(text) <= left + right + 3:
            return text
        return f"{text[:left]}...{text[-right:]}"

    def _set_public_url(self, url: str):
        self._url_label.config(text=self._short_middle(url, 16, 10) if url else "正在建立公网连接...")

    def _set_local_url(self, url: str):
        value = str(url or "").strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme.lower() == "http"
                and str(parsed.hostname or "").lower() in _LOOPBACK_SERVER_HOSTS
                and parsed.port is not None
                and parsed.path in ("", "/")
            )
        except ValueError:
            valid = False
        self._local_url = value if valid else API_BASE
        self._local_url_label.config(text=self._local_url)

    def _clear_public_url(self):
        self._tunnel_url = ""
        self._set_public_url("")
        self._initial_session_sync_done = False

    def _set_api_key(self, api_key: str):
        self._key_label.config(text=self._short_middle(api_key, 18, 10) if api_key else "—")
        settings_label = getattr(self, "_settings_key_label", None)
        if settings_label is not None:
            masked = f"{api_key[:12]}{'•' * 12}{api_key[-6:]}" if len(api_key) > 20 else "服务启动后自动生成"
            try:
                settings_label.config(text=masked)
            except Exception:
                pass

    def _copy_public_url(self):
        self._copy(self._tunnel_url)

    def _copy_local_url(self):
        self._copy(self._local_url or API_BASE)

    def _copy_api_key(self):
        self._copy(self._api_key)

    def _edit_api_key(self):
        """Let the user change the local gateway key and apply it safely."""
        popup = tk.Toplevel(self)
        popup.title("修改访问密钥")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        popup.grab_set()
        self._center_popup(popup, 540, 255)

        panel = self._card(popup, fill="both", expand=True, padx=16, pady=16)
        tk.Label(panel, text="修改访问密钥", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=22, pady=(20, 5))
        tk.Label(
            panel,
            text="其他软件使用这个 Key 调用客户端。保存后，正在运行的 API 会自动重启。",
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
        ).pack(anchor="w", padx=22)

        entry = self._entry_widget(panel, width=430 if CTK_AVAILABLE else 52)
        entry.pack(fill="x", padx=22, pady=(14, 5), ipady=7 if not CTK_AVAILABLE else 0)
        entry.insert(0, self._api_key or f"sk-local-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}")
        status = tk.Label(panel, text="16–128 位，仅支持字母、数字、点、下划线和短横线。", font=F["tiny"], fg=C["muted"], bg=C["card"])
        status.pack(anchor="w", padx=22)

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=22, pady=(13, 0))

        def regenerate():
            value = f"sk-local-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
            entry.delete(0, "end")
            entry.insert(0, value)
            status.config(text="已生成新密钥，点击保存后生效。", fg=C["warn"])

        def save():
            value = entry.get().strip()
            if not 16 <= len(value) <= 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
                status.config(text="密钥格式不正确，请按提示修改。", fg=C["error"])
                return
            status.config(text="正在保存并应用...", fg=C["warn"])
            regenerate_button.configure(state="disabled")
            save_button.configure(state="disabled")

            def apply_key():
                if not self._begin_backend_action("应用访问密钥"):
                    self.after(0, lambda: show_error("其他后台操作正在进行，请稍候重试。"))
                    return
                was_running = False
                saved = False
                try:
                    was_running = self._process_supervisor.is_running("api")
                    if was_running:
                        self._invalidate_health_polling()
                        error = self._process_supervisor.terminate("api", timeout=8)
                        if error:
                            self._ensure_health_polling()
                            self.after(0, lambda e=error: show_error(f"API 无法安全重启：{e}"))
                            return
                        self._api_proc = None
                    # Stop the old API before writing.  Its in-memory state may
                    # contain the previous key and must not overwrite this save.
                    RuntimeState(BASE_DIR / "runtime").set_api_key(value)
                    saved = True
                    if not self._run_ui_backend_step(
                        lambda: finish_save(value, was_running)
                    ):
                        return
                    if (
                        was_running
                        and not self._shutting_down
                        and not self._runtime_maintenance_active()
                    ):
                        if self._run_ui_backend_step(self._start_api_service):
                            self._ensure_health_polling()
                except Exception as exc:
                    if was_running and not self._shutting_down:
                        try:
                            if self._run_ui_backend_step(self._start_api_service):
                                self._ensure_health_polling()
                        except Exception as restore_error:
                            print(f"[Settings] API restore failed: {restore_error}")
                    if saved:
                        self.after(
                            0,
                            lambda e=exc: self._report_backend_failure(
                                f"访问密钥已保存，但 API 重启失败：{e}"
                            ),
                        )
                    else:
                        self.after(0, lambda e=exc: show_error(f"保存失败：{e}"))
                finally:
                    self._end_backend_action()

            threading.Thread(target=apply_key, daemon=True).start()

        def show_error(message: str):
            try:
                if popup.winfo_exists():
                    status.config(text=message, fg=C["error"])
                    regenerate_button.configure(state="normal")
                    save_button.configure(state="normal")
            except Exception:
                pass

        def finish_save(value: str, restart_api: bool):
            self._api_key = value
            self._set_api_key(value)
            try:
                popup.grab_release()
                popup.destroy()
            except Exception:
                pass
            self._footer_label.config(text="  访问密钥已保存")
            if restart_api and not self._shutting_down:
                self._clear_public_url()
                self._footer_label.config(text="  访问密钥已保存，正在重启 API...")
                self._set_light("api", "loading", "重启中")
                self._set_light("tunnel", "loading", "重连中")

        regenerate_button = self._button(actions, "重新生成", regenerate, "plain", width=88)
        regenerate_button.pack(side="left")
        save_button = self._button(actions, "保存并应用", save, "primary", width=104)
        save_button.pack(side="right")

    def _json_headers(self, server_url: str = "", token: str = "") -> dict:
        server_url = (server_url or "").rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/126.0.0.0 Safari/537.36 LingJingClient/{APP_VERSION}"
            ),
        }
        if server_url.startswith(("http://", "https://")):
            headers["Origin"] = server_url
            headers["Referer"] = f"{server_url}/"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _current_local_api_key(self) -> str:
        """Read the key used by the running local API, with memory as fallback."""
        persisted = ""
        try:
            persisted = str(
                RuntimeState(BASE_DIR / "runtime").api_key or ""
            ).strip()
        except Exception as exc:
            print(f"[Runtime] failed to reload local API key: {exc}")
        api_key = persisted or str(self._api_key or "").strip()
        if api_key and api_key != self._api_key:
            self._api_key = api_key
        return api_key

    def _local_api_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        api_key = self._current_local_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _local_admin_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        try:
            admin_key = str(RuntimeState(BASE_DIR / "runtime").admin_key or "").strip()
        except Exception as exc:
            raise RuntimeError(f"无法读取本机管理凭据：{exc}") from exc
        if not admin_key:
            raise RuntimeError("本机管理凭据尚未生成，请先重启 API 服务")
        headers["Authorization"] = f"Bearer {admin_key}"
        return headers

    def _set_account_status(self, text: str, status: str = "warn"):
        color = C["success"] if status == "success" else C["error"] if status == "error" else C["warn"]
        self._account_status_text = text
        if hasattr(self, "_account_status_label"):
            self._account_status_label.config(text=text, fg=color)
        self._render_account_badge()

    def _fetch_account_profile(self, server_url: str, token: str) -> dict:
        if not server_url or not token:
            return {}
        import urllib.request as ur
        req = ur.Request(
            f"{server_url.rstrip('/')}/api/auth/me",
            method="GET",
            headers=self._json_headers(server_url, token),
        )
        with _open_platform_request(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _format_http_error(self, error: Exception) -> str:
        try:
            import urllib.error as ue
            if isinstance(error, ue.HTTPError):
                raw = error.read().decode("utf-8", errors="ignore")
                lowered = raw.lower()
                if "user-agent has been banned" in lowered or "cloudflare" in lowered:
                    return "Cloudflare 拦截了桌面客户端请求，请在服务端安全规则放行客户端登录接口"
                try:
                    data = json.loads(raw)
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        code = str(err.get("code") or "").strip()
                        msg = str(err.get("message") or "").strip()
                        auth_errors = {
                            "USER_NOT_FOUND",
                            "INVALID_CREDENTIALS",
                            "INVALID_PASSWORD",
                            "WRONG_PASSWORD",
                            "AUTH_FAILED",
                            "LOGIN_FAILED",
                        }
                        auth_text = f"{code} {msg}".casefold()
                        if (
                            code.upper() in auth_errors
                            or "not registered" in auth_text
                            or "not found" in auth_text
                            or "未注册" in auth_text
                            or "密码错误" in auth_text
                        ):
                            return "用户名或密码错误"
                        return f"{code}: {msg}" if code and msg else msg or code or f"HTTP {error.code}"
                    if isinstance(data, dict) and data.get("message"):
                        return str(data.get("message"))
                except Exception:
                    pass
                raw = raw.strip()
                if raw.startswith("<"):
                    return f"HTTP {error.code} {error.reason}"
                return raw[:240] or f"HTTP {error.code} {error.reason}"
        except Exception:
            pass
        return str(error)

    def _use_guest_mode(self):
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_mode = "guest"
        self._server_account_profile = {}
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._apply_account_visibility()
        self._set_account_status("本地模式：不会连接平台服务端", "success")
        self._set_light("server", "online", "本地模式")

    def _show_login_prompt(self, force: bool = False):
        if self._shutting_down:
            return
        try:
            if self._login_popup is not None and self._login_popup.winfo_exists():
                self._login_popup.lift()
                self._login_popup.focus_force()
                return
        except Exception:
            self._login_popup = None
        if self._login_prompt_shown and not force:
            return
        self._login_prompt_shown = True

        popup = tk.Toplevel(self)
        self._login_popup = popup
        popup.title("登录平台账号")
        popup.geometry("600x470")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(True, True)
        popup.minsize(580, 450)
        self._center_popup(popup, 600, 470)

        def close_popup():
            self._login_popup = None
            try:
                popup.grab_release()
            except Exception:
                pass
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)

        title_row = tk.Frame(panel, bg=C["card"])
        title_row.pack(fill="x", padx=28, pady=(22, 8))
        tk.Label(title_row, text="▷", font=("Microsoft YaHei UI", 18, "bold"),
                 fg="#ffffff", bg=C["primary"], width=3).pack(side="left", padx=(0, 10), ipady=4)
        tk.Label(title_row, text="登录灵境造片厂账号", font=F["title"],
                 fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Label(
            panel,
            text=(
                "登录后会向你填写的服务端同步公网 URL、本机 API Key、设备名称及工作流/模型状态，"
                "供平台调用；不会同步生成提示词。不登录也能使用完整的本地功能。"
            ),
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
            justify="left",
            wraplength=500,
        ).pack(anchor="w", padx=28, pady=(0, 18))

        form = tk.Frame(panel, bg=C["card"])
        form.pack(fill="x", padx=28)

        def popup_field(label, text="", show=None):
            row = tk.Frame(form, bg=C["card"])
            row.pack(fill="x", pady=(0, 10))
            tk.Label(row, text=label, font=F["normal"], fg=C["text"], bg=C["card"], width=10, anchor="w").pack(side="left")
            entry = self._entry_widget(
                row,
                show=show,
                width=320,
                font=("Microsoft YaHei UI", 11),
                height=40,
            )
            entry.pack(side="left", fill="x", expand=True, ipady=7 if not CTK_AVAILABLE else 0)
            if text:
                entry.insert(0, text)
            return entry

        server_entry = popup_field("服务端地址", self._get_server_url() or "https://ai.lol-lu.site")
        email_entry = popup_field("账号邮箱", self._get_server_email())
        password_entry = popup_field("密码", self._get_server_password(), show="*")
        status_lbl = tk.Label(panel, text="", font=F["normal"], fg=C["error"], bg=C["card"], anchor="w")
        status_lbl.pack(fill="x", padx=28)

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=28, pady=(14, 0))

        def guest():
            self._set_account_form_values(
                server_url=server_entry.get().strip(),
                email=email_entry.get().strip(),
                password="",
            )
            self._use_guest_mode()
            close_popup()

        def login():
            normalized_email = _normalize_account_email(email_entry.get())
            self._set_account_form_values(
                server_url=server_entry.get().strip(),
                email=normalized_email,
                password=password_entry.get(),
            )
            def on_result(success: bool, message: str):
                try:
                    if not popup.winfo_exists():
                        return
                    status_lbl.config(text=message, fg=C["success"] if success else C["error"])
                    login_button.configure(state="normal", text="登录并同步")
                    if success:
                        popup.after(500, close_popup)
                    else:
                        password_entry.focus_set()
                except Exception:
                    pass

            status_lbl.config(text="正在验证账号...", fg=C["warn"])
            login_button.configure(state="disabled", text="正在登录...")
            self._login_and_sync(on_result=on_result)

        login_button = self._button(actions, "登录并同步", login, "primary")
        login_button.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self._button(actions, "暂不登录，使用本地功能", guest, "plain").pack(side="left", fill="x", expand=True, ipady=7, padx=(8, 0))

        links = tk.Frame(panel, bg=C["card"])
        links.pack(fill="x", padx=28, pady=(16, 0))
        tk.Button(links, text="注册账号", font=F["small"], bg=C["card"], fg=C["primary"],
                  activebackground=C["card"], relief="flat", bd=0, cursor="hand2",
                  command=self._open_register).pack(side="left")

    def _login_and_sync(self, on_result=None):
        if self._server_sync_running:
            if callable(on_result):
                on_result(False, "已有登录请求正在处理中，请稍候。")
            return
        server_url = self._get_server_url()
        email = _normalize_account_email(self._get_server_email())
        password = self._get_server_password()
        if not server_url:
            self._set_account_status("请填写服务端地址", "error")
            if callable(on_result):
                on_result(False, "请填写服务端地址。")
            return
        url_error = _platform_server_url_error(server_url)
        if url_error:
            self._set_account_status(url_error.rstrip("。"), "error")
            if callable(on_result):
                on_result(False, url_error)
            return
        if not email or not password:
            self._set_account_status("请填写账号邮箱和密码", "error")
            if callable(on_result):
                on_result(False, "请填写账号邮箱和密码。")
            return
        email_error = _account_email_error(email)
        if email_error:
            self._set_account_status(email_error.rstrip("。"), "error")
            if callable(on_result):
                on_result(False, email_error)
            return
        self._set_account_form_values(email=email)
        threading.Thread(
            target=self._login_and_sync_worker,
            args=(server_url, email, password, on_result),
            daemon=True,
        ).start()

    def _login_and_sync_worker(self, server_url: str, email: str, password: str, on_result=None):
        email = _normalize_account_email(email)
        self._server_sync_running = True
        self.after(0, lambda: self._set_account_status("正在登录服务端...", "warn"))
        try:
            import urllib.request as ur

            login_payload = json.dumps({"email": email, "password": password}).encode("utf-8")
            req = ur.Request(
                f"{server_url}/api/auth/login",
                data=login_payload,
                method="POST",
                headers=self._json_headers(server_url),
            )
            with _open_platform_request(req, timeout=20) as resp:
                login_data = json.loads(resp.read().decode("utf-8"))
            token = login_data.get("sessionToken", "")
            if not token:
                raise RuntimeError("服务端未返回 sessionToken")
            self._server_session_token = token
            self._offline_notice_sent = False
            self._server_user_email = email
            self._server_url_value = server_url
            self._server_mode = "logged_in"
            self._server_account_profile = {"user": login_data.get("user", {})}
            try:
                profile = self._fetch_account_profile(server_url, token)
                if profile:
                    self._server_account_profile = profile
            except Exception as profile_error:
                print(f"[Account] profile refresh failed: {profile_error}")
            self._save_account_session()
            self.after(0, lambda: self._set_account_form_values(server_url=server_url, email=email, password=""))
            self.after(0, self._render_account_badge)
            self.after(0, self._apply_account_visibility)
            self.after(0, lambda: self._set_account_status("登录成功，正在同步客户端...", "warn"))
            if callable(on_result):
                self.after(0, lambda: on_result(True, "登录成功，正在同步客户端信息..."))
            try:
                self._sync_to_server_with_retry(server_url, max_retries=SERVER_SYNC_MAX_RETRIES, takeover=True)
                self._save_account_session()
                self.after(0, lambda: self._set_account_status(f"已同步：{email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as sync_error:
                friendly = self._format_http_error(sync_error)
                self.after(0, lambda e=friendly: self._set_account_status(f"已登录，同步失败：{e}", "error"))
                self.after(0, lambda: self._set_light("server", "offline", "同步失败"))
        except Exception as ex:
            self._server_session_token = ""
            self._server_mode = "guest"
            self.after(0, lambda: self._set_account_form_values(password=""))
            friendly = self._format_http_error(ex)
            self.after(0, lambda e=friendly: self._set_account_status(f"同步失败：{e}", "error"))
            if callable(on_result):
                self.after(0, lambda e=friendly: on_result(False, f"登录失败：{e}"))
            self.after(0, lambda: self._set_light("server", "online", "本地模式"))
            self.after(0, self._render_account_badge)
            self.after(0, self._apply_account_visibility)
        finally:
            self._server_sync_running = False

    def _sync_payload(self, status: str = "", takeover: bool = False) -> dict:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        workflows = self._last_health.get("workflows") or []
        if isinstance(workflows, int):
            workflows = []
        if not workflows:
            try:
                workflows = read_local_workflow_catalog(
                    _workflows_dir(),
                    BASE_DIR / "runtime" / "workflow_config.json",
                )
            except (OSError, ValueError):
                workflows = []
        models = []
        model_groups = {"image": [], "video": [], "text": []}
        for wf in workflows:
            if not isinstance(wf, dict):
                continue
            model = {
                "id": wf.get("id", ""),
                "name": wf.get("name", wf.get("id", "")),
                "label": wf.get("name", wf.get("id", "")),
                "type": wf.get("type", ""),
                "capability": wf.get("capability", ""),
                "model_group": wf.get("model_group", ""),
                "available": wf.get("enabled", True) and self._workflow_model_available(wf),
                "workflowId": wf.get("id", ""),
                "input_schema": wf.get("input_schema") or {},
                "inputs": wf.get("inputs") or [],
            }
            models.append({
                **model,
            })
            group = self._model_group_for_type(model["type"])
            model_groups.setdefault(group, []).append(model)
        raw_current_task = self._last_health.get("current_task")
        safe_current_task = None
        if isinstance(raw_current_task, dict):
            safe_fields = (
                "id",
                "task_id",
                "workflow_id",
                "workflow_name",
                "phase",
                "progress_label",
                "progress_percent",
                "status",
                "progress",
                "progress_max",
                "started_at",
                "elapsed",
                "elapsed_seconds",
            )
            safe_current_task = {
                key: raw_current_task[key]
                for key in safe_fields
                if key in raw_current_task
            }

        return {
            "baseUrl": self._tunnel_url,
            "apiKey": self._api_key,
            "base_url": self._tunnel_url,
            "api_key": self._api_key,
            "clientId": self._last_health.get("session_id", "") or f"local-{os.environ.get('COMPUTERNAME', 'windows')}",
            "instanceId": self._client_instance_id,
            "clientName": os.environ.get("COMPUTERNAME", "Windows 客户端"),
            "localApi": API_BASE,
            "client_id": self._last_health.get("session_id", "") or f"local-{os.environ.get('COMPUTERNAME', 'windows')}",
            "instance_id": self._client_instance_id,
            "client_name": os.environ.get("COMPUTERNAME", "Windows 客户端"),
            "version": self._last_health.get("version", APP_VERSION),
            "local_api": API_BASE,
            "status": status or ("online" if self._tunnel_url else "starting"),
            "heartbeatAt": now_iso,
            "heartbeat_at": now_iso,
            "lastSeenAt": now_iso,
            "last_seen_at": now_iso,
            "workflows": workflows,
            "models": models,
            "modelGroups": model_groups,
            "model_groups": model_groups,
            "current_task": safe_current_task,
            "source": "desktop-gui",
            "takeover": bool(takeover),
        }

    def _model_group_for_type(self, workflow_type: str) -> str:
        text = str(workflow_type or "").lower()
        if "video" in text or "flf2v" in text:
            return "video"
        if "text" in text or "chat" in text:
            return "text"
        return "image"

    def _workflow_model_available(self, workflow: dict) -> bool:
        if "available" in workflow:
            return bool(workflow.get("available"))
        dependencies = workflow.get("dependencies")
        if isinstance(dependencies, dict):
            report = workflow_dependency_report(dependencies, _models_dir())
            if report["dependency_status"] != "ready":
                return False
        for key in ("missing_models", "missingModels", "missing"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple, set)) and value:
                return False
            if isinstance(value, str) and value.strip():
                return False

        model_key = self._workflow_model_key(workflow)
        if model_key:
            return self._model_status.get(model_key) == "完整"
        workflow_id = str(workflow.get("id") or "").lower()
        workflow_name = str(workflow.get("name") or workflow.get("label") or "").lower()
        text = f"{workflow_id} {workflow_name}"
        if "wan" in text or "wan2.1" in text:
            return self._model_status.get("Wan2.1") == "完整"
        if "flux" in text:
            return self._model_status.get("Flux2") == "完整"
        if "qwen" in text or "千问" in text:
            return self._model_status.get("Qwen3.5") == "完整"
        return bool(workflow.get("enabled", True))

    def _sync_to_server(self, server_url: str = "", status: str = "", takeover: bool = False):
        if not self._server_session_token:
            return
        server_url = (server_url or self._server_url_value).rstrip("/")
        if not server_url:
            return
        payload = self._sync_payload(status=status, takeover=takeover)
        if not payload["base_url"] or not payload["api_key"]:
            raise RuntimeError("公网 URL 或 API Key 尚未就绪")

        import urllib.request as ur
        req = ur.Request(
            f"{server_url}/api/client/local-session/sync",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._json_headers(server_url, self._server_session_token),
        )
        with _open_platform_request(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _sync_to_server_with_retry(self, server_url: str = "", status: str = "", max_retries: int = SERVER_SYNC_MAX_RETRIES, takeover: bool = False):
        last_error = None
        attempts = max(1, int(max_retries or 1))
        for attempt in range(1, attempts + 1):
            if self._shutting_down:
                raise RuntimeError("客户端正在退出，已取消服务端同步")
            try:
                result = self._sync_to_server(server_url, status, takeover=takeover)
                if self._shutting_down:
                    raise RuntimeError("客户端正在退出，已取消服务端同步")
                self._server_sync_fail_count = 0
                return result
            except Exception as ex:
                if self._is_session_replaced_error(ex):
                    self._post_to_ui(self._handle_remote_session_replaced)
                    raise ex
                last_error = ex
                self._server_sync_fail_count = attempt
                if attempt < attempts:
                    self._post_to_ui(
                        lambda a=attempt: self._set_light("server", "loading", f"重试 {a}/{attempts}")
                    )
                    delay = min(2 * attempt, 6)
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        if self._shutting_down:
                            raise RuntimeError("客户端正在退出，已取消服务端同步")
                        time.sleep(min(0.1, deadline - time.monotonic()))
        raise last_error

    def _is_session_replaced_error(self, error: Exception) -> bool:
        code = getattr(error, "code", None)
        if code != 409:
            return False
        friendly = self._format_http_error(error)
        return "LOCAL_CLIENT_SESSION_REPLACED" in friendly or "其他客户端" in friendly

    def _handle_remote_session_replaced(self):
        self._server_session_token = ""
        self._server_mode = "guest"
        self._server_account_profile = {}
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._apply_account_visibility()
        self._set_account_status("账号已在其他客户端登录，本客户端已停止同步。", "error")
        self._set_light("server", "online", "本地模式")

    def _notify_server_offline(self, server_url: str = "", token: str = ""):
        session_token = token or self._server_session_token
        if self._offline_notice_sent or not session_token:
            return
        self._offline_notice_sent = True
        server_url = (server_url or self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
        if not server_url:
            return
        try:
            import urllib.request as ur
            payload = self._sync_payload(status="offline")
            req = ur.Request(
                f"{server_url}/api/client/local-session/offline",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=self._json_headers(server_url, session_token),
            )
            with _open_platform_request(req, timeout=5) as resp:
                resp.read()
        except Exception as ex:
            print(f"[Account] offline notify failed: {ex}")

    def _heartbeat_loop(self):
        while self._heartbeat_run:
            time.sleep(30)
            if self._shutting_down or not self._server_session_token:
                continue
            try:
                self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES)
                if self._server_user_email:
                    self.after(0, lambda: self._set_account_status(f"已同步：{self._server_user_email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as ex:
                friendly = self._format_http_error(ex)
                self.after(0, lambda e=friendly: self._set_account_status(f"同步异常：{e}", "error"))
                self.after(0, lambda: self._set_light("server", "offline", "同步失败"))

    def _refresh_saved_login_and_sync(self):
        if self._initial_session_sync_done or self._server_mode != "logged_in" or not self._server_session_token:
            return
        if not self._tunnel_url or not self._api_key:
            return
        self._initial_session_sync_done = True

        def _worker():
            server_url = (self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
            try:
                profile = self._fetch_account_profile(server_url, self._server_session_token)
                if profile:
                    self._server_account_profile = profile
                    user = profile.get("user") if isinstance(profile.get("user"), dict) else {}
                    self._server_user_email = user.get("email") or self._server_user_email
                self._sync_to_server_with_retry(server_url, max_retries=SERVER_SYNC_MAX_RETRIES)
                self._save_account_session()
                email = self._server_user_email or "账号"
                self.after(0, self._render_account_badge)
                self.after(0, lambda: self._set_account_status(f"已自动同步：{email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as ex:
                friendly = self._format_http_error(ex)
                if "UNAUTHENTICATED" in friendly or "Please log in" in friendly or "HTTP 401" in friendly:
                    self._clear_account_session()
                    self._server_mode = "guest"
                    self._save_account_session()
                    self.after(0, self._render_account_badge)
                    self.after(0, self._apply_account_visibility)
                    self.after(0, lambda: self._set_account_status("平台登录已过期，本地功能不受影响", "warn"))
                    self.after(0, lambda: self._set_light("server", "online", "本地模式"))
                else:
                    self.after(0, lambda e=friendly: self._set_account_status(f"自动同步失败：{e}", "error"))
                    self.after(0, lambda: self._set_light("server", "offline", "同步失败"))

        threading.Thread(target=_worker, daemon=True).start()

    def _selected_workflow_path(self) -> str:
        workflow_id = self._workflow_mode.get()
        if not workflow_id or workflow_id == "default":
            return "/v1/workflows/run"
        return f"/v1/workflows/run/{workflow_id}"

    def _selected_image_model(self) -> str:
        return "flux_t2i_v1"

    def _open_server_url(self, path: str):
        base = (self._get_server_url() or self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
        try:
            webbrowser.open(f"{base}{path}")
        except Exception as ex:
            messagebox.showinfo("提示", f"请在浏览器打开：{base}{path}\n\n{ex}")

    def _open_register(self):
        self._open_server_url("/?auth=register")

    def _copy_ark_image_example(self):
        url = self._tunnel_url or API_BASE
        key = self._api_key or "YOUR_API_KEY"
        model = self._selected_image_model()
        example = (
            f'curl -X POST {url}/api/v3/images/generations \\\n'
            f'  -H "Authorization: Bearer {key}" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"model":"{model}","prompt":"一只可爱的猫","response_format":"url","size":"2K","stream":false,"watermark":false}}\''
        )
        self.clipboard_clear()
        self.clipboard_append(example)
        self._footer_label.config(text="  已复制火山兼容图片示例")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _copy_example(self):
        url = self._tunnel_url or API_BASE
        key = self._api_key or "YOUR_API_KEY"
        path = self._selected_workflow_path()
        example = (
            f'curl -X POST {url}{path} \\\n'
            f'  -H "Authorization: Bearer {key}" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"prompt": "一只可爱的猫", "steps": 20}}\''
        )
        self.clipboard_clear()
        self.clipboard_append(example)
        self._footer_label.config(text="  已复制调用示例到剪贴板")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _storage_directory(self, name: str) -> Path:
        getters = {
            "models": _models_dir,
            "workflows": _workflows_dir,
            "outputs": _outputs_dir,
            "logs": _logs_dir,
        }
        try:
            return getters[name]()
        except KeyError as exc:
            raise ValueError(f"未知文件目录：{name}") from exc

    def _choose_storage_directory(self, name: str):
        labels = {
            "models": "模型",
            "workflows": "工作流",
            "outputs": "生成结果",
            "logs": "运行日志",
        }
        label = labels.get(name)
        if not label:
            messagebox.showerror("无法设置目录", "未识别的目录类型。", parent=self)
            return
        current = self._storage_directory(name)
        selected = filedialog.askdirectory(
            title=f"选择{label}目录",
            initialdir=str(current),
            mustexist=True,
            parent=self,
        )
        if not selected:
            return
        try:
            config = Config(BASE_DIR)
            saved = config.set_directory_mapping(name, selected)
            mapped = bool(config.get(f"directories.{name}", ""))
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror(
                "目录设置失败",
                f"所选位置当前不可用，请重新选择。\n\n{exc}",
                parent=self,
            )
            return

        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.update_storage_path(name, saved)
        action = "目录映射已保存" if mapped else "已恢复默认目录"
        self._footer_label.config(text=f"  {label}{action}，重新打开客户端后生效")
        def scan_selected():
            try:
                patterns = {"models": ("*.safetensors", "*.ckpt", "*.pt", "*.pth", "*.bin", "*.gguf"),
                            "workflows": ("manifest.json",),
                            "outputs": ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.mp4", "*.webm")}
                count = sum(1 for pattern in patterns.get(name, ()) for entry in saved.rglob(pattern) if entry.is_file())
                message = f"  {label}新目录扫描完成：发现 {count} 个文件；重新打开客户端后使用新目录"
            except OSError:
                message = f"  {label}目录已保存，部分内容无法读取；请检查目录权限"
            self.after(0, lambda: self._footer_label.config(text=message))
        threading.Thread(target=scan_selected, daemon=True).start()
        messagebox.showinfo(
            action,
            f"{label}将使用：\n{saved}\n\n"
            "请退出并重新打开灵境造片厂后生效。\n"
            "原目录中的文件不会自动搬移；如果所选目录以后不存在，客户端会自动恢复默认位置。",
            parent=self,
        )

    def _open_outputs(self):
        outputs_dir = _outputs_dir()
        outputs_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(outputs_dir))

    def _open_last_output(self):
        self._open_outputs_for_items(self._last_completed_outputs or [], self._last_completed_task_id)

    def _open_outputs_for_items(self, outputs: list, task_id: str = ""):
        outputs_dir = _outputs_dir()
        outputs_dir.mkdir(parents=True, exist_ok=True)
        outputs_root = outputs_dir.resolve()
        task_id = str(task_id or "").strip()
        for item in outputs:
            if not isinstance(item, dict):
                continue
            filename = Path(str(item.get("filename") or item.get("file") or "")).name
            if not filename:
                continue
            subfolder = str(item.get("subfolder") or "").strip().replace("\\", "/")
            candidates = []
            subfolder_path = Path(subfolder)
            if (
                subfolder
                and not subfolder_path.is_absolute()
                and not subfolder_path.drive
                and all(part not in {"", ".", ".."} for part in subfolder_path.parts)
            ):
                candidate = (outputs_dir / subfolder_path / filename).resolve()
                if candidate.is_relative_to(outputs_root):
                    candidates.append(candidate)
            item_task_id = str(item.get("task_id") or item.get("taskId") or task_id)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", item_task_id):
                candidate = (outputs_dir / item_task_id / filename).resolve()
                if candidate.is_relative_to(outputs_root):
                    candidates.append(candidate)
            candidates.append((outputs_dir / filename).resolve())
            for candidate in candidates:
                try:
                    if candidate.exists() and candidate.is_file():
                        os.startfile(str(candidate))
                        return
                except Exception:
                    continue
        os.startfile(str(outputs_dir))

    def _open_models(self):
        models_dir = _models_dir()
        models_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(models_dir))

    def _open_runtime_dir(self):
        runtime_dir = BASE_DIR / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(runtime_dir))

    def _open_logs_dir(self):
        log_dir = self._backend_log_dir()
        os.startfile(str(log_dir))

    def _open_runtime_config(self):
        try:
            config_path = Config(BASE_DIR).ensure_file()
            os.startfile(str(config_path))
            self._footer_label.config(text="  高级设置已打开，修改后请退出并重新打开客户端")
        except Exception as exc:
            messagebox.showerror(
                "无法打开高级设置",
                f"配置文件未能打开：{exc}",
                parent=self,
            )

    def _start_background_model_recheck(self):
        threading.Thread(target=self._recheck_models, daemon=True).start()

    def _start_background_runtime_recheck(self):
        """Check package, GPU and PyTorch in the background, then refresh the card."""
        if not self._begin_backend_action("检查环境"):
            return
        self._set_light("env", "loading", "检查中")
        self._footer_label.config(text="  正在检查运行环境...")
        try:
            threading.Thread(
                target=self._environment_recheck_worker,
                daemon=True,
            ).start()
        except Exception:
            self._end_backend_action()
            raise

    def _environment_recheck_worker(self):
        try:
            self._retry_env_check()
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._finish_runtime_recheck(
                    {
                        "package_ready": not missing_runtime_paths(BASE_DIR),
                        "ready": False,
                        "missing": missing_runtime_paths(BASE_DIR),
                        "gpu_name": "",
                        "message": f"环境检查失败：{message}",
                    }
                ),
            )
        finally:
            self._end_backend_action()

    def _show_workflow_tutorial(self):
        popup = tk.Toplevel(self)
        popup.title("添加工作流教程")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(True, True)
        popup.minsize(620, 440)
        self._center_popup(popup, 660, 480)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        tk.Label(panel, text="三步添加自己的 ComfyUI 工作流", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=24, pady=(22, 14))
        steps = [
            ("1", "导出 API 工作流", "在 ComfyUI 中选择“保存（API 格式）”，得到可供程序调用的 JSON 文件。"),
            ("2", "添加到客户端", "点击“添加工作流”，选择 JSON 文件或包含工作流文件的文件夹。"),
            ("3", "确认输入与输出", "客户端会自动识别提示词、图片、输出节点和模型；只有识别不明确时才需要手动确认。"),
        ]
        for number, title, detail in steps:
            row = tk.Frame(panel, bg=C["card"])
            row.pack(fill="x", padx=24, pady=7)
            tk.Label(row, text=number, font=F["bold"], fg="#fff", bg=C["primary"], width=3, pady=6).pack(side="left", padx=(0, 12))
            text_box = tk.Frame(row, bg=C["card"])
            text_box.pack(side="left", fill="x", expand=True)
            tk.Label(text_box, text=title, font=F["h2"], fg=C["text"], bg=C["card"]).pack(anchor="w")
            tk.Label(text_box, text=detail, wraplength=520, justify="left", font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", pady=(4, 0))

        note = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        note.pack(fill="x", padx=24, pady=(12, 14))
        tk.Label(note, text="专业用户", font=F["bold"], fg=C["primary"], bg=C["hover"]).pack(anchor="w", padx=12, pady=(9, 2))
        tk.Label(note, text="导入后可在“查看参数”中检查英文调用名称、公开输入、输出节点和工作流结构。", font=F["small"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", padx=12, pady=(0, 9))
        self._button(panel, "开始添加工作流", lambda: (popup.destroy(), self._show_workflow_upload_dialog()), "primary", width=132).pack(anchor="e", padx=24, pady=(0, 18))

    @staticmethod
    def _comfyui_update_task_active(health: dict | None) -> bool:
        task = (health or {}).get("current_task") or {}
        return str(task.get("status") or "").strip().lower() in {
            "reserving",
            "queued",
            "pending",
            "submitted",
            "running",
        }

    @staticmethod
    def _activity_lock_busy(lock) -> bool:
        if lock is None:
            return False
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    def _comfyui_update_local_activity_reason(self) -> str:
        """Inspect local writers after the maintenance reservation is held."""
        if self._comfyui_update_task_active(self.__dict__.get("_last_health", {})):
            return "当前有生成任务正在提交或执行"
        if self._activity_lock_busy(self.__dict__.get("_backend_action_lock")):
            name = self.__dict__.get("_backend_action_name") or "后台操作"
            return f"{name}正在进行"
        if self._activity_lock_busy(self.__dict__.get("_workflow_management_lock")):
            name = self.__dict__.get("_workflow_operation_name") or "工作流导入或维护"
            return f"{name}正在进行"
        lock, transfers = self._model_transfer_state()
        with lock:
            if self.__dict__.get("_model_import_in_progress", False):
                return "模型导入正在进行"
            if transfers:
                return "模型下载或维护正在进行"
        return ""

    def _comfyui_update_live_queue_reason(self) -> str:
        """Query ComfyUI itself; cached API health is not authoritative here."""
        try:
            payload = ComfyUIClient(COMFY_BASE).get_queue_status()
        except Exception as exc:
            try:
                owned_running = self._process_supervisor.is_running("comfyui")
            except Exception:
                owned_running = True
            if owned_running:
                return f"无法实时确认 ComfyUI 队列是否空闲：{str(exc)[:240]}"
            return ""
        if not isinstance(payload, dict):
            return "ComfyUI 返回了无法识别的队列状态"
        running = payload.get("queue_running") or []
        pending = payload.get("queue_pending") or []
        if not isinstance(running, (list, tuple)) or not isinstance(
            pending, (list, tuple)
        ):
            return "ComfyUI 返回了无法识别的队列状态"
        count = len(running) + len(pending)
        if count:
            return f"ComfyUI 队列中还有 {count} 个生成任务"
        return ""

    def _stop_api_submission_for_comfyui_update(self) -> str:
        """Close the local submission path before the final live queue check."""
        error = self._process_supervisor.terminate("api", timeout=8)
        if not self._process_supervisor.is_running("api"):
            self._api_proc = None
        port_ready, port_error = self._process_supervisor.prepare_port(API_PORT)
        errors = [value for value in (error, "" if port_ready else port_error) if value]
        return "；".join(errors)

    def _quiesce_comfyui_for_update(self) -> str:
        """Reserve local writers, close API submissions, then recheck live work."""
        for check in (
            self._comfyui_update_local_activity_reason,
            self._comfyui_update_live_queue_reason,
        ):
            reason = check()
            if reason:
                return reason
        stop_error = self._stop_api_submission_for_comfyui_update()
        if stop_error:
            raise RuntimeError(f"API 服务未能安全停止：{stop_error}")
        for check in (
            self._comfyui_update_local_activity_reason,
            self._comfyui_update_live_queue_reason,
        ):
            reason = check()
            if reason:
                return reason
        return ""

    def _set_comfyui_update_progress(
        self,
        dialog: dict,
        percent: float,
        stage: str,
        detail: str,
        *,
        indeterminate: bool = False,
        error: bool = False,
    ):
        self._stop_runtime_progress_activity(dialog)
        self._set_runtime_progress(dialog, percent, stage, detail, error=error)
        if indeterminate and not error:
            progress = dialog.get("progress")
            if progress is not None:
                progress.configure(mode="indeterminate")
                progress.start(12)

    @staticmethod
    def _comfyui_update_operation_id(staging_core: Path) -> str:
        match = re.fullmatch(
            r"\.comfyui-update-staging-([0-9a-f]{32})",
            Path(staging_core).name,
        )
        if not match:
            raise ValueError("ComfyUI 更新暂存目录名称无效")
        return match.group(1)

    def _write_comfyui_update_manifest(self, prepared) -> Path:
        base = BASE_DIR.resolve()
        staging = Path(prepared.staging_core).resolve(strict=False)
        if staging.parent != base:
            raise ValueError("ComfyUI 更新暂存目录不在客户端目录内")
        operation_id = self._comfyui_update_operation_id(staging)
        manifest = base / f".comfyui-update-manifest-{operation_id}.json"
        temporary = base / f".comfyui-update-manifest-{operation_id}.json.tmp"
        if os.path.lexists(str(manifest)) or os.path.lexists(str(temporary)):
            raise FileExistsError("ComfyUI 更新清单已存在，请重新尝试")
        payload = prepared.to_manifest()
        payload["status"] = "ready"
        payload["overlay_command"] = []
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, manifest)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return manifest

    @staticmethod
    def _cleanup_comfyui_update_paths(prepared=None, manifest: Path | None = None):
        """Remove only updater-owned direct children after a pre-handoff failure."""
        base = BASE_DIR.resolve()
        candidates: list[Path] = []
        if prepared is not None:
            for value in (
                getattr(prepared, "staging_core", None),
                getattr(prepared, "dependency_overlay", None),
                getattr(prepared, "overlay_requirements_file", None),
            ):
                if value:
                    candidates.append(Path(value))
        if manifest is not None:
            candidates.append(Path(manifest))
        allowed = re.compile(
            r"^\.comfyui-update-(?:staging|overlay)-[0-9a-f]{32}$|"
            r"^\.comfyui-update-(?:requirements|manifest)-[0-9a-f]{32}\.json$|"
            r"^\.comfyui-update-requirements-[0-9a-f]{32}\.txt$"
        )
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if resolved.parent != base or not allowed.fullmatch(resolved.name):
                continue
            if candidate.is_symlink() or (
                hasattr(candidate, "is_junction") and candidate.is_junction()
            ):
                continue
            try:
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink(missing_ok=True)
            except OSError:
                pass

    def _run_comfyui_overlay_prepare(self, command: list[str]):
        """Run pip with termination-aware semantics before staged files may be removed."""
        try:
            return self._process_supervisor.run_observed(
                "comfyui-update-prepare",
                list(command),
                cwd=str(BASE_DIR),
                env={
                    **os.environ,
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUTF8": "1",
                },
                stdin=subprocess.DEVNULL,
                timeout=900,
            )
        except Exception as exc:
            try:
                still_running = self._process_supervisor.is_running(
                    "comfyui-update-prepare"
                )
            except Exception:
                still_running = True
            if still_running:
                raise _ComfyUIOverlayProcessStillRunning(
                    "官方组件准备进程仍在运行，无法确认文件已释放；"
                    "客户端已保留暂存文件，请退出客户端后再重试。"
                ) from exc
            raise

    def _start_comfyui_update(self):
        """Update Git installs by fast-forward; package installs use the swap worker."""
        if self._shutting_down:
            return
        if self._comfyui_update_task_active(self._last_health):
            messagebox.showinfo(
                "暂不能更新 ComfyUI",
                "当前有生成任务正在执行。请等待任务完成后再点击更新。",
                parent=self,
            )
            return
        live_core = BASE_DIR / "runtime" / "ComfyUI"
        if not (live_core / "comfyui_version.py").is_file():
            messagebox.showinfo(
                "请先安装运行环境",
                "当前没有可更新的内置 ComfyUI，请先使用“修复运行环境”安装环境包。",
                parent=self,
            )
            self._open_runtime_maintenance()
            return
        git_installation = os.path.lexists(str(live_core / ".git"))

        reserved, running_before, reserve_error = self._reserve_runtime_maintenance()
        if not reserved:
            messagebox.showinfo(
                "环境维护正在进行",
                reserve_error or "已有环境维护任务正在进行，请稍候。",
                parent=self,
            )
            return

        try:
            dialog = self._create_runtime_progress_dialog(
                title="更新 ComfyUI",
                heading="正在更新 ComfyUI 核心",
                stage="准备更新",
                detail="正在安全停止本地服务",
                background_action_text="后台更新",
            )
        except Exception:
            self._end_runtime_maintenance(restart=False)
            raise
        popup = dialog["popup"]
        self._set_comfyui_update_progress(
            dialog,
            5,
            "准备更新",
            "正在安全停止本地服务",
            indeterminate=True,
        )

        def post_progress(
            percent: float,
            stage: str,
            detail: str,
            *,
            indeterminate: bool = False,
        ):
            self._post_to_ui(
                lambda: self._set_comfyui_update_progress(
                    dialog,
                    percent,
                    stage,
                    detail,
                    indeterminate=indeterminate,
                )
            )

        def finish_without_handoff(
            title: str,
            message: str,
            *,
            error: bool = False,
            open_runtime_maintenance: bool = False,
        ):
            def finish_on_ui():
                self._end_runtime_maintenance(restart=bool(running_before))
                self._close_maintenance_dialog(dialog)
                if error:
                    messagebox.showerror(title, message, parent=self)
                else:
                    messagebox.showinfo(title, message, parent=self)
                if open_runtime_maintenance:
                    self.after(700, self._open_runtime_maintenance)

            self._post_to_ui(finish_on_ui)

        def update_worker():
            prepared = None
            manifest = None
            handoff_started = False
            cleanup_safe = True
            try:
                activity_reason = self._quiesce_comfyui_for_update()
                if activity_reason:
                    finish_without_handoff(
                        "暂不能更新 ComfyUI",
                        f"{activity_reason}。请等待当前操作完成后再点击更新。",
                    )
                    return
                stop_error = self._stop_runtime_for_maintenance()
                if stop_error:
                    raise RuntimeError(f"后台服务未能安全停止：{stop_error}")
                post_progress(
                    12,
                    "检查官方稳定版",
                    "正在连接 ComfyUI 官方发布源",
                    indeterminate=True,
                )

                def report_download(done: int, total: int | None):
                    if total and total > 0:
                        percent = 18 + min(1.0, done / total) * 32
                        detail = (
                            f"已下载 {done / 1024 / 1024:.1f} / "
                            f"{total / 1024 / 1024:.1f} MB"
                        )
                        post_progress(percent, "下载官方核心", detail)
                    else:
                        detail = f"已下载 {done / 1024 / 1024:.1f} MB"
                        post_progress(
                            18,
                            "下载官方核心",
                            detail,
                            indeterminate=True,
                        )

                if git_installation:
                    post_progress(30, "更新 Git 安装", "检查本地修改并获取官方稳定版", indeterminate=True)
                    git_result = update_git_comfyui(live_core, python_executable=sys.executable, channel_callback=lambda detail:
                        post_progress(30, '更新 Git 安装', detail, indeterminate=True))
                    finish_without_handoff(
                        ("国内镜像暂无更新" if git_result.get('source') == 'domestic' else "ComfyUI 已是最新版")
                        if git_result['status'] == 'up_to_date' else "ComfyUI 更新完成",
                        f"Git 安装：v{git_result['version']}。现有分支、模型和自定义节点已保留。",
                    )
                    return
                prepared = prepare_comfyui_update(
                    BASE_DIR,
                    progress_callback=report_download,
                    channel_callback=lambda detail: post_progress(18, '连接更新渠道', detail, indeterminate=True),
                )
                if prepared.status == "up_to_date":
                    version = str(prepared.release_metadata.get("version") or "")
                    finish_without_handoff(
                        "国内镜像暂无更新" if prepared.release_metadata.get('source') == 'domestic' else "ComfyUI 已是最新版",
                        (f"当前版本无需更新；国内镜像已同步到 v{version}，可能晚于上游。"
                         if prepared.release_metadata.get('source') == 'domestic'
                         else f"当前已是官方最新稳定版 v{version}。"),
                    )
                    return
                if prepared.status == "full_environment_required":
                    reasons = "\n".join(prepared.dependency_plan.reasons[:5])
                    detail = f"\n\n原因：\n{reasons}" if reasons else ""
                    finish_without_handoff(
                        "需要更新完整运行环境",
                        "这个版本涉及 Torch/CUDA 或未审核依赖，已停止核心更新。"
                        "请改用“修复运行环境”安装对应环境包。"
                        + detail,
                        open_runtime_maintenance=True,
                    )
                    return

                if prepared.status == "overlay_build_required":
                    post_progress(
                        58,
                        "准备官方组件",
                        "正在下载并校验与新版配套的官方 wheel",
                        indeterminate=True,
                    )
                    overlay_result = self._run_comfyui_overlay_prepare(
                        list(prepared.overlay_command)
                    )
                    if overlay_result.returncode != 0:
                        raw_detail = overlay_result.stderr or overlay_result.stdout or b""
                        detail = (
                            raw_detail.decode("utf-8", errors="replace")
                            if isinstance(raw_detail, bytes)
                            else str(raw_detail)
                        ).strip()
                        raise RuntimeError(detail[-2000:] or "官方组件准备失败")
                    if prepared.overlay_requirements_file:
                        Path(prepared.overlay_requirements_file).unlink(missing_ok=True)
                elif prepared.status != "ready":
                    raise ComfyUIUpdateError(f"无法处理更新状态：{prepared.status}")

                post_progress(
                    82,
                    "准备安全切换",
                    "核心与官方组件已就绪，退出后将完成文件切换",
                )
                manifest = self._write_comfyui_update_manifest(prepared)
                if not self._show_manual_restart_notice(dialog, component="ComfyUI"):
                    if self._shutting_down:
                        return
                    raise RuntimeError("未能显示重启提示，ComfyUI 尚未切换")
                self._comfyui_update_worker_proc = launch_comfyui_update_worker(
                    manifest,
                    restart_client=None,
                    parent_pid=os.getpid(),
                )
                handoff_started = True
                self._post_to_ui(lambda: self._exit_for_runtime_update(popup))
            except _ComfyUIOverlayProcessStillRunning as exc:
                cleanup_safe = False
                finish_without_handoff(
                    "ComfyUI 更新未完成",
                    "更新尚未应用，本地 ComfyUI 保持原样。\n\n"
                    f"详细信息：{exc}",
                    error=True,
                )
            except Exception as exc:
                if cleanup_safe:
                    self._cleanup_comfyui_update_paths(prepared, manifest)
                finish_without_handoff(
                    "ComfyUI 更新失败",
                    ("Git 更新未完成，请检查下方原因。客户端没有执行强制重置。\n\n"
                     if git_installation else "更新未应用，本地 ComfyUI 保持原样。\n\n")
                    +
                    f"详细信息：{exc}",
                    error=True,
                )
            finally:
                if not handoff_started and self._shutting_down and cleanup_safe:
                    self._cleanup_comfyui_update_paths(prepared, manifest)

        threading.Thread(target=update_worker, daemon=True).start()

    def _install_runtime(self):
        """Compatibility entry: installation now lives in the maintenance center."""
        self._open_runtime_maintenance()

    def _show_runtime_maintenance(self):
        """Show advanced runtime maintenance without leaving the resources page."""
        existing = self._runtime_maintenance_popup
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            self._runtime_maintenance_popup = None

        missing = missing_runtime_paths(BASE_DIR)
        ready = not missing
        popup = tk.Toplevel(self)
        self._runtime_maintenance_popup = popup
        popup.title("运行环境维护")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        self._center_popup(popup, 650, 370)

        def close_popup():
            self._runtime_maintenance_popup = None
            try:
                popup.destroy()
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", close_popup)
        panel = self._card(popup, fill="both", expand=True, padx=16, pady=16)

        header = tk.Frame(panel, bg=C["card"])
        header.pack(fill="x", padx=22, pady=(20, 12))
        header_text = tk.Frame(header, bg=C["card"])
        header_text.pack(side="left", fill="x", expand=True)
        tk.Label(header_text, text="运行环境维护", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        tk.Label(
            header_text,
            text="环境与模型分开保存；修复环境不会删除模型、工作流和生成结果。",
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
        ).pack(anchor="w", pady=(5, 0))
        status_text = "环境文件完整" if ready else f"需要补齐 {len(missing)} 项"
        status_fg = C["success"] if ready else C["warn"]
        tk.Label(header, text=f"  {status_text}  ", font=F["small"], fg=status_fg,
                 bg=C["soft_success"] if ready else C["soft_warn"], padx=5, pady=4).pack(side="right")

        def maintenance_row(title, detail, actions):
            row = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
            row.pack(fill="x", padx=22, pady=5)
            text_box = tk.Frame(row, bg=C["hover"])
            text_box.pack(side="left", fill="both", expand=True, padx=14, pady=11)
            tk.Label(text_box, text=title, font=F["bold"], fg=C["text"], bg=C["hover"]).pack(anchor="w")
            tk.Label(text_box, text=detail, font=F["small"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", pady=(4, 0))
            action_box = tk.Frame(row, bg=C["hover"])
            action_box.pack(side="right", padx=12)
            for index, (label, command, variant, width) in enumerate(actions):
                self._button(action_box, label, command, variant, width=width).pack(
                    side="left", padx=(0 if index == 0 else 7, 0)
                )

        maintenance_row(
            "在线安装或修复",
            "自动下载已配置的环境包，校验后安装；拉取失败时提供手动下载地址。",
            [("开始修复", lambda: (close_popup(), self._install_runtime_from_mirror()), "primary", 86)],
        )
        maintenance_row(
            "使用本地环境包",
            "适合已经下载好 7z 环境包，或将环境包放在客户端同目录的情况。",
            [
                ("选择文件", lambda: (close_popup(), self._select_runtime()), "plain", 82),
                ("同目录查找", lambda: (close_popup(), self._install_local_runtime()), "plain", 92),
            ],
        )

        footer = tk.Frame(panel, bg=C["card"])
        footer.pack(fill="x", padx=22, pady=(14, 18))
        self._button(footer, "检查环境", lambda: (close_popup(), self._start_background_runtime_recheck()), "primary", width=88).pack(side="left")
        self._button(footer, "打开运行目录", self._open_runtime_dir, "plain", width=102).pack(side="left", padx=(8, 0))
        self._button(footer, "查看运行日志", self._open_logs_dir, "plain", width=102).pack(side="left", padx=(8, 0))
        self._button(footer, "关闭", close_popup, "plain", width=72).pack(side="right")

    def _import_models(self):
        if self._runtime_maintenance_active():
            self._footer_label.config(text="  正在维护运行环境，请稍候")
            return
        if self._model_import_in_progress:
            self._footer_label.config(text="  已有模型导入任务正在运行，请稍候")
            return
        if self._has_active_model_transfers():
            self._footer_label.config(text="  模型正在下载，完成后再导入已有模型")
            messagebox.showinfo(
                "模型下载进行中",
                "请等待当前模型下载完成后再导入，避免两个任务同时写入同一个模型文件。",
                parent=self,
            )
            return
        path = filedialog.askdirectory(title="选择模型目录")
        if not path:
            return
        src = Path(path)
        unsafe_files = unsafe_model_files(src)
        allow_unsafe = False
        if unsafe_files:
            examples = "\n".join(f"• {item.name}" for item in unsafe_files[:5])
            extra = "" if len(unsafe_files) <= 5 else f"\n另有 {len(unsafe_files) - 5} 个文件"
            allow_unsafe = messagebox.askyesno(
                "检测到高风险模型格式",
                "PT、PTH、BIN 和 CKPT 可能在加载时执行其中携带的代码。\n"
                "只有在确认文件来源可信时才应继续。\n\n"
                f"{examples}{extra}\n\n仍要导入这些文件吗？",
                parent=self,
            )
            if not allow_unsafe:
                self._footer_label.config(text="  已取消高风险模型导入")
                return
        models_dir = _models_dir()
        transfer_lock, transfers = self._model_transfer_state()
        with transfer_lock:
            if (
                self._runtime_maintenance_active()
                or transfers
                or self._model_import_in_progress
            ):
                self._footer_label.config(text="  另一个模型维护任务已开始，请稍候")
                return
            self._model_import_in_progress = True

        def report_progress(index: int, total: int, filename: str):
            if not self._shutting_down:
                self.after(
                    0,
                    lambda: self._footer_label.config(
                        text=f"  正在导入模型 {index}/{total}：{filename}"
                    ),
                )

        def finish(result: dict):
            with self._model_transfer_state()[0]:
                self._model_import_in_progress = False
            if self._shutting_down:
                return
            self._model_status = _check_models_status()
            self._set_light(
                "models",
                "online" if self._model_status.get("all_ok") else "offline",
                "完整" if self._model_status.get("all_ok") else "缺失",
            )
            self._update_model_display()
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.refresh(self._last_health)

            summary = (
                f"已导入 {result.get('imported', 0)} 个，"
                f"跳过 {result.get('skipped', 0)} 个，失败 {result.get('failed', 0)} 个"
            )
            if not result.get("found"):
                self._footer_label.config(text="  未找到支持的模型文件")
                messagebox.showwarning(
                    "未找到模型",
                    "所选目录中没有找到 safetensors、GGUF、PT、PTH、BIN 或 CKPT 模型文件。",
                    parent=self,
                )
            elif result.get("failed"):
                self._footer_label.config(text=f"  模型导入完成：{summary}")
                detail = "\n".join((result.get("errors") or [])[:5])
                messagebox.showwarning(
                    "部分模型导入失败",
                    f"{summary}\n\n{detail}",
                    parent=self,
                )
            else:
                self._footer_label.config(text=f"  模型导入完成：{summary}")

        def fail(message: str):
            with self._model_transfer_state()[0]:
                self._model_import_in_progress = False
            if self._shutting_down:
                return
            self._footer_label.config(text=f"  模型导入失败：{message}")
            messagebox.showerror("模型导入失败", message, parent=self)

        def _do_copy():
            try:
                result = import_model_directory(
                    src,
                    models_dir,
                    on_progress=report_progress,
                    allow_unsafe=allow_unsafe,
                )
            except Exception as exc:
                if not self._shutting_down:
                    self.after(0, lambda error=str(exc): fail(error))
                return
            if not self._shutting_down:
                self.after(0, lambda data=result: finish(data))

        self._footer_label.config(text="  正在扫描并导入模型...")
        threading.Thread(target=_do_copy, daemon=True).start()

    def _recheck_models(self):
        if self._shutting_down:
            return
        self._post_to_ui(lambda: self._set_light("models", "loading"))
        time.sleep(0.3)
        self._model_status = _check_models_status()
        clear_model_index_cache()
        if self._shutting_down:
            return
        if self._model_status["all_ok"]:
            self._post_to_ui(lambda: self._set_light("models", "online", "完整"))
        else:
            self._post_to_ui(lambda: self._set_light("models", "offline", "缺失"))
        self._post_to_ui(self._update_model_display)
        if hasattr(self, "_dashboard_pages"):
            self._post_to_ui(lambda: self._dashboard_pages.refresh(self._last_health))

    def _finish_runtime_recheck(self, result: dict):
        if self._shutting_down:
            return
        self._environment_status = dict(result)
        if result.get("ready"):
            self._set_light("env", "online", "可用")
            gpu_name = str(result.get("gpu_name") or "显卡环境正常")
            self._footer_label.config(text=f"  运行环境检查通过：{gpu_name}")
        elif result.get("package_ready"):
            self._set_light("env", "offline", "需处理")
            self._footer_label.config(text=f"  运行环境需要处理：{result.get('message') or '检查未通过'}")
        else:
            self._set_light("env", "offline", "未安装")
            self._footer_label.config(text=f"  {result.get('message') or '运行环境尚未安装'}")
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(self._last_health)

    def _restart_backend(self):
        """重启所有后台服务"""
        if not self._begin_backend_action("重启后台"):
            return
        self._footer_label.config(text="  正在重启后台服务...")
        self._clear_public_url()
        for key in ("comfyui", "api", "tunnel"):
            self._set_light(key, "loading", "重启中")
        try:
            threading.Thread(target=self._restart_backend_worker, daemon=True).start()
        except Exception:
            self._end_backend_action()
            raise

    def _restart_backend_worker(self):
        try:
            errors = self._shutdown_backend()
            if errors:
                self.after(
                    0,
                    lambda message=errors: self._report_backend_failure(
                        f"后台服务未能完全停止：{message}"
                    ),
                )
                return
            if self._shutting_down or self._runtime_maintenance_active():
                return
            self._startup_sequence()
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._report_backend_failure(
                    f"后台重启失败：{message}"
                ),
            )
        finally:
            self._end_backend_action()

    def _report_backend_failure(self, message: str):
        if self._shutting_down:
            return
        for key in ("comfyui", "api", "tunnel"):
            self._set_light(key, "offline", "重试可用")
        self._footer_label.config(text=f"  {message}")
        self._ensure_health_polling()

    def _begin_backend_action(self, action_name: str) -> bool:
        """Reserve one service lifecycle action without blocking the UI."""
        if self._shutting_down:
            return False
        if self.__dict__.get("_runtime_start_blocked", False):
            self.after(
                0,
                lambda: self._footer_label.config(
                    text="  自动恢复未完成，请先修复运行环境"
                ),
            )
            return False
        if self._runtime_maintenance_active():
            self.after(
                0,
                lambda: self._footer_label.config(
                    text="  正在维护运行环境，完成后会自动恢复后台服务"
                ),
            )
            return False
        lock = self._backend_action_lock
        if not lock.acquire(blocking=False):
            current = self._backend_action_name or "后台操作"
            self.after(
                0,
                lambda name=current: self._footer_label.config(
                    text=f"  {name}正在进行，请稍候"
                ),
            )
            return False
        if self._shutting_down or self._runtime_maintenance_active():
            lock.release()
            return False
        self._backend_action_name = str(action_name or "后台操作")
        return True

    def _end_backend_action(self):
        self._backend_action_name = ""
        try:
            self._backend_action_lock.release()
        except RuntimeError:
            pass

    # ══════════════════════════════════════════════════════
    # 后台启动 ComfyUI + API + Tunnel
    # ══════════════════════════════════════════════════════
    def _backend_env(self):
        python_exe = sys.executable  # 使用当前 venv Python（GUI 自己）
        from app.core.python_import_paths import ensure_portable_import_order
        ensure_portable_import_order(python_exe)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return python_exe, env

    def _backend_log_dir(self) -> Path:
        log_dir = _logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _open_backend_log(self, filename: str):
        """Append service output and keep one bounded previous log for diagnosis."""
        path = self._backend_log_dir() / filename
        try:
            if path.is_file() and path.stat().st_size >= MAX_BACKEND_LOG_BYTES:
                backup = path.with_name(f"{path.name}.1")
                backup.unlink(missing_ok=True)
                path.replace(backup)
        except OSError:
            pass
        return open(path, "a", encoding="utf-8", buffering=1)

    def _start_comfyui_service(self):
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            return
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                return
            return self._start_comfyui_service_unlocked()
        finally:
            lock.release()

    def _start_comfyui_service_unlocked(self):
        """只启动或复检 ComfyUI，不重启 API/Tunnel。"""
        if self._shutting_down:
            return
        self._set_light("comfyui", "loading")
        self._comfy_starting_until = time.time() + 150
        python_exe, env = self._backend_env()
        log_dir = self._backend_log_dir()
        # 确保 outputs 目录存在
        outputs_dir = _outputs_dir()
        outputs_dir.mkdir(parents=True, exist_ok=True)

        # ComfyUI 直接使用当前有效的生成结果目录，支持其他盘符和中文路径。
        port_ready, port_error = self._process_supervisor.prepare_port(COMFY_PORT)
        if not port_ready:
            self._comfy_proc = None
            self._set_light("comfyui", "offline", "端口占用")
            self._footer_label.config(text=f"  ComfyUI 无法启动：{port_error}")
            print(f"[GUI] ComfyUI start blocked: {port_error}")
        else:
            try:
                install_frontend_bridge(BASE_DIR / 'runtime' / 'ComfyUI')
            except Exception as exc:
                print(f"[Workflow] 原生转换扩展准备失败（不影响已有 API 工作流）：{exc}")
            comfy_log = self._open_backend_log("comfyui.log")
            comfy_command = [
                str(python_exe), "-s", "-B", "main.py",
                "--listen", "127.0.0.1", "--port", str(COMFY_PORT),
                "--disable-auto-launch",
                *_comfy_vram_args(),
                "--output-directory", str(outputs_dir),
                "--extra-model-paths-config", "extra_model_paths.yaml",
            ]
            try:
                self._comfy_proc = self._process_supervisor.launch(
                    "comfyui",
                    comfy_command,
                    cwd=str(BASE_DIR / "runtime" / "ComfyUI"),
                    env=env,
                    stdout=comfy_log, stderr=comfy_log,
                )
            finally:
                comfy_log.close()
            print(f"[GUI] ComfyUI starting (output={outputs_dir})...")

    def _start_api_service(self):
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            return
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                return
            return self._start_api_service_unlocked()
        finally:
            lock.release()

    def _start_api_service_unlocked(self):
        """只启动 API 服务，不重启 ComfyUI。API 内部会自行启动/恢复 Tunnel。"""
        if self._shutting_down:
            return
        self._clear_public_url()
        self._set_light("api", "loading")
        self._set_light("tunnel", "loading")
        python_exe, env = self._backend_env()
        log_dir = self._backend_log_dir()
        port_ready, port_error = self._process_supervisor.prepare_port(API_PORT)
        if not port_ready:
            self._api_proc = None
            self._set_light("api", "offline", "端口占用")
            self._set_light("tunnel", "offline", "未启动")
            self._footer_label.config(text=f"  API 无法启动：{port_error}")
            print(f"[GUI] API start blocked: {port_error}")
            return
        api_log = self._open_backend_log("api.log")
        try:
            self._api_proc = self._process_supervisor.launch(
                "api",
                [str(python_exe), "-s", "-B", str(BASE_DIR / "app" / "server.py")],
                cwd=str(BASE_DIR), env=env,
                stdout=api_log, stderr=api_log,
            )
        finally:
            api_log.close()
        print("[GUI] API server starting...")

    def _start_backend(self):
        """启动 ComfyUI 引擎，然后启动 API 服务器"""
        if self._shutting_down or self._runtime_maintenance_active():
            return
        self._set_light("comfyui", "loading")
        self._set_light("api", "loading")
        self._set_light("tunnel", "loading")
        self._start_comfyui_service()
        self._start_api_service()
        self._ensure_health_polling()

    def _ensure_health_polling(self):
        """确保只有一条健康检查轮询线程在跑。"""
        if self._shutting_down:
            self._poll_run = False
            return
        with self._health_poll_lock:
            if (
                self._poll_run
                and self._health_poll_thread
                and self._health_poll_thread.is_alive()
            ):
                return
            self._health_poll_generation += 1
            generation = self._health_poll_generation
            self._poll_run = True
            self._health_needs_full_refresh = True
            worker = threading.Thread(
                target=self._poll_health,
                args=(generation,),
                daemon=True,
            )
            self._health_poll_thread = worker
            try:
                worker.start()
            except Exception:
                self._poll_run = False
                raise

    def _invalidate_health_polling(self):
        """Retire the current poll before an intentional API stop/restart."""
        with self._health_poll_lock:
            self._health_poll_generation += 1
            self._poll_run = False

    def _health_poll_active(self, generation: int) -> bool:
        return (
            not self._shutting_down
            and self._poll_run
            and generation == self._health_poll_generation
        )

    def _finish_health_poll(self, generation: int) -> bool:
        """Stop only the current poll generation, never a replacement."""
        with self._health_poll_lock:
            if generation != self._health_poll_generation:
                return False
            self._poll_run = False
            return True

    def _next_health_event_sequence(self) -> int:
        with self._health_poll_lock:
            self._health_event_sequence = (
                int(getattr(self, "_health_event_sequence", 0)) + 1
            )
            return self._health_event_sequence

    def _reserve_health_event(self) -> tuple[int, int] | None:
        with self._health_poll_lock:
            if self._shutting_down or not self._poll_run:
                return None
            self._health_event_sequence = (
                int(getattr(self, "_health_event_sequence", 0)) + 1
            )
            return self._health_poll_generation, self._health_event_sequence

    def _claim_health_event(self, generation: int, sequence: int | None) -> bool:
        """Accept only the newest queued event from the active poll generation."""
        with self._health_poll_lock:
            if (
                self._shutting_down
                or not self._poll_run
                or generation != self._health_poll_generation
            ):
                return False
            if sequence is None:
                return True
            applied = int(getattr(self, "_health_event_applied", 0))
            if sequence <= applied:
                return False
            self._health_event_applied = sequence
            return True

    def _deliver_health_snapshot(
        self,
        generation: int,
        data: dict,
        first: bool = False,
        sequence: int | None = None,
    ):
        """Discard stale snapshots from old polls or older same-poll probes."""
        if not self._claim_health_event(generation, sequence):
            return
        needs_full_refresh = bool(
            first or getattr(self, "_health_needs_full_refresh", True)
        )
        if needs_full_refresh:
            self._on_first_health(data)
            self._health_needs_full_refresh = False
        else:
            self._on_health_update(data)

    def _deliver_health_failure(
        self,
        generation: int,
        sequence: int | None = None,
    ):
        """Apply a confirmed outage only while its poll is still authoritative."""
        if self._claim_health_event(generation, sequence):
            self._health_needs_full_refresh = True
            self._on_server_unreachable()

    def _deliver_health_degraded(
        self,
        generation: int,
        message: str = "",
        sequence: int | None = None,
    ):
        """Show a slow status refresh without declaring healthy services offline."""
        if self._claim_health_event(generation, sequence):
            self._on_health_degraded(message)

    @staticmethod
    def _probe_local_api_health(urlopen) -> bool:
        """Use the lightweight public health route to distinguish delay from outage."""
        import urllib.request as ur

        try:
            request = ur.Request(f"{API_BASE}/health", method="GET")
            response = urlopen(request, timeout=2)
            try:
                data = json.loads(response.read().decode("utf-8"))
            finally:
                closer = getattr(response, "close", None)
                if callable(closer):
                    closer()
            return bool(isinstance(data, dict) and data.get("status") == "ok")
        except Exception:
            return False

    @staticmethod
    def _health_degraded_message(error: Exception) -> str:
        try:
            code = int(getattr(error, "code", 0) or 0)
        except (TypeError, ValueError):
            code = 0
        if code in (401, 403):
            return "API 已启动，但访问密钥暂未同步，客户端会自动重试"
        if code == 429:
            return "API 已启动，状态检查较频繁，客户端会稍后重试"
        if code >= 500:
            return "API 已启动，但状态接口暂时异常，客户端会自动重试"
        return "API 正常运行，状态刷新稍慢，客户端会自动重试"

    @staticmethod
    def _health_retry_delay(error: Exception, default: float) -> float:
        try:
            code = int(getattr(error, "code", 0) or 0)
        except (TypeError, ValueError):
            code = 0
        if code != 429:
            return default
        headers = getattr(error, "headers", None)
        try:
            raw = headers.get("Retry-After") if headers is not None else None
        except Exception:
            return default
        try:
            return min(30.0, max(default, float(raw)))
        except (TypeError, ValueError):
            return default

    def _poll_health(self, generation: int | None = None):
        if generation is None:
            with self._health_poll_lock:
                if self._shutting_down:
                    self._poll_run = False
                    return
                self._health_poll_generation += 1
                generation = self._health_poll_generation
                self._poll_run = True
                self._health_needs_full_refresh = True
        if not self._health_poll_active(generation):
            return
        import urllib.request as ur

        ever_connected = False
        status_failures = 0
        api_liveness_failures = 0
        outage_reported = False
        while self._health_poll_active(generation):
            try:
                req = ur.Request(
                    f"{API_BASE}/v1/status",
                    method="GET",
                    headers=self._local_api_headers(),
                )
                resp = ur.urlopen(req, timeout=5)
                try:
                    data = json.loads(resp.read().decode())
                finally:
                    closer = getattr(resp, "close", None)
                    if callable(closer):
                        closer()
                if not self._health_poll_active(generation):
                    return
                full_refresh = not ever_connected or outage_reported
                ever_connected = True
                status_failures = 0
                api_liveness_failures = 0
                outage_reported = False
                sequence = self._next_health_event_sequence()
                self.after(
                    0,
                    lambda g=generation, d=data, first=full_refresh, s=sequence:
                    self._deliver_health_snapshot(
                        g,
                        d,
                        first=first,
                        sequence=s,
                    ),
                )
                time.sleep(3)
                continue
            except Exception as error:
                status_failures += 1
                api_alive = self._probe_local_api_health(ur.urlopen)
                if api_alive:
                    api_liveness_failures = 0
                    if status_failures == 3 or (
                        status_failures > 3 and (status_failures - 3) % 10 == 0
                    ):
                        sequence = self._next_health_event_sequence()
                        message = self._health_degraded_message(error)
                        self.after(
                            0,
                            lambda g=generation, m=message, s=sequence:
                            self._deliver_health_degraded(
                                g,
                                m,
                                sequence=s,
                            ),
                        )
                else:
                    api_liveness_failures += 1
                    outage_threshold = 3 if ever_connected else 60
                    should_report = (
                        api_liveness_failures == outage_threshold
                        or (
                            api_liveness_failures > outage_threshold
                            and (api_liveness_failures - outage_threshold) % 10 == 0
                        )
                    )
                    if should_report:
                        outage_reported = True
                        sequence = self._next_health_event_sequence()
                        self.after(
                            0,
                            lambda g=generation, s=sequence:
                            self._deliver_health_failure(g, sequence=s),
                        )
                default_delay = 3.0 if ever_connected or api_alive else 1.0
                retry_delay = self._health_retry_delay(error, default_delay)
                closer = getattr(error, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass
                time.sleep(retry_delay)

    def _on_first_health(self, data: dict):
        """首次获取健康状态"""
        if self._shutting_down:
            return
        self._last_health = data
        self._update_status(data)

        self._api_key = self._current_local_api_key()
        self._set_api_key(self._api_key)

        self._update_workflow_display(data)
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(data)
        self._refresh_saved_login_and_sync()

    def _on_health_update(self, data: dict):
        if self._shutting_down:
            return
        self._last_health = data
        self._update_status(data)
        # Every health snapshot can move a workflow from "loading" to its real
        # dependency state. The display fingerprint prevents unchanged polls
        # from rebuilding the tree, so startup refreshes automatically without
        # bringing back the old periodic flicker.
        self._update_workflow_display(data)
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(data)

        # 进度更新
        task = data.get("current_task")
        self._update_task_display(task)

    def _update_status(self, data: dict):
        url = data.get("base_url", "")
        tunnel_data = data.get("tunnel", {})
        comfy_data = data.get("comfyui", {})

        self._set_local_url(data.get("local_api") or self._local_url or API_BASE)
        if self.__dict__.get("_lan_url_label") is not None:
            addresses = data.get("lan_urls") or []
            self._lan_url = addresses[0] if addresses else ""
            self._lan_url_label.config(text=self._lan_url or "局域网不可用，请用公网 URL")

        self._set_light("api", "online")

        ts = tunnel_data.get("status", "offline")
        if ts == "online":
            self._set_light("tunnel", "online")
        elif ts == "unavailable":
            self._set_light("tunnel", "offline", self._tunnel_error_label(tunnel_data.get("error", "")))
        elif ts in ("starting", "retrying"):
            self._set_light("tunnel", "loading")
        else:
            self._set_light("tunnel", "offline", "未连接")

        if ts != "online" or not url:
            if self._tunnel_url:
                self._clear_public_url()

        cs = comfy_data.get("status", "offline")
        if cs == "online":
            self._set_light("comfyui", "online")
        else:
            comfy_starting = (
                self._comfy_proc
                and self._comfy_proc.poll() is None
                and time.time() < self._comfy_starting_until
            )
            if cs == "offline" and comfy_starting:
                self._set_light("comfyui", "loading", "启动中")
            else:
                self._set_light("comfyui", "offline" if cs == "offline" else "loading")

        if ts == "online" and url and url != self._tunnel_url:
            self._tunnel_url = url
            self._set_public_url(url)
            self._initial_session_sync_done = False
            if self._server_mode == "logged_in" and self._server_session_token and self._api_key:
                threading.Thread(target=self._refresh_saved_login_and_sync, daemon=True).start()

        if ts == "online":
            parts = ["公网连接已建立"]
        elif ts in ("starting", "retrying"):
            parts = ["正在准备公网连接"]
        elif ts == "unavailable":
            error = str(tunnel_data.get("error") or "").strip()
            parts = [f"公网连接：{error or '连接失败'}"]
        else:
            parts = ["公网连接未建立"]
        self._footer_label.config(text="  " + "  ".join(parts))

    def _tunnel_error_label(self, error: str) -> str:
        text = str(error or "").strip()
        lowered = text.lower()
        if "cloudflared not found" in lowered or "not found:" in lowered:
            return "未安装"
        if "not reachable" in lowered:
            return "公网不可达"
        if "failed to obtain tunnel url" in lowered:
            return "获取 URL 失败"
        if text:
            return "连接失败"
        return "未连接"

    def _set_light(self, key: str, status: str, custom_text: str = ""):
        """设置状态灯
        status: "loading" | "online" | "offline"
        """
        group = self._light_groups.get(key)
        if not group:
            return
        dot, dot_id, lbl, status_lbl = group[:4]

        self._light_states[key] = status

        if status == "online":
            color = C["success"]
            text = custom_text or "已连接"
        elif status == "loading":
            color = C["warn"]
            text = custom_text or "加载中..."
        else:
            color = C["error"]
            text = custom_text or "失败"

        dot.itemconfig(dot_id, fill=color)
        if lbl:
            lbl.config(fg=C["text"])
        if status_lbl:
            status_lbl.config(text=text, fg=color)
        if len(group) >= 5:
            retry_btn = group[4]
            if status == "offline":
                if not retry_btn.winfo_ismapped():
                    retry_btn.pack(side="left", padx=(4, 0))
            elif retry_btn.winfo_ismapped():
                retry_btn.pack_forget()

    def _retry_component(self, key: str):
        if self._shutting_down:
            return
        if self._runtime_maintenance_active() and key in {"env", "comfyui", "api", "tunnel"}:
            self._footer_label.config(text="  正在维护运行环境，请稍候")
            return
        if key == "models":
            threading.Thread(target=self._recheck_models, daemon=True).start()
            return
        if key == "env":
            self._start_background_runtime_recheck()
            return
        if key == "comfyui":
            threading.Thread(target=self._retry_comfyui_service, daemon=True).start()
            return
        if key == "api":
            threading.Thread(target=self._retry_api_service, daemon=True).start()
            return
        if key == "tunnel":
            threading.Thread(target=self._retry_tunnel_service, daemon=True).start()
            return
        if key == "server":
            threading.Thread(
                target=lambda: self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES),
                daemon=True,
            ).start()
            return
        self._request_backend_start("启动后台")

    def _retry_env_check(self):
        if self._shutting_down or self._runtime_maintenance_active():
            return
        missing = missing_runtime_paths(BASE_DIR)
        result = {
            "package_ready": not missing,
            "ready": False,
            "missing": missing,
            "gpu_name": "",
            "message": "",
        }
        if missing:
            result["message"] = f"运行环境不完整，缺少 {len(missing)} 项"
            if not self._shutting_down:
                self.after(0, lambda data=result: self._finish_runtime_recheck(data))
            return

        system_info = _check_system_env(
            lambda args, **kwargs: self._run_runtime_probe(
                "environment-check", args, **kwargs
            )
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return
        torch_info = _check_torch(
            BASE_DIR / "runtime" / "python" / "python.exe",
            lambda args, **kwargs: self._run_runtime_probe(
                "torch-check", args, **kwargs
            ),
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        result["gpu_name"] = str(torch_info.get("gpu_name") or system_info.get("gpu_name") or "")
        result["ready"] = bool(system_info.get("success") and torch_info.get("success"))
        if not result["ready"]:
            result["message"] = str(
                torch_info.get("error")
                or system_info.get("error")
                or "显卡或 PyTorch 检查未通过"
            )
        self.after(0, lambda data=result: self._finish_runtime_recheck(data))

    def _retry_comfyui_service(self):
        if not self._begin_backend_action("ComfyUI 重试"):
            return
        try:
            self.after(0, lambda: self._set_light("comfyui", "loading", "重试中"))
            error = self._kill_proc(self._comfy_proc, "ComfyUI")
            if error:
                self.after(0, lambda e=error: self._set_light("comfyui", "offline", "关闭失败"))
                self.after(0, lambda e=error: self._footer_label.config(text=f"  ComfyUI 无法重启：{e}"))
                return
            self._comfy_proc = None
            if not self._shutting_down and not self._runtime_maintenance_active():
                if self._run_ui_backend_step(self._start_comfyui_service):
                    self._ensure_health_polling()
        finally:
            self._end_backend_action()

    def _retry_api_service(self):
        if not self._begin_backend_action("API 重试"):
            return
        try:
            self.after(0, lambda: self._set_light("api", "loading", "重试中"))
            self._invalidate_health_polling()
            error = self._kill_proc(self._api_proc, "API")
            if error:
                self._ensure_health_polling()
                self.after(0, lambda e=error: self._set_light("api", "offline", "关闭失败"))
                self.after(0, lambda e=error: self._footer_label.config(text=f"  API 无法重启：{e}"))
                return
            self._api_proc = None
            if not self._shutting_down and not self._runtime_maintenance_active():
                if self._run_ui_backend_step(self._start_api_service):
                    self._ensure_health_polling()
        finally:
            self._end_backend_action()

    def _retry_tunnel_service(self):
        if not self._begin_backend_action("公网连接重试"):
            return
        try:
            import urllib.request as ur

            self.after(0, lambda: self._set_light("tunnel", "loading", "重试中"))
            if not self._run_ui_backend_step(self._clear_public_url):
                return
            req = ur.Request(
                f"{API_BASE}/v1/tunnel/restart",
                data=b"{}",
                method="POST",
                headers=self._local_admin_headers(),
            )
            raw = ur.urlopen(req, timeout=20).read()
            result = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(result, dict) or not result.get("ok"):
                detail = result.get("error") if isinstance(result, dict) else ""
                raise RuntimeError(str(detail or "公网连接未能开始重连"))
            if not self._shutting_down:
                self._ensure_health_polling()
        except Exception as exc:
            if not self._shutting_down:
                self.after(0, lambda e=str(exc): self._set_light("tunnel", "offline", "重试失败"))
                self.after(0, lambda e=str(exc): self._footer_label.config(text=f"  公网连接重试失败：{e}"))
        finally:
            self._end_backend_action()

    def _on_server_unreachable(self):
        if self._shutting_down:
            return
        self._set_light("api", "offline")
        self._set_light("tunnel", "offline")
        self._set_light("comfyui", "offline")
        self._clear_public_url()
        self._key_label.config(text="（无法连接）")
        self._url_label.config(text="API 服务启动失败")
        self._footer_label.config(text="服务启动失败 — 请在设置中打开运行日志")

    def _on_health_degraded(self, message: str = ""):
        """The API answered /health, so retain the last trusted component state."""
        if self._shutting_down:
            return
        self._set_light("api", "online", "状态同步中")
        self._footer_label.config(
            text=f"  {message or 'API 正常运行，状态刷新稍慢，客户端会自动重试'}"
        )

    # ══════════════════════════════════════════════════════
    # 工作流更新
    # ══════════════════════════════════════════════════════
    def _workflow_display_fingerprint(
        self,
        workflows: list,
        default_workflow_id: str = "",
    ) -> str:
        """Return a stable snapshot of the state rendered by the workflow tree."""
        payload = {
            "workflows": workflows if isinstance(workflows, list) else [],
            "default_workflow_id": str(default_workflow_id or ""),
            "models": getattr(self, "_model_status", {}),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _update_workflow_display(self, data: dict):
        """按本地 API 返回的工作流列表动态刷新界面。"""
        if not hasattr(self, "_wf_sections"):
            return
        previous_scroll = 0.0
        if hasattr(self, "_workflow_canvas"):
            try:
                previous_scroll = self._workflow_canvas.yview()[0]
            except (IndexError, tk.TclError):
                previous_scroll = 0.0
        remote_workflows = data.get("workflows") if isinstance(data, dict) else []
        if isinstance(remote_workflows, int) or not isinstance(remote_workflows, list):
            remote_workflows = []
        try:
            local_workflows = read_local_workflow_catalog(
                _workflows_dir(),
                BASE_DIR / "runtime" / "workflow_config.json",
            )
        except (OSError, ValueError):
            local_workflows = []
        workflows = merge_workflow_catalog(local_workflows, remote_workflows)
        default_workflow_id = (
            str(data.get("default_workflow_id") or data.get("default_workflow") or "").strip()
            if isinstance(data, dict)
            else ""
        )
        fingerprint = self._workflow_display_fingerprint(workflows, default_workflow_id)
        if fingerprint == getattr(self, "_workflow_display_fingerprint_value", ""):
            return

        for box in self._wf_sections.values():
            for child in box.winfo_children()[1:]:
                child.destroy()
        self._wf_rows = {}

        counts = {"image": 0, "video": 0, "text": 0}
        available_count = 0
        total_count = 0

        valid_ids = set()
        first_valid_id = ""
        for workflow in workflows:
            if not isinstance(workflow, dict):
                continue
            wf_id = str(workflow.get("id") or "").strip()
            if not wf_id:
                continue
            group = self._workflow_group_for_display(workflow)
            parent_box = self._wf_sections.get(group) or self._wf_sections["image"]
            self._add_workflow_row(parent_box, workflow, group)
            counts[group] = counts.get(group, 0) + 1
            valid_ids.add(wf_id)
            if not first_valid_id:
                first_valid_id = wf_id
            total_count += 1
            if workflow.get("enabled", True) and self._workflow_model_available(workflow):
                available_count += 1

        empty_labels = {
            "image": "暂无图片工作流",
            "video": "暂无视频工作流",
            "text": "暂无文字工作流",
        }
        for group, box in self._wf_sections.items():
            visible_count = counts.get(group, 0)
            if not visible_count:
                tk.Label(
                    box,
                    text=empty_labels.get(group, "暂无工作流"),
                    font=F["small"],
                    fg=C["muted"],
                    bg=C["card"],
                ).pack(anchor="w", pady=(3, 6), padx=(24, 0))

        if self._workflow_mode.get() not in valid_ids:
            if default_workflow_id in valid_ids:
                self._workflow_mode.set(default_workflow_id)
            else:
                self._workflow_mode.set(first_valid_id or "")
        if hasattr(self, "_model_hint_label"):
            self._model_hint_label.config(text="")
        if hasattr(self, "_workflow_scroll_content"):
            self._bind_workflow_mousewheel_tree(self._workflow_scroll_content)

            def restore_scroll_position():
                try:
                    self._workflow_canvas.configure(
                        scrollregion=self._workflow_canvas.bbox("all")
                    )
                    self._workflow_canvas.yview_moveto(previous_scroll)
                except tk.TclError:
                    pass

            self.after_idle(restore_scroll_position)
        self._workflow_display_fingerprint_value = fingerprint

    # ══════════════════════════════════════════════════════
    # 系统托盘
    # ══════════════════════════════════════════════════════
    def _create_tray_icon(self):
        try:
            from PIL import Image, ImageDraw
            import pystray

            icon_path = BASE_DIR / "icon.png"
            if icon_path.exists():
                img = Image.open(icon_path)
            else:
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=C["primary"])
                draw.text((18, 16), "AI", fill="white")

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._restore_from_tray, default=True),
                pystray.MenuItem("退出", lambda *_args: self.after(0, self._on_close)),
            )
            self._tray = pystray.Icon("ai_gateway", img, "灵境造片厂", menu)
            self._tray.run()
        except Exception as e:
            print(f"[Tray] 托盘创建失败: {e}")

    def _restore_from_tray(self):
        self.after(0, self._do_restore)

    def _do_restore(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None

    def _on_minimize(self, event):
        if self._shutting_down:
            return
        if self.state() == "iconic":
            self.withdraw()
            if self._tray is None:
                self._tray_thread = threading.Thread(target=self._create_tray_icon, daemon=True)
                self._tray_thread.start()

    # ══════════════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════════════
    def _copy(self, text: str):
        if not text or text in ("正在建立公网连接...", "生成中...", "—", "API 服务启动失败"):
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._footer_label.config(text="  已复制到剪贴板")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _center_popup(self, popup, w, h):
        popup.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - w) // 2
        py = self.winfo_y() + (self.winfo_height() - h) // 2
        popup.geometry(f"{w}x{h}+{px}+{py}")

    # ══════════════════════════════════════════════════════
    # 关闭确认 + 终止后台
    # ══════════════════════════════════════════════════════
    def _on_close(self):
        """Close the window only after every client-owned service is stopped."""
        if self._shutting_down:
            return
        confirmed = messagebox.askyesno(
            "确认退出",
            "确定要退出灵境造片厂吗？\n\n"
            "退出后将关闭：\n"
            "  - ComfyUI\n"
            "  - 本地 API 服务\n"
            "  - 公网连接\n"
            "  - 当前生成任务",
            parent=self,
        )
        if not confirmed:
            return

        self._shutting_down = True
        self._anim_running = False
        self._heartbeat_run = False
        self._poll_run = False
        if self._server_session_token:
            threading.Thread(target=self._notify_server_offline, daemon=True).start()

        popup = tk.Toplevel(self)
        popup.title("正在关闭")
        popup.configure(bg=C["surface"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        popup.protocol("WM_DELETE_WINDOW", lambda: None)
        self._center_popup(popup, 440, 315)

        title_label = tk.Label(
            popup,
            text="正在彻底关闭后台服务...",
            font=F["bold"],
            fg=C["text"],
            bg=C["surface"],
        )
        title_label.pack(pady=(24, 12))

        list_frame = tk.Frame(popup, bg=C["surface"])
        list_frame.pack(fill="x", padx=30)

        items = {}
        for name in ["ComfyUI", "API 服务", "公网连接", "下载与更新"]:
            row = tk.Frame(list_frame, bg=C["surface"])
            row.pack(fill="x", pady=3)
            status_lbl = tk.Label(
                row,
                text="等待...",
                font=F["small"],
                fg=C["warn"],
                bg=C["surface"],
                width=13,
                anchor="w",
            )
            status_lbl.pack(side="left")
            name_lbl = tk.Label(
                row,
                text=name,
                font=F["small"],
                fg=C["text2"],
                bg=C["surface"],
                anchor="w",
            )
            name_lbl.pack(side="left", padx=(8, 0))
            items[name] = status_lbl

        close_btn = tk.Button(
            popup,
            text="再次强制清理并退出",
            font=F["small"],
            bg=C["error"],
            fg="#fff",
            activebackground="#c0392b",
            relief="flat",
            bd=0,
            cursor="hand2",
            command=lambda: self._force_destroy(popup),
            state="disabled",
        )
        close_btn.pack(pady=(14, 0), ipadx=10, ipady=4)
        error_label = tk.Label(
            popup,
            text="",
            font=F["tiny"],
            fg=C["error"],
            bg=C["surface"],
            wraplength=380,
            justify="left",
        )
        error_label.pack(fill="x", padx=30, pady=(8, 0))
        popup._shutdown_title = title_label
        popup._shutdown_error = error_label
        popup._shutdown_button = close_btn

        def update_item(name: str, text: str, color: str):
            label = items.get(name)
            if label is not None:
                label.config(text=text, fg=color)

        def finish_success():
            title_label.config(text="后台服务已全部停止", fg=C["success"])
            self.after(250, lambda: self._finish_shutdown(popup))

        def _do_shutdown():
            self.after(0, lambda: update_item("ComfyUI", "关闭中...", C["warn"]))
            err = self._process_supervisor.terminate("comfyui", timeout=8)
            self.after(0, lambda e=err: update_item("ComfyUI", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            if not self._process_supervisor.is_running("comfyui"):
                self._comfy_proc = None

            self.after(0, lambda: update_item("API 服务", "关闭中...", C["warn"]))
            self.after(0, lambda: update_item("公网连接", "关闭中...", C["warn"]))
            api_error = self._process_supervisor.terminate("api", timeout=8)
            self.after(0, lambda e=api_error: update_item("API 服务", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            self.after(0, lambda e=api_error: update_item("公网连接", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            if not self._process_supervisor.is_running("api"):
                self._api_proc = None

            self.after(0, lambda: update_item("下载与更新", "关闭中...", C["warn"]))
            retry_results = self._process_supervisor.shutdown_all(timeout=8)
            workflow_idle = self._wait_for_workflow_idle(timeout=20)
            remaining = self._process_supervisor.remaining()
            comfy_left = bool(remaining.get("comfyui"))
            api_left = bool(remaining.get("api"))
            self.after(0, lambda left=comfy_left: update_item("ComfyUI", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(0, lambda left=api_left: update_item("API 服务", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(0, lambda left=api_left: update_item("公网连接", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(
                0,
                lambda left=remaining: update_item(
                    "下载与更新",
                    "已停止 ✓" if not left and workflow_idle else "需要清理",
                    C["success"] if not left and workflow_idle else C["error"],
                ),
            )

            if remaining or not workflow_idle:
                details = [error for error in retry_results.values() if error]
                if not workflow_idle:
                    details.append("工作流导入或设置仍在安全收尾")
                if not details:
                    details = [
                        f"{role}: PID {', '.join(map(str, pids))}"
                        for role, pids in remaining.items()
                    ]
                message = "；".join(details)
                self.after(0, lambda m=message: title_label.config(text="仍有服务需要强制清理", fg=C["error"]))
                self.after(0, lambda m=message: error_label.config(text=m))
                self.after(0, lambda: close_btn.config(state="normal"))
            else:
                self.after(0, finish_success)

        threading.Thread(target=_do_shutdown, daemon=True).start()

    def _kill_proc(self, proc, name: str) -> str:
        """Stop a registered service tree (used by component retries)."""
        role = "comfyui" if "comfy" in str(name).lower() else "api"
        error = self._process_supervisor.terminate(role, timeout=8)
        if role == "comfyui":
            self._comfy_proc = None
        else:
            self._api_proc = None
        return error

    def _shutdown_backend(self):
        """关闭所有后台进程（用于重启）"""
        self._poll_run = False
        errors = []
        for role in ("comfyui", "api"):
            error = self._process_supervisor.terminate(role, timeout=8)
            if error:
                errors.append(f"{role}: {error}")
        if not self._process_supervisor.is_running("comfyui"):
            self._comfy_proc = None
        if not self._process_supervisor.is_running("api"):
            self._api_proc = None
        return "；".join(errors)

    def _final_process_cleanup(self, timeout=4):
        """Non-UI, idempotent cleanup used by every final exit path."""
        self._heartbeat_run = False
        self._poll_run = False
        self._shutting_down = True
        results = {}
        try:
            results = self._process_supervisor.shutdown_all(timeout=timeout)
        except Exception as exc:
            print(f"[Shutdown] final cleanup failed: {exc}")
            results = {"supervisor": str(exc)}
        remaining = self._process_supervisor.remaining()
        for role, pids in remaining.items():
            results[role] = results.get(role) or f"PID {', '.join(map(str, pids))} 仍在运行"
        if not self._wait_for_workflow_idle(timeout=timeout):
            results["workflow"] = "工作流导入或设置仍在安全收尾"
        try:
            RuntimeState(BASE_DIR / "runtime").set_offline()
        except Exception as exc:
            # Runtime state does not own a process; report it without claiming
            # that background cleanup failed.
            print(f"[Shutdown] failed to persist offline state: {exc}")
        return {role: error for role, error in results.items() if error}

    @staticmethod
    def _cleanup_error_text(errors):
        return "；".join(f"{role}: {error}" for role, error in errors.items())

    def _show_shutdown_failure(self, popup, errors):
        message = self._cleanup_error_text(errors)
        print(f"[Shutdown] owned processes remain: {message}")
        try:
            if popup is not None and popup.winfo_exists():
                popup._shutdown_title.config(text="仍有后台程序未关闭", fg=C["error"])
                popup._shutdown_error.config(text=message)
                popup._shutdown_button.config(text="再次强制清理并退出", state="normal")
                return
        except Exception:
            pass
        try:
            messagebox.showerror("暂时无法退出", f"后台程序尚未完全停止：\n{message}", parent=self)
        except Exception:
            pass

    def _finish_shutdown(self, popup=None):
        errors = self._final_process_cleanup(timeout=2)
        if errors:
            self._show_shutdown_failure(popup, errors)
            return
        self._complete_destroy(popup)

    def _complete_destroy(self, popup=None):
        try:
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.cancel_pending()
        except Exception:
            pass
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        self._tray = None
        try:
            if popup is not None and popup.winfo_exists():
                popup.grab_release()
                popup.destroy()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _do_destroy(self):
        errors = self._final_process_cleanup(timeout=4)
        if errors:
            self._show_shutdown_failure(None, errors)
            return False
        self._complete_destroy()
        return True

    def _force_destroy(self, popup):
        """Force means close owned Jobs now; it never bypasses cleanup."""
        self._poll_run = False
        self._heartbeat_run = False

        def cleanup_and_finish():
            errors = self._final_process_cleanup(timeout=6)
            if errors:
                self.after(0, lambda e=errors: self._show_shutdown_failure(popup, e))
                return
            self._comfy_proc = None
            self._api_proc = None
            self.after(0, lambda: self._complete_destroy(popup))

        try:
            popup._shutdown_title.config(text="正在再次清理...", fg=C["warn"])
            popup._shutdown_error.config(text="")
            popup._shutdown_button.config(state="disabled")
        except Exception:
            pass

        threading.Thread(target=cleanup_and_finish, daemon=True).start()


def main():
    if not _acquire_instance_lock():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "灵境造片厂已在运行",
            "已经打开了一个灵境造片厂客户端。\n\n请使用已打开的窗口，避免多个客户端同时同步 URL / Key。",
            parent=root,
        )
        root.destroy()
        return
    app = None
    try:
        app = GatewayApp()
        app.mainloop()
    finally:
        if app is not None:
            app._final_process_cleanup()
        _release_instance_lock()


if __name__ == "__main__":
    main()
