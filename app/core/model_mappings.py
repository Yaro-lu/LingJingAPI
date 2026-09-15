"""Explicit local file references, consumed by checks and ComfyUI search paths."""
import json
import os
from pathlib import Path, PurePosixPath
import uuid

MAPPING_FILE = ".lingjing-model-mappings.json"
CATEGORIES = {"checkpoints", "diffusion_models", "unet", "text_encoders", "clip", "vae", "loras", "clip_vision", "controlnet", "upscale_models", "embeddings", "model_patches"}


def safe_relative(value):
    text = str(value or "").replace("\\", "/")
    parts = PurePosixPath(text).parts
    if not parts or parts[0] not in CATEGORIES or len(parts) < 2 or any(p in {"..", "."} or ":" in p for p in parts):
        raise ValueError("无法确定安全的模型分类目录")
    return "/".join(parts)


def read_mappings(models_dir):
    try:
        path = Path(models_dir) / MAPPING_FILE
        if path.stat().st_size > 1024 * 1024:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for relative, source in raw.items():
            try:
                relative = safe_relative(relative)
                if not isinstance(source, str) or not Path(source).is_absolute():
                    continue
                # ComfyUI uses the same filename under the registered category root.
                suffix = PurePosixPath(relative).parts[1:]
                if tuple(p.lower() for p in Path(source).parts[-len(suffix):]) != tuple(p.lower() for p in suffix):
                    continue
                result[relative] = source
            except (ValueError, TypeError):
                continue
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def resolve_model_file(path):
    path = Path(path)
    if path.is_file():
        return path
    for root in list(path.parents)[:16]:
        if (root / MAPPING_FILE).is_file():
            source = read_mappings(root).get(path.relative_to(root).as_posix())
            return Path(source) if source else path
    return path


def register_mapping(models_dir, relative, source, expected_size=None):
    root = Path(models_dir).resolve()
    relative = safe_relative(relative)
    source = Path(source).resolve(strict=True)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("请选择非空模型文件")
    suffix = PurePosixPath(relative).parts[1:]
    if tuple(p.lower() for p in source.parts[-len(suffix):]) != tuple(p.lower() for p in suffix):
        raise ValueError(f"请选择工作流要求的文件 {('/'.join(suffix))}；不能用其他文件名或量化版本代替")
    if expected_size and source.stat().st_size != int(expected_size):
        raise ValueError("模型文件大小与工作流要求不符，请确认文件完整且版本一致")
    if (root / relative).is_file() and (root / relative).resolve() != source:
        raise ValueError("模型目标位置已有文件，请先处理已有文件；不会自动覆盖")
    root.mkdir(parents=True, exist_ok=True)
    mappings = read_mappings(root)
    mappings[relative] = str(source)
    target = root / MAPPING_FILE
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(mappings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return source


def extra_search_paths(models_dir):
    rows = []
    for relative, source in read_mappings(models_dir).items():
        parts = PurePosixPath(relative).parts
        directory = Path(source).parents[len(parts) - 2]
        pair = (parts[0], directory.as_posix())
        if pair not in rows:
            rows.append(pair)
    return rows


def find_related_models(selected_file, items, models_dir):
    """Check only the chosen directory, its parent and matching category paths."""
    selected = Path(selected_file).resolve()
    roots = (selected.parent, selected.parent.parent)
    matches = []
    seen = set()
    for item in items:
        relative = safe_relative(item.get("path"))
        if relative in seen:
            continue
        seen.add(relative)
        current = resolve_model_file(Path(models_dir) / relative)
        expected_size = item.get("size_bytes")
        try:
            if current.is_file() and current.stat().st_size > 0 and (not expected_size or current.stat().st_size == int(expected_size)):
                continue
        except OSError:
            pass
        suffix = PurePosixPath(relative).parts[1:]
        for root in roots:
            for candidate in (root / relative, root.joinpath(*suffix)):
                try:
                    source = candidate.resolve()
                    if source == selected or not source.is_file():
                        continue
                    size = source.stat().st_size
                    if size <= 0 or (expected_size and size != int(expected_size)):
                        continue
                    if tuple(p.lower() for p in source.parts[-len(suffix):]) != tuple(p.lower() for p in suffix):
                        continue
                    matches.append({"item": item, "source": source})
                    break
                except OSError:
                    continue
            else:
                continue
            break
    return matches
