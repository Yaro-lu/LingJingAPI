"""
工作流注册表 — 动态扫描根目录 workflows/ 文件夹，自动发现工作流

目录结构约定：
  workflows/                  ← 项目根目录下
    flux_t2i_v1/
      manifest.json           ← 必选（id, name, type, ...）
      workflow.json           ← 可选（ComfyUI API workflow）
      *_api.json              ← 原始 API workflow（保留备用）
      *.json                  ← 原始 ComfyUI workflow 导出
    wan_flf2v_v1/
      ...

URL 路由：
  /v1/workflows/run/{workflow_id} → 调用指定工作流
  /v1/workflows/run             → 调用默认工作流
"""
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from app.core.workflow_dependencies import normalize_workflow_dependencies
from app.core.workflow_import import ensure_safe_workflows_root
from app.core.workflow_adaptation import MAPPING_KEYS, make_mapping


# manifest["type"] → output_type
_TYPE_MAP = {
    "video.first_last_to_video": "video",
    "video.image_to_video": "video",
    "video.custom": "video",
    "image.text_to_image": "image",
    "image.text_image_to_image": "image",
    "image.image_to_image": "image",
    "text.chat": "text",
}


def _safe_workflow_variants(value) -> dict[str, str]:
    """Keep only same-folder JSON variants declared by a workflow manifest."""
    if not isinstance(value, dict):
        return {}
    variants: dict[str, str] = {}
    for raw_key, raw_name in value.items():
        key = str(raw_key or "").strip()
        name = str(raw_name or "").strip().replace("\\", "/")
        path = Path(name)
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key)
            or not name
            or path.name != name
            or path.suffix.lower() != ".json"
        ):
            continue
        variants[key] = name
    return variants


