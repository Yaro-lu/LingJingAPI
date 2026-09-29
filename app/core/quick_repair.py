"""Small, local-only plan and state for the first-run repair assistant."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from app.core.model_maintenance import (
    MODEL_REQUIREMENTS, model_file_ready, model_file_sha256_matches,
)
from app.core.model_mappings import resolve_model_file


VRAM_THRESHOLD_MB = 12 * 1024
QUICK_REPAIR_PROFILES = {
    "above_12gb": {
        "label": "显存大于 12 GB",
        "groups": ("Qwen3.5", "Flux2 Klein 4B", "H3 Regular"),
        "workflows": (
            "llm_qwen3_text_gen",
            "flux2_klein_4b_v1",
            "lingjing_h3_3060ti_regular_it2v",
        ),
        "names": ("Qwen3.5", "FLUX.2 Klein 4B", "性价比H3"),
    },
    "at_most_12gb": {
        "label": "显存小于等于 12 GB",
        "groups": ("Qwen3.5", "Flux2", "H3 High Quality"),
        "workflows": (
            "llm_qwen3_text_gen",
            "flux_t2i_v1",
            "lingjing_h3_4070_hq_it2v",
        ),
        "names": ("Qwen3.5", "FLUX.2 Klein 9B", "高质量H3"),
    },
}


def profile_for_vram(vram_mb: int | None) -> str | None:
    """Do not guess a hardware tier when the GPU probe failed."""
    try:
        size = int(vram_mb or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return "above_12gb" if size > VRAM_THRESHOLD_MB else "at_most_12gb"


def profile_model_items(profile: str, requirements: dict | None = None) -> list[dict]:
    """Return unique, pinned files for all three workflows in this tier."""
    if profile not in QUICK_REPAIR_PROFILES:
        raise ValueError("未知的显存档位")
    catalog = requirements if requirements is not None else MODEL_REQUIREMENTS
    result: list[dict] = []
    seen: set[str] = set()
    for group in QUICK_REPAIR_PROFILES[profile]["groups"]:
        spec = catalog.get(group)
        if not isinstance(spec, dict):
            raise ValueError(f"模型清单缺少 {group}")
        for item in spec.get("items", []):
            relative = str(item.get("path") or "").strip()
            if not relative or relative in seen:
                continue
            if not item.get("url") or not item.get("sha256") or not item.get("size_bytes"):
                raise ValueError(f"模型来源或校验信息不完整：{relative}")
            seen.add(relative)
            result.append(dict(item))
    return result


def missing_profile_items(models_dir: Path, profile: str) -> list[dict]:
    """Startup check uses only file metadata; large-file hashes run on download."""
    root = Path(models_dir)
    return [
        item
        for item in profile_model_items(profile)
        if not model_file_ready(root / item["path"], item["size_bytes"])
    ]


def inspect_existing_profile_models(
    models_dir: Path, profile: str, on_file=None, cancelled=None
) -> tuple[list[dict], list[Path]]:
    """Hash existing selected files only after a user starts repair.

    Corrupt canonical files are returned for reversible quarantine by the GUI;
    mapped external files are never modified by the repair assistant.
    """
    root = Path(models_dir)
    items = profile_model_items(profile)
    missing: list[dict] = []
    invalid: list[Path] = []
    for index, item in enumerate(items, 1):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("模型检查已取消")
        if on_file is not None:
            on_file(index, len(items), item["path"])
        target = root / item["path"]
        if not model_file_ready(target, item["size_bytes"]):
            resolved = resolve_model_file(target)
            if resolved.is_file():
                if resolved.resolve() != target.resolve():
                    raise ValueError(
                        f"已映射模型的文件大小不匹配：{item['path']}。"
                        "请在模型管理中重新选择本地文件。"
                    )
                invalid.append(target)
            missing.append(item)
            continue
        if model_file_sha256_matches(target, item["sha256"]):
            continue
        resolved = resolve_model_file(target)
        if resolved.resolve() != target.resolve():
            raise ValueError(
                f"已映射模型的 SHA256 不匹配：{item['path']}。"
                "请在模型管理中重新选择本地文件。"
            )
        invalid.append(target)
        missing.append(item)
    return missing, invalid


def load_quick_repair_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"dismissed": False, "pending_profile": ""}
    if not isinstance(data, dict):
        return {"dismissed": False, "pending_profile": ""}
    pending = str(data.get("pending_profile") or "")
    return {
        "dismissed": data.get("dismissed") is True,
        "pending_profile": pending if pending in QUICK_REPAIR_PROFILES else "",
    }


def save_quick_repair_state(path: Path, *, dismissed: bool, pending_profile: str = "") -> None:
    if pending_profile and pending_profile not in QUICK_REPAIR_PROFILES:
        raise ValueError("未知的显存档位")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"dismissed": bool(dismissed), "pending_profile": pending_profile},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
