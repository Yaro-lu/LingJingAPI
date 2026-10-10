"""Local UI preferences and cheap, snapshot-only beginner service status."""

from __future__ import annotations

import ipaddress
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from app.core.quick_repair import QUICK_REPAIR_PROFILES, profile_for_vram, profile_model_items
from app.core.model_maintenance import model_file_ready
from app.core.runtime_package import RUNTIME_PACKAGE_NAME, RUNTIME_PACKAGE_SIZE


def load_interface_preferences(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    profile = data.get("profile")
    return {
        "mode": "expert" if data.get("mode") == "expert" else "beginner",
        "onboarding_complete": data.get("onboarding_complete") is True,
        "profile": profile if isinstance(profile, str) and profile in QUICK_REPAIR_PROFILES else "",
    }


def save_interface_preferences(path: Path, preferences: dict) -> None:
    # Persist UI choices only; credentials remain in the existing DPAPI store.
    profile = preferences.get("profile")
    data = {
        "mode": "expert" if preferences.get("mode") == "expert" else "beginner",
        "onboarding_complete": preferences.get("onboarding_complete") is True,
        "profile": profile if isinstance(profile, str) and profile in QUICK_REPAIR_PROFILES else "",
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def automatic_repair_profile(vram_mb: int = 0, pending: str = "") -> str:
    if pending in QUICK_REPAIR_PROFILES:
        return pending
    # If the probe failed, use the smaller 4B / regular H3 plan, without asking
    # beginners for configuration. The existing environment check still blocks
    # unsupported hardware before downloading any models.
    return profile_for_vram(vram_mb) or "above_12gb"


def beginner_service_state(
    *, environment: dict, lights: dict, health: dict, profile: str,
    onboarding_complete: bool, api_key_ready: bool, repair_phase: str = "",
    maintaining: bool = False, start_blocked: bool = False, backend_error: str = "",
) -> tuple[str, str, str]:
    """No filesystem walks, model hashing, subprocesses or network requests."""
    if maintaining or repair_phase not in ("", "complete", "failed"):
        return "starting", "启动中…", "正在准备环境与模型，完成后自动启动服务。"
    if start_blocked:
        return "failed", "启动失败", "运行环境恢复未完成，请点击一键修复。"
    if repair_phase == "failed":
        return "failed", "启动失败", "修复尚未完成，请点击一键修复重试。"
    if backend_error:
        return "failed", "启动失败", backend_error[:220]
    if environment.get("ready") is False:
        message = str(environment.get("message") or "运行环境尚未就绪")
        return "repair", "需要一键修复", message
    if any(lights.get(key) == "offline" for key in ("api", "comfyui")):
        return "failed", "启动失败", "生成服务暂不可用，请点击一键修复。"
    if not environment.get("ready") or any(lights.get(key) != "online" for key in ("api", "comfyui")):
        return "starting", "启动中…", "正在检查环境并启动生成服务。"
    if (health.get("comfyui") or {}).get("status") != "online":
        return "starting", "启动中…", "正在同步服务状态。"
    available = {
        item.get("id") for item in (health.get("workflows") or [])
        if isinstance(item, dict) and item.get("available")
    }
    if "llm_qwen3_text_gen" not in available:
        return "repair", "需要一键修复", "Qwen3.5 文字工作流尚未就绪，请补齐缺失项。"
    if not api_key_ready:
        return "starting", "启动中…", "正在准备生成接口 Key。"
    # A public tunnel is optional. LAN/local generation works without it.
    complete = any(all(key in available for key in spec["workflows"]) for spec in QUICK_REPAIR_PROFILES.values())
    return "ready", "启动成功", "文字、图片、视频服务已就绪。" if complete else "服务已就绪，可使用已经安装的工作流。"


def bounded_file_size(path: Path, maximum: int) -> int:
    try:
        return min(maximum, max(0, path.stat().st_size)) if path.is_file() else 0
    except OSError:
        return 0


def beginner_download_plan(base_dir: Path, models_dir: Path, profile: str, environment_ready: bool) -> dict:
    """Selected files only, no directory scans or hashes. Run off the UI thread.

    This is an estimate until repair verifies SHA256. Disk space after extraction
    is different from the compressed download volume shown here.
    """
    items = profile_model_items(profile)
    model_total = sum(int(item["size_bytes"]) for item in items)
    model_have = 0
    for item in items:
        target, size = Path(models_dir) / item["path"], int(item["size_bytes"])
        model_have += size if model_file_ready(target, size) else bounded_file_size(target.with_name(target.name + ".part"), size)
    runtime_have = RUNTIME_PACKAGE_SIZE if environment_ready else max(
        bounded_file_size(Path(base_dir) / RUNTIME_PACKAGE_NAME, RUNTIME_PACKAGE_SIZE),
        bounded_file_size(Path(base_dir) / "cache" / RUNTIME_PACKAGE_NAME, RUNTIME_PACKAGE_SIZE),
        bounded_file_size(Path(base_dir) / "cache" / (RUNTIME_PACKAGE_NAME + ".part"), RUNTIME_PACKAGE_SIZE),
    )
    return {
        "runtime_bytes": RUNTIME_PACKAGE_SIZE, "model_bytes": model_total,
        "total_bytes": RUNTIME_PACKAGE_SIZE + model_total,
        "runtime_have": runtime_have, "model_have": model_have,
        "remaining_bytes": RUNTIME_PACKAGE_SIZE + model_total - runtime_have - model_have,
    }


def beginner_download_summary(plan: dict) -> str:
    gib = lambda count: f"{count / 1024**3:.2f} GB"
    return (
        f"完整下载量约 {gib(plan['total_bytes'])}（环境 {gib(plan['runtime_bytes'])} + 模型 {gib(plan['model_bytes'])}）\n"
        f"复用已有文件和下载断点后，本次预计还需 {gib(plan['remaining_bytes'])}。校验不通过会重新下载；安装后占用另计。"
    )


def beginner_model_download_progress(plan: dict, controls: list[dict]) -> tuple[int, float]:
    """Weight files by bytes, keeping reused files and resumable data credited."""
    transfer_total = sum(int((control.get("item") or {}).get("size_bytes") or 0) for control in controls)
    completed = max(0, plan["model_bytes"] - transfer_total) + plan["runtime_bytes"]
    for control in controls:
        size = int((control.get("item") or {}).get("size_bytes") or 0)
        if control.get("state") == "done":
            completed += size
        else:
            completed += min(size, max(0, int(control.get("downloaded_bytes", size * float(control.get("progress_percent") or 0) / 100))))
    completed = min(plan["total_bytes"], completed)
    return completed, completed * 100 / max(1, plan["total_bytes"])


def build_example_launch_url(local_url: str, api_key: str) -> str:
    parsed = urlsplit(local_url)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (
        parsed.scheme not in ("http", "https") or not loopback or not parsed.port
        or parsed.username or parsed.password or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment or not api_key
    ):
        raise ValueError("本机服务尚未准备就绪")
    # Fragments are not sent to the server or included in HTTP access logs.
    return local_url.rstrip("/") + "/#" + urlencode({"lingjing_key": api_key})