def _as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class WorkflowDef:
    def __init__(
        self,
        id: str = "",
        name: str = "",
        enabled: bool = True,
        description: str = "",
        workflow_json: str = "",
        output_type: str = "",
        folder_name: str = "",
        input_schema: Optional[dict] = None,
        inputs: Optional[list] = None,
        dependencies: Optional[dict] = None,
        workflow_type: str = "",
        capability: str = "",
        model_group: str = "",
        workflow_variants: Optional[dict] = None,
        api_mapping: Optional[dict] = None,
    ):
        self.id = str(id or "").strip()
        self.name = str(name or self.id).strip()
        self.enabled = _as_bool(enabled)
        self.description = str(description or "")
        self.workflow_json = str(workflow_json or "")  # ComfyUI workflow JSON 相对路径
        self.output_type = str(output_type or "")       # image / video / text / audio / 3d
        self.folder_name = str(folder_name or "")       # 所属子文件夹名
        self.input_schema = input_schema if isinstance(input_schema, dict) else {}
        self.inputs = inputs if isinstance(inputs, list) else []
        self.dependencies = normalize_workflow_dependencies(dependencies)
        self.workflow_type = str(workflow_type or "").strip()
        self.capability = str(capability or "").strip()
        self.model_group = str(model_group or "").strip()
        self.workflow_variants = _safe_workflow_variants(workflow_variants)
        self.api_mapping = {k: v for k, v in (api_mapping or {}).items() if k in MAPPING_KEYS}
        self._workflows_dir: Optional[Path] = None  # 由 Registry 注入

    @property
    def folder(self) -> Optional[Path]:
        if self._workflows_dir and self.folder_name:
            relative = Path(self.folder_name)
            if relative.name != self.folder_name or self.folder_name in {".", ".."}:
                return None
            root = self._workflows_dir.resolve(strict=False)
            candidate = (self._workflows_dir / relative).resolve(strict=False)
            if candidate.is_relative_to(root):
                return candidate
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "description": self.description,
            "workflow_json": self.workflow_json,
            "output_type": self.output_type,
            "folder_name": self.folder_name,
            "input_schema": self.input_schema,
            "inputs": self.inputs,
            "dependencies": self.dependencies,
            "type": self.workflow_type,
            "capability": self.capability,
            "model_group": self.model_group,
            "workflow_variants": self.workflow_variants,
            **self.api_mapping,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowDef":
        if not isinstance(d, dict):
            raise ValueError("工作流配置项必须是对象")
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            enabled=d.get("enabled", True),
            description=d.get("description", ""),
            workflow_json=d.get("workflow_json", ""),
            output_type=d.get("output_type", ""),
            folder_name=d.get("folder_name", ""),
            input_schema=d.get("input_schema") or {},
            inputs=d.get("inputs") or [],
            dependencies=d.get("dependencies") or {},
            workflow_type=d.get("type") or d.get("workflow_type") or "",
            capability=d.get("capability") or "",
            model_group=d.get("model_group") or d.get("modelGroup") or "",
            workflow_variants=d.get("workflow_variants") or d.get("workflowVariants") or {},
            api_mapping={k: d[k] for k in MAPPING_KEYS if k in d},
        )

    @classmethod
    def from_manifest(cls, folder: Path, manifest: dict) -> "WorkflowDef":
        """从 manifest.json 创建 WorkflowDef"""
        raw_id = manifest.get("id", folder.name)
        wf_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) else folder.name
        wf_type = str(manifest.get("type") or "")
        output_type = _TYPE_MAP.get(wf_type, wf_type.split(".")[-1] if "." in wf_type else "")

        # 自动检测 workflow.json（存相对路径）
        wf_json_path = ""
        wf_json_file = folder / "workflow.json"
        workflow_file_ready = (
            wf_json_file.is_file()
            and not wf_json_file.is_symlink()
            and not _is_reparse_point(wf_json_file)
        )
        if workflow_file_ready:
            # 相对于 workflows 目录的路径
            wf_json_path = f"{folder.name}/workflow.json"

        workflow_data = {}
        if workflow_file_ready:
            try:
                if wf_json_file.stat().st_size <= 64 * 1024 * 1024:
                    with open(wf_json_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    workflow_data = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError):
                workflow_data = {}

        input_schema = manifest.get("input_schema") or manifest.get("inputSchema") or {}
        if not isinstance(input_schema, dict):
            input_schema = {}
        inputs = manifest.get("inputs") or input_schema.get("inputs") or []
        if not isinstance(inputs, list):
            inputs = []

        return cls(
            id=wf_id,
            name=str(manifest.get("name") or wf_id),
            enabled=_as_bool(manifest.get("enabled", True)),
            description=str(manifest.get("description") or manifest.get("name") or ""),
            workflow_json=wf_json_path,
            output_type=output_type,
            folder_name=folder.name,
            input_schema=input_schema,
            inputs=inputs,
            dependencies=normalize_workflow_dependencies(
                manifest.get("dependencies") or {},
                workflow_data,
            ),
            workflow_type=wf_type,
            capability=manifest.get("capability") or "",
            model_group=manifest.get("model_group") or manifest.get("modelGroup") or "",
            workflow_variants=manifest.get("workflow_variants") or manifest.get("workflowVariants") or {},
            api_mapping={k: manifest[k] for k in MAPPING_KEYS if k in manifest},
        )


