"""Human-readable local configuration stored in ``runtime/config.local.txt``."""

from __future__ import annotations

import configparser
import copy
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, Optional


CONFIG_FILENAME = "config.local.txt"
LEGACY_CONFIG_FILENAME = "config.local.json"
DIRECTORY_NAMES = ("models", "workflows", "outputs", "logs")
DEFAULT_DIRECTORY_PARTS = {
    "models": ("models",),
    "workflows": ("workflows",),
    "outputs": ("outputs",),
    "logs": ("runtime", "logs"),
}


class Config:
    """Load the client's editable settings from a plain INI-style TXT file."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = Path(base_dir or Path(__file__).parent.parent)
        self.config_path = self.base_dir / "runtime" / CONFIG_FILENAME
        self.legacy_config_path = self.base_dir / "runtime" / LEGACY_CONFIG_FILENAME
        should_migrate_legacy = (
            not self.config_path.is_file() and self.legacy_config_path.is_file()
        )
        self._config = self._load_config()
        invalid_directories = self._clear_invalid_directory_mappings()
        if should_migrate_legacy or (
            invalid_directories and self.config_path.is_file()
        ):
            try:
                self.save()
            except OSError:
                # The resolved paths still fall back safely for this process
                # even if an externally protected config cannot be rewritten.
                pass

    def _load_config(self) -> Dict[str, Any]:
        defaults = self._default_config()
        loaded: Dict[str, Any] = {}
        if self.config_path.is_file():
            loaded = self._read_text_config(self.config_path, defaults)
        elif self.legacy_config_path.is_file():
            try:
                candidate = json.loads(
                    self.legacy_config_path.read_text(encoding="utf-8-sig")
                )
                if isinstance(candidate, dict):
                    loaded = candidate
            except (OSError, json.JSONDecodeError):
                loaded = {}
        return self._merge_dicts(defaults, loaded)

    @staticmethod
    def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = Config._merge_dicts(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    @staticmethod
    def _parse_text_value(raw: str, template: Any = None, *, key: str = "") -> Any:
        value = str(raw or "").strip()
        if isinstance(template, bool):
            return value.casefold() in {"1", "true", "yes", "on", "开启", "是"}
        if isinstance(template, int) and not isinstance(template, bool):
            try:
                return int(value)
            except ValueError:
                return template
        if isinstance(template, float):
            try:
                return float(value)
            except ValueError:
                return template
        if isinstance(template, list) or key == "launch_args":
            if not value:
                return []
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            try:
                legacy_items = shlex.split(value, posix=False)
            except ValueError:
                return []
            return [
                item[1:-1]
                if len(item) >= 2 and item[0] == item[-1] and item[0] in {'"', "'"}
                else item
                for item in legacy_items
            ]
        return value

    def _read_text_config(
        self,
        path: Path,
        defaults: Dict[str, Any],
    ) -> Dict[str, Any]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error):
            return {}

        loaded: Dict[str, Any] = {}
        for section in parser.sections():
            if section.startswith("model_alias:"):
                alias = section.removeprefix("model_alias:").strip()
                if not alias:
                    continue
                target = loaded.setdefault("model_aliases", {}).setdefault(alias, {})
                template = defaults.get("model_aliases", {}).get(alias, {})
            else:
                target = loaded.setdefault(section, {})
                template = defaults.get(section, {})
            if not isinstance(target, dict):
                continue
            for key, raw in parser.items(section):
                expected = template.get(key) if isinstance(template, dict) else None
                target[key] = self._parse_text_value(raw, expected, key=key)
        return loaded

    def _default_config(self) -> Dict[str, Any]:
        return {
            "server": {"host": "127.0.0.1", "port": 18188},
            "tunnel": {
                "provider": "cloudflare_quick_tunnel",
                "protocol": "auto",
                "cloudflared_path": "bin/cloudflared.exe",
            },
            "auth": {
                "api_key_mode": "auto_generate_on_start",
                "api_key_prefix": "sk-local",
            },
            "queue": {"max_concurrent": 1, "max_pending": 10},
            "runtime": {"download_url": ""},
            "directories": {
                "models": "",
                "workflows": "",
                "outputs": "",
                "logs": "",
            },
            "storage": {
                "keep_requests": True,
                "keep_inputs": True,
                "keep_outputs": True,
                "keep_logs": True,
                "auto_cleanup": False,
                "unsafe_keep_auth_header": False,
            },
            "comfyui": {
                "base_url": "http://127.0.0.1:8188",
                "timeout_sec": 3600,
                "vram_mode": "auto",
                "launch_args": [],
            },
            "text_model": {
                "mode": "mock",
                "forward_base_url": "",
                "forward_api_key": "",
                "forward_model": "",
            },
            "model_aliases": {
                "seedance-1.5-pro": {
                    "type": "video.first_last_to_video",
                    "workflow_id": "wan_flf2v_v1",
                },
                "local-video-flf2v": {
                    "type": "video.first_last_to_video",
                    "workflow_id": "wan_flf2v_v1",
                },
                "local-text-default": {
                    "type": "text.chat",
                    "adapter": "chat_default",
                },
            },
        }

    @staticmethod
    def _format_text_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple)):
            return json.dumps([str(item) for item in value], ensure_ascii=False)
        return str(value)

    def save(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        for section, values in self._config.items():
            if section == "model_aliases" and isinstance(values, dict):
                for alias, alias_values in values.items():
                    if not isinstance(alias_values, dict):
                        continue
                    parser[f"model_alias:{alias}"] = {
                        key: self._format_text_value(value)
                        for key, value in alias_values.items()
                    }
                continue
            if isinstance(values, dict):
                parser[section] = {
                    key: self._format_text_value(value)
                    for key, value in values.items()
                }

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("# 灵境造片厂高级设置\n")
                handle.write("# 修改端口、显存模式等参数后，请退出并重新打开客户端。\n\n")
                handle.write("# directories 中可填写现有的绝对目录；留空使用安装目录下的默认位置。\n")
                handle.write("# 如果映射目录不存在，客户端会自动清除该项并恢复默认位置。\n\n")
                parser.write(handle, space_around_delimiters=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
        finally:
            temporary.unlink(missing_ok=True)

    def ensure_file(self) -> Path:
        if not self.config_path.is_file():
            self.save()
        return self.config_path

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._config)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value: Any = self._config
        for item in keys:
            if isinstance(value, dict) and item in value:
                value = value[item]
            else:
                return default
        return value

    def set(self, key: str, value: Any):
        keys = key.split(".")
        target = self._config
        for item in keys[:-1]:
            if item not in target or not isinstance(target[item], dict):
                target[item] = {}
            target = target[item]
        target[keys[-1]] = value

    def _default_directory(self, name: str) -> Path:
        if name not in DEFAULT_DIRECTORY_PARTS:
            raise KeyError(f"Unknown directory mapping: {name}")
        return self.base_dir.joinpath(*DEFAULT_DIRECTORY_PARTS[name])

    @staticmethod
    def _existing_absolute_directory(value: Any) -> Optional[Path]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                return None
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved if resolved.is_dir() else None

    def _clear_invalid_directory_mappings(self) -> list[str]:
        invalid = []
        for name in DIRECTORY_NAMES:
            raw = self.get(f"directories.{name}", "")
            if str(raw or "").strip() and self._existing_absolute_directory(raw) is None:
                self.set(f"directories.{name}", "")
                invalid.append(name)
        return invalid

    def directory(self, name: str) -> Path:
        if name not in DEFAULT_DIRECTORY_PARTS:
            raise KeyError(f"Unknown directory mapping: {name}")
        mapped = self._existing_absolute_directory(
            self.get(f"directories.{name}", "")
        )
        if mapped is not None:
            return mapped
        default = self._default_directory(name)
        default.mkdir(parents=True, exist_ok=True)
        return default.resolve()

    def set_directory_mapping(self, name: str, path: Path | str) -> Path:
        if name not in DEFAULT_DIRECTORY_PARTS:
            raise KeyError(f"Unknown directory mapping: {name}")
        resolved = self._existing_absolute_directory(path)
        if resolved is None:
            raise ValueError("目录不存在或不是可用的绝对路径")
        # Accept either the storage folder itself or a matching application root.
        # Only adapt the requested kind; never change sibling mappings implicitly.
        if resolved.name.casefold() != name.casefold():
            for parts in ((name,), ("ComfyUI", name)):
                candidate = resolved.joinpath(*parts)
                if candidate.is_dir():
                    resolved = candidate.resolve(strict=True)
                    break
        default = self._default_directory(name).resolve(strict=False)
        self.set(
            f"directories.{name}",
            "" if resolved == default else str(resolved),
        )
        self.save()
        return resolved

    def reset_directory_mapping(self, name: str) -> Path:
        if name not in DEFAULT_DIRECTORY_PARTS:
            raise KeyError(f"Unknown directory mapping: {name}")
        self.set(f"directories.{name}", "")
        self.save()
        return self.directory(name)

    @property
    def server_host(self) -> str:
        return str(self.get("server.host", "127.0.0.1"))

    @property
    def server_port(self) -> int:
        return int(self.get("server.port", 18188))

    @property
    def comfyui_url(self) -> str:
        return str(self.get("comfyui.base_url", "http://127.0.0.1:8188"))

    @property
    def comfyui_timeout(self) -> int:
        return int(self.get("comfyui.timeout_sec", 3600))

    @property
    def max_concurrent(self) -> int:
        return int(self.get("queue.max_concurrent", 1))

    @property
    def max_pending(self) -> int:
        return int(self.get("queue.max_pending", 10))

    @property
    def runtime_dir(self) -> Path:
        path = self.base_dir / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def models_dir(self) -> Path:
        return self.directory("models")

    @property
    def workflows_dir(self) -> Path:
        return self.directory("workflows")

    @property
    def requests_dir(self) -> Path:
        path = self.runtime_dir / "requests"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def inputs_dir(self) -> Path:
        path = self.runtime_dir / "inputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def outputs_dir(self) -> Path:
        return self.directory("outputs")

    @property
    def tasks_dir(self) -> Path:
        path = self.runtime_dir / "tasks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        return self.directory("logs")

    @property
    def temp_dir(self) -> Path:
        path = self.runtime_dir / "temp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cloudflared_path(self) -> Path:
        relative = self.get("tunnel.cloudflared_path", "bin/cloudflared.exe")
        return self.base_dir / str(relative)

    @property
    def tunnel_protocol(self) -> str:
        return str(self.get("tunnel.protocol", "auto"))

    @property
    def model_aliases(self) -> Dict[str, Any]:
        value = self.get("model_aliases", {})
        return value if isinstance(value, dict) else {}

    @property
    def storage_config(self) -> Dict[str, Any]:
        value = self.get("storage", {})
        return value if isinstance(value, dict) else {}

    def get_model_alias(self, model_id: str) -> Optional[Dict[str, Any]]:
        value = self.model_aliases.get(model_id)
        return value if isinstance(value, dict) else None
