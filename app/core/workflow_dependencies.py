"""Detect and evaluate portable ComfyUI workflow dependencies."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from app.core.model_maintenance import MODEL_EXTENSIONS, MODEL_REQUIREMENTS


_MODEL_SUFFIXES = {suffix.lower() for suffix in MODEL_EXTENSIONS} | {
    ".onnx",
    ".engine",
}
_MODEL_INPUT_HINTS = (
    "model",
    "ckpt",
    "checkpoint",
    "unet",
    "vae",
    "clip",
    "lora",
    "controlnet",
    "control_net",
    "upscale",
    "embedding",
    "ipadapter",
    "gguf",
)
_INDEX_CACHE_SECONDS = 10.0
_MAX_DEPENDENCIES = 4096
_MAX_DEPENDENCY_TEXT = 512
_MODEL_METADATA_KEYS = {"sha256", "size_bytes", "category", "directory", "url", "download_url"}
_index_lock = threading.Lock()
_index_cache: dict[str, tuple[float, dict[str, list[tuple[str, int]]], dict[str, int]]] = {}


def _limited_text(value) -> str:
    text = str(value or "").strip()
    return text if len(text) <= _MAX_DEPENDENCY_TEXT else ""


def _normalized_model_source(value) -> str:
    source = _limited_text(value).replace("\\", "/")
    if not source or source.startswith("/") or re.match(r"^[a-zA-Z]:", source):
        return ""
    parts = [part for part in source.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." or ":" in part for part in parts):
        return ""
    if parts[0].lower() == "models" and len(parts) > 1:
        parts = parts[1:]
    return "/".join(parts)


def _model_reference(input_name: str, value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    suffix = Path(value.replace("\\", "/")).suffix.lower()
    if suffix not in _MODEL_SUFFIXES:
        return False
    lowered = str(input_name or "").lower()
    return any(hint in lowered for hint in _MODEL_INPUT_HINTS) or bool(suffix)


def _model_entry(value: str, input_name: str = "", node: str = "") -> dict:
    source = _normalized_model_source(value)
    return {
        "name": Path(source).name,
        "source": source,
        "input": _limited_text(input_name),
        "node": _limited_text(node),
    }


def _dependency_items(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)[:_MAX_DEPENDENCIES]
    return [value]


def extract_workflow_dependencies(workflow: dict) -> dict:
    """Extract model filenames and node classes from an API-format workflow."""
    models: list[dict] = []
    nodes: list[str] = []
    seen_models: set[tuple[str, str]] = set()
    seen_nodes: set[str] = set()

    if not isinstance(workflow, dict):
        return {"models": [], "nodes": []}

    for raw_node in list(workflow.values())[:_MAX_DEPENDENCIES]:
        if not isinstance(raw_node, dict):
            continue
        class_type = _limited_text(raw_node.get("class_type"))
        if class_type and class_type not in seen_nodes:
            seen_nodes.add(class_type)
            nodes.append(class_type)
        inputs = raw_node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if not _model_reference(str(input_name), value):
                continue
            entry = _model_entry(value, str(input_name), class_type)
            key = (entry["name"].lower(), entry["source"].lower())
            if not entry["name"] or key in seen_models:
                continue
            seen_models.add(key)
            models.append(entry)
            if len(models) >= _MAX_DEPENDENCIES:
                break
        if len(models) >= _MAX_DEPENDENCIES:
            break

    return {"models": models, "nodes": nodes}


def normalize_workflow_dependencies(dependencies, workflow: dict | None = None) -> dict:
    """Normalize manifest declarations and merge auto-detected dependencies."""
    declared = dependencies if isinstance(dependencies, dict) else {}
    detected = extract_workflow_dependencies(workflow or {})

    models: list[dict] = []
    seen_models: set[tuple[str, str]] = set()
    for value in [*_dependency_items(declared.get("models")), *detected["models"]]:
        if isinstance(value, str):
            entry = _model_entry(value)
        elif isinstance(value, dict):
            source = str(value.get("source") or value.get("path") or value.get("name") or "")
            entry = _model_entry(source, value.get("input", ""), value.get("node", ""))
            declared_name = _limited_text(value.get("name") or entry["name"])
            if len(declared_name) <= _MAX_DEPENDENCY_TEXT:
                entry["name"] = Path(declared_name.replace("\\", "/")).name
            for key in _MODEL_METADATA_KEYS:
                item = value.get(key)
                if isinstance(item, (str, int, float, bool)) and len(str(item)) <= _MAX_DEPENDENCY_TEXT:
                    entry[key] = item
        else:
            continue
        key = (entry["name"].lower(), entry["source"].lower())
        if not entry["name"] or key in seen_models:
            continue
        seen_models.add(key)
        models.append(entry)
        if len(models) >= _MAX_DEPENDENCIES:
            break

    nodes: list[str] = []
    seen_nodes: set[str] = set()
    for value in [*_dependency_items(declared.get("nodes")), *detected["nodes"]]:
        name = str(value.get("name") if isinstance(value, dict) else value or "").strip()
        if len(name) > _MAX_DEPENDENCY_TEXT:
            continue
        if name and name not in seen_nodes:
            seen_nodes.add(name)
            nodes.append(name)
            if len(nodes) >= _MAX_DEPENDENCIES:
                break
    return {"models": models, "nodes": nodes}


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def clear_model_index_cache():
    with _index_lock:
        _index_cache.clear()


def model_file_index(
    models_dir: Path,
    *,
    cache_seconds: float = _INDEX_CACHE_SECONDS,
) -> tuple[dict[str, list[tuple[str, int]]], dict[str, int]]:
    """Return model basename/path metadata, cached for health polling."""
    models_dir = Path(models_dir)
    key = str(models_dir.resolve(strict=False)).lower()
    now = time.monotonic()
    with _index_lock:
        cached = _index_cache.get(key)
        if cached and now - cached[0] < max(0.0, cache_seconds):
            return (
                {name: list(items) for name, items in cached[1].items()},
                dict(cached[2]),
            )

    names: dict[str, list[tuple[str, int]]] = {}
    relative_paths: dict[str, int] = {}
    if models_dir.is_dir():
        for root, dirnames, filenames in os.walk(models_dir, followlinks=False):
            root_path = Path(root)
            dirnames[:] = [
                name
                for name in dirnames
                if not (root_path / name).is_symlink()
                and not _is_reparse_point(root_path / name)
            ]
            for filename in filenames:
                path = root_path / filename
                if path.suffix.lower() not in _MODEL_SUFFIXES or path.is_symlink() or _is_reparse_point(path):
                    continue
                try:
                    size = path.stat().st_size
                    if size <= 0:
                        continue
                    relative = path.relative_to(models_dir).as_posix().lower()
                except (OSError, ValueError):
                    continue
                relative_paths[relative] = size
                names.setdefault(filename.lower(), []).append((relative, size))

    from app.core.model_mappings import read_mappings
    for relative, source in read_mappings(models_dir).items():
        try:
            if not Path(source).is_file():
                continue
            size = Path(source).stat().st_size
            if size <= 0 or relative.lower() in relative_paths:
                continue
            relative_paths[relative.lower()] = size
            names.setdefault(Path(relative).name.lower(), []).append((relative.lower(), size))
        except OSError:
            continue
    with _index_lock:
        _index_cache[key] = (
            now,
            {name: list(items) for name, items in names.items()},
            dict(relative_paths),
        )
    return names, relative_paths


def workflow_dependency_report(
    dependencies,
    models_dir: Path,
    *,
    installed_nodes: set[str] | None = None,
    cache_seconds: float = _INDEX_CACHE_SECONDS,
    model_requirements: dict | None = None,
) -> dict:
    """Return a stable UI/API dependency report for one workflow."""
    normalized = normalize_workflow_dependencies(dependencies)
    names, relative_paths = model_file_index(models_dir, cache_seconds=cache_seconds)
    required_models: list[str] = []
    missing_models: list[str] = []
    unverified_models: list[str] = []
    known_specs: dict[str, list[tuple[str, int]]] = {}
    requirements = MODEL_REQUIREMENTS if model_requirements is None else model_requirements
    if not isinstance(requirements, dict):
        requirements = {}
    for group in requirements.values():
        if not isinstance(group, dict):
            continue
        for item in group.get("items") or []:
            if not isinstance(item, dict):
                continue
            known_source = _normalized_model_source(item.get("path"))
            try:
                known_size = int(item.get("size_bytes") or 0)
            except (TypeError, ValueError, OverflowError):
                known_size = 0
            if known_source and known_size > 0:
                known_specs.setdefault(Path(known_source).name.lower(), []).append(
                    (known_source.lower(), known_size)
                )

    for entry in normalized["models"]:
        name = str(entry.get("name") or "").strip()
        source = _normalized_model_source(entry.get("source") or name)
        if not name:
            continue
        required_models.append(name)
        source_name = Path(source).name or name
        expected_size = entry.get("size_bytes")
        try:
            expected_size = int(expected_size) if expected_size not in (None, "") else None
        except (TypeError, ValueError, OverflowError):
            expected_size = None
        if expected_size is not None and expected_size <= 0:
            expected_size = None

        match_source = source
        matching_known = known_specs.get(source_name.lower(), [])
        if "/" in source:
            exact_known = next(
                (spec for spec in matching_known if spec[0] == source.lower()),
                None,
            )
            if expected_size is None and exact_known:
                expected_size = exact_known[1]
        elif len(matching_known) == 1:
            match_source, known_size = matching_known[0]
            if expected_size is None:
                expected_size = known_size

        if "/" in match_source:
            actual_size = relative_paths.get(match_source.lower())
            present = actual_size is not None and (
                expected_size is None or actual_size == expected_size
            )
        else:
            candidates = names.get(Path(match_source).name.lower(), [])
            present = any(
                expected_size is None or actual_size == expected_size
                for _relative, actual_size in candidates
            )
        if not present:
            missing_models.append(name)
        elif entry.get("sha256"):
            # Hashing multi-gigabyte models during every health poll would freeze
            # the gateway. A declared digest therefore remains explicitly
            # unverified until the dedicated model installer verifies it.
            unverified_models.append(name)

    required_models = list(dict.fromkeys(required_models))
    missing_models = list(dict.fromkeys(missing_models))
    unverified_models = list(dict.fromkeys(unverified_models))
    required_nodes = list(normalized["nodes"])
    missing_nodes = (
        [name for name in required_nodes if name not in installed_nodes]
        if installed_nodes is not None
        else []
    )
    if missing_models or missing_nodes:
        status = "missing"
    elif unverified_models or (required_nodes and installed_nodes is None):
        status = "unverified"
    elif required_models or required_nodes:
        status = "ready"
    else:
        status = "unverified"
    return {
        "dependencies": normalized,
        "required_models": required_models,
        "missing_models": missing_models,
        "unverified_models": unverified_models,
        "required_nodes": required_nodes,
        "missing_nodes": missing_nodes,
        "nodes_verified": installed_nodes is not None,
        "dependency_status": status,
        "available": status == "ready",
    }