def read_local_workflow_catalog(
    workflows_dir: Path,
    config_path: Optional[Path] = None,
) -> list[dict]:
    """Read manifests for UI fallback without changing workflow_config.json."""
    workflows_dir = Path(workflows_dir)
    if not workflows_dir.is_dir():
        return []
    ensure_safe_workflows_root(workflows_dir)

    configured: dict[str, dict] = {}
    default_workflow_id = ""
    config_path = Path(config_path) if config_path else None
    try:
        if config_path and config_path.is_file() and config_path.stat().st_size <= 2 * 1024 * 1024:
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw_config, dict):
                default_workflow_id = str(raw_config.get("default_workflow_id") or "").strip()
                configured = {
                    str(item.get("id") or "").strip(): item
                    for item in raw_config.get("workflows", [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        configured = {}

    records: list[dict] = []
    seen_ids: set[str] = set()
    for folder in sorted(workflows_dir.iterdir()):
        if (
            folder.name.lower().startswith(".importing_")
            or not folder.is_dir()
            or folder.is_symlink()
            or _is_reparse_point(folder)
        ):
            continue
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink() or _is_reparse_point(manifest_path):
            continue
        try:
            if manifest_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("manifest.json 过大")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest.json 顶层必须是对象")
            workflow = WorkflowDef.from_manifest(folder, manifest)
            workflow.id = str(workflow.id or folder.name).strip()
            normalized_id = workflow.id.casefold()
            if (
                not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", workflow.id)
                or normalized_id in seen_ids
            ):
                continue
            seen_ids.add(normalized_id)
            saved = configured.get(workflow.id) or {}
            workflow.enabled = _as_bool(saved.get("enabled", workflow.enabled))
            workflow.description = str(saved.get("description") or workflow.description)
            workflow._workflows_dir = workflows_dir
            record = workflow.to_dict()
            record["is_default"] = workflow.id == default_workflow_id
            records.append(record)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            records.append(
                {
                    "id": folder.name,
                    "name": folder.name,
                    "enabled": False,
                    "workflow_json": "",
                    "manifest_error": True,
                    "is_default": False,
                }
            )
    return records


def merge_workflow_catalog(
    local_records: list[dict],
    remote_records: list[dict],
) -> list[dict]:
    """Keep every local workflow visible while overlaying live backend state.

    Local manifests are the catalogue of what the installed client ships.  A
    backend can legitimately return a partial list while it is starting or
    when its workflow config is stale, so treating the remote list as the
    entire catalogue would make missing-model workflows disappear.
    """
    local = [dict(item) for item in local_records if isinstance(item, dict)]
    remote = [dict(item) for item in remote_records if isinstance(item, dict)]
    remote_by_id = {
        str(item.get("id") or "").strip(): item
        for item in remote
        if str(item.get("id") or "").strip()
    }
    merged: list[dict] = []
    seen: set[str] = set()
    manifest_fields = (
        "name",
        "description",
        "type",
        "workflow_type",
        "output_type",
        "capability",
        "model_group",
        "workflow_json",
        "workflow_variants",
        "input_schema",
    )
    for local_item in local:
        workflow_id = str(local_item.get("id") or "").strip()
        if not workflow_id:
            continue
        live_item = remote_by_id.get(workflow_id)
        combined = {**local_item, **live_item} if live_item else local_item
        if live_item:
            for field in manifest_fields:
                if combined.get(field) in (None, "", {}):
                    combined[field] = local_item.get(field)
        merged.append(combined)
        seen.add(workflow_id)
    for item in remote:
        workflow_id = str(item.get("id") or "").strip()
        if workflow_id and workflow_id not in seen:
            merged.append(item)
            seen.add(workflow_id)
    return merged


class WorkflowRegistry:
    _thread_lock = threading.RLock()

    def __init__(self, config_path: Optional[Path] = None, workflows_dir: Optional[Path] = None):
        self.config_path = config_path
        self.workflows_dir = workflows_dir
        self.workflows: List[WorkflowDef] = []
        self.default_workflow_id: Optional[str] = None
        self._lock = threading.RLock()

        # 有配置文件 → 加载；否则尝试扫描文件夹
        if config_path and config_path.exists():
            self.load()
        elif workflows_dir and workflows_dir.exists():
            self.scan_folder(workflows_dir)

    # ══════════════════════════════════════════════════════
    # 动态扫描
    # ══════════════════════════════════════════════════════
    def scan_folder(self, folder: Optional[Path] = None) -> List[WorkflowDef]:
        with self._thread_lock, self._lock, self._locked_config():
            if self.config_path and self.config_path.exists():
                self._load_unlocked()
            return self._scan_folder_unlocked(folder)

    def _scan_folder_unlocked(
        self,
        folder: Optional[Path] = None,
        *,
        save: bool = True,
    ) -> List[WorkflowDef]:
        """
        扫描文件夹，自动发现所有含 manifest.json 的子目录。
        返回新发现的工作流列表。
        """
        folder = Path(folder or self.workflows_dir) if (folder or self.workflows_dir) else None
        if not folder or not folder.exists():
            return []
        ensure_safe_workflows_root(folder)

        # 保留已有工作流的 enabled 状态和默认设置
        existing_map: dict[str, WorkflowDef] = {w.id: w for w in self.workflows}

        discovered: List[WorkflowDef] = []
        new_workflows: List[WorkflowDef] = []

        seen_ids: set[str] = set()
        for subdir in sorted(folder.iterdir()):
            if (
                subdir.name.lower().startswith(".importing_")
                or not subdir.is_dir()
                or subdir.is_symlink()
                or _is_reparse_point(subdir)
            ):
                continue
            manifest_file = subdir / "manifest.json"
            if (
                not manifest_file.is_file()
                or manifest_file.is_symlink()
                or _is_reparse_point(manifest_file)
            ):
                continue

            try:
                if manifest_file.stat().st_size > 2 * 1024 * 1024:
                    continue
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            if not isinstance(manifest, dict):
                continue
            if str(manifest.get("engine") or "comfyui").strip().lower() != "comfyui":
                continue
            wf = WorkflowDef.from_manifest(subdir, manifest)
            wf.id = str(wf.id or subdir.name).strip()
            wf.name = str(wf.name or wf.id).strip()
            normalized_id = wf.id.casefold()
            if (
                not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", wf.id)
                or len(wf.name) > 200
                or normalized_id in seen_ids
            ):
                continue
            seen_ids.add(normalized_id)
            wf._workflows_dir = folder  # 注入根目录，供 folder 属性使用

            # 恢复用户设置
            existing = existing_map.get(wf.id)
            if existing:
                wf.enabled = existing.enabled
                wf.description = existing.description or wf.description

            discovered.append(wf)
            if wf.id not in existing_map:
                new_workflows.append(wf)

        self.workflows = discovered

        # 校验默认工作流
        self._repair_default_unlocked()

        if save:
            self._save_unlocked()
        return new_workflows

    # ══════════════════════════════════════════════════════
    # 持久化
    # ══════════════════════════════════════════════════════
    @contextmanager
    def _locked_config(self, timeout: float = 8.0):
        """Serialize registry read/modify/write operations across GUI/API processes."""
        if not self.config_path:
            yield
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.config_path.parent / ".workflow_config.lock"
        handle = open(lock_path, "a+b")
        locked = False
        deadline = time.monotonic() + max(0.2, float(timeout))
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            if os.name == "nt":
                import msvcrt

                while time.monotonic() < deadline:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.03)
            else:
                import fcntl

                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        time.sleep(0.03)
            if not locked:
                raise TimeoutError("workflow_config.json 正在被其他进程更新")
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _load_unlocked(self):
        if not self.config_path or not self.config_path.exists():
            self.workflows = []
            self.default_workflow_id = None
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("workflow_config.json 顶层必须是对象")
        raw_workflows = data.get("workflows", [])
        if not isinstance(raw_workflows, list):
            raise ValueError("workflow_config.json 的 workflows 必须是列表")
        self.workflows = [
            WorkflowDef.from_dict(w) for w in raw_workflows if isinstance(w, dict)
        ]
        raw_default = data.get("default_workflow_id")
        self.default_workflow_id = str(raw_default).strip() if raw_default else None
        for workflow in self.workflows:
            workflow._workflows_dir = self.workflows_dir

    def load(self):
        with self._thread_lock, self._lock:
            self._load_unlocked()

    def _save_unlocked(self):
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "workflows": [w.to_dict() for w in self.workflows],
            "default_workflow_id": self.default_workflow_id,
        }
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            dir=self.config_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.config_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def save(self):
        if not self.config_path:
            return
        with self._thread_lock, self._lock, self._locked_config():
            self._save_unlocked()

    def _refresh_before_mutation_unlocked(self) -> None:
        if self.config_path and self.config_path.exists():
            self._load_unlocked()

    @contextmanager
    def locked_mutation(self):
        """Hold the cross-process registry lock for one filesystem/config commit."""
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            yield self

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════
    def get(self, wf_id: str) -> Optional[WorkflowDef]:
        with self._lock:
            for wf in self.workflows:
                if wf.id == wf_id:
                    return wf
        return None

    @property
    def enabled_workflows(self) -> List[WorkflowDef]:
        with self._lock:
            return [w for w in self.workflows if w.enabled]

    def resolve(self, workflow_id: Optional[str] = None) -> Optional[WorkflowDef]:
        """
        核心路由：
        - 指定 workflow_id → 查对应工作流（必须 enabled）
        - 未指定 → 返回默认工作流（或第一个 enabled）
        """
        with self._lock:
            if workflow_id:
                wf = self.get(workflow_id)
                return wf if (wf and wf.enabled) else None

            if self.default_workflow_id:
                fallback = self.get(self.default_workflow_id)
                if fallback and fallback.enabled:
                    return fallback

            enabled = self.enabled_workflows
            return enabled[0] if enabled else None

    def _repair_default_unlocked(self) -> None:
        default = next(
            (w for w in self.workflows if w.id == self.default_workflow_id),
            None,
        )
        if not default or not default.enabled:
            enabled = [w for w in self.workflows if w.enabled]
            self.default_workflow_id = enabled[0].id if enabled else None

    # ══════════════════════════════════════════════════════
    # 增删改
    # ══════════════════════════════════════════════════════
    def add(self, wf: WorkflowDef):
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            wf._workflows_dir = self.workflows_dir
            self.workflows.append(wf)
            self._repair_default_unlocked()
            self._save_unlocked()

    def remove(self, wf_id: str):
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            self.workflows = [w for w in self.workflows if w.id != wf_id]
            self._repair_default_unlocked()
            self._save_unlocked()

    def update(self, wf_id: str, **kwargs):
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            for wf in self.workflows:
                if wf.id == wf_id:
                    for k, v in kwargs.items():
                        if hasattr(wf, k):
                            setattr(wf, k, v)
                    if "enabled" in kwargs:
                        wf.enabled = _as_bool(wf.enabled)
                    self._repair_default_unlocked()
                    self._save_unlocked()
                    return True
        return False

    def move_up(self, wf_id: str) -> bool:
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            for i, wf in enumerate(self.workflows):
                if wf.id == wf_id and i > 0:
                    self.workflows[i], self.workflows[i - 1] = (
                        self.workflows[i - 1], self.workflows[i],
                    )
                    self._save_unlocked()
                    return True
        return False

    def save_mapping(self, wf_id: str, fields: list, output_type: str, expected_hash: str) -> dict:
        """Commit a manual mapping alongside its source manifest, or roll back."""
        from app.core.workflow_adaptation import graph_hash

        with self.locked_mutation():
            wf = self.get(wf_id)
            folder = wf.folder if wf else None
            if folder is None:
                raise ValueError("工作流不存在，请刷新列表")
            ensure_safe_workflows_root(self.workflows_dir)
            graph_path, manifest_path = folder / "workflow.json", folder / "manifest.json"
            if any(p.is_symlink() or _is_reparse_point(p) for p in (folder, graph_path, manifest_path)):
                raise ValueError("工作流路径不安全")
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            if graph_hash(graph) != expected_hash:
                raise ValueError("编辑期间工作流已改变，请重新打开参数设置")
            mapping = make_mapping(graph, fields, output_type)
            previous = manifest_path.read_bytes()
            manifest = json.loads(previous)
            manifest.update(mapping)
            manifest["type"] = {"image": "image.text_to_image", "video": "video.custom", "text": "text.chat"}[output_type]
            handle, name = tempfile.mkstemp(prefix=".mapping-", suffix=".json", dir=folder)
            temporary = Path(name)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(manifest, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, manifest_path)
                wf.api_mapping = {k: mapping[k] for k in MAPPING_KEYS}
                wf.input_schema = mapping["input_schema"]
                wf.output_type = output_type
                wf.workflow_type = manifest["type"]
                wf.inputs = mapping["input_schema"]["inputs"]
                self._save_unlocked()
            except Exception:
                temporary.write_bytes(previous)
                os.replace(temporary, manifest_path)
                self._load_unlocked()
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return wf.to_dict()

    def move_down(self, wf_id: str) -> bool:
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            for i, wf in enumerate(self.workflows):
                if wf.id == wf_id and i < len(self.workflows) - 1:
                    self.workflows[i], self.workflows[i + 1] = (
                        self.workflows[i + 1], self.workflows[i],
                    )
                    self._save_unlocked()
                    return True
        return False

    def set_enabled(self, wf_id: str, enabled: bool) -> bool:
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            wf = self.get(wf_id)
            if not wf:
                return False
            wf.enabled = _as_bool(enabled)
            self._repair_default_unlocked()
            self._save_unlocked()
            return True

    def set_default(self, wf_id: str) -> bool:
        with self._thread_lock, self._lock, self._locked_config():
            self._refresh_before_mutation_unlocked()
            wf = self.get(wf_id)
            if not wf or not wf.enabled:
                return False
            self.default_workflow_id = wf_id
            self._save_unlocked()
            return True
