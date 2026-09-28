"""Resolve exact ComfyUI model filenames to verifiable Hugging Face files.

The ComfyUI Manager index supplies candidates, never an authority for bytes.
Only Hugging Face LFS metadata supplies the size and SHA256 used by the model
downloader. Ambiguous filenames remain a user choice.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


_INDEX_URLS = (
    "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/model-list.json",
    "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/node_db/new/model-list.json",
)
_MAX_JSON_BYTES = 6 * 1024 * 1024
_CACHE_SECONDS = 3600
_index_lock = threading.Lock()
_store_lock = threading.RLock()
_index_cache: tuple[float, list[dict]] = (0.0, [])


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if destination.scheme != "https" or destination.hostname != self.host or destination.username or destination.password:
            raise ValueError("模型目录发生了不受信任的跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get_json(url: str, *, host: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "LingJing-model-source-discovery/1.0"})
    opener = urllib.request.build_opener(_SameHostRedirect(host))
    with opener.open(request, timeout=15) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != host:
            raise ValueError("模型目录发生了不受信任的跳转")
        data = response.read(_MAX_JSON_BYTES + 1)
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("模型目录过大")
    return json.loads(data)


def _model_index() -> list[dict]:
    global _index_cache
    with _index_lock:
        updated_at, cached = _index_cache
        if cached and time.monotonic() - updated_at < _CACHE_SECONDS:
            return list(cached)
        records: list[dict] = []
        for url in _INDEX_URLS:
            try:
                payload = _get_json(url, host="raw.githubusercontent.com")
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                records.extend(item for item in payload["models"][:10000] if isinstance(item, dict))
        if records:
            _index_cache = (time.monotonic(), records)
        return records


def _parse_hf_file_url(raw_url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(str(raw_url or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"huggingface.co", "hf-mirror.com"} or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("只支持 Hugging Face 文件地址")
    parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
    if len(parts) < 5 or parts[2] not in {"resolve", "blob"}:
        raise ValueError("请提供模型文件的 Hugging Face resolve/blob 地址")
    owner, repo, _, revision, *file_parts = parts
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", value or "") for value in (owner, repo)):
        raise ValueError("模型仓库名称无效")
    if not (revision == "main" or re.fullmatch(r"[0-9a-f]{40}", revision)):
        raise ValueError("模型地址须指向 main 或明确的提交版本")
    if not file_parts or any(part in {"", ".", ".."} or "\\" in part for part in file_parts):
        raise ValueError("模型文件路径无效")
    filename = "/".join(file_parts)
    if Path(filename).suffix.lower() not in {".safetensors", ".gguf"}:
        raise ValueError("自动下载仅支持 safetensors 或 GGUF 模型文件")
    return f"{owner}/{repo}", revision, filename


def verify_hf_model_url(raw_url: str, expected_filename: str) -> dict:
    """Return an immutable URL and LFS digest for an exact filename match."""
    repo, revision, path = _parse_hf_file_url(raw_url)
    if Path(path).name.casefold() != Path(expected_filename).name.casefold():
        raise ValueError("下载链接的文件名与工作流所需模型不一致")
    if revision == "main":
        info = _get_json(f"https://huggingface.co/api/models/{repo}", host="huggingface.co")
        revision = str(info.get("sha") or "") if isinstance(info, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("无法确认模型仓库版本")
    parent = path.rpartition("/")[0]
    tree_url = f"https://huggingface.co/api/models/{repo}/tree/{revision}"
    if parent:
        tree_url += "/" + urllib.parse.quote(parent, safe="/")
    entries = _get_json(tree_url + "?expand=true", host="huggingface.co")
    if not isinstance(entries, list):
        raise ValueError("模型仓库没有返回文件清单")
    matches = [item for item in entries if isinstance(item, dict) and item.get("path") == path]
    if len(matches) != 1:
        raise ValueError("模型仓库中找不到指定文件")
    item = matches[0]
    lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
    digest = str(lfs.get("oid") or "").lower()
    size = item.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(size, int) or size <= 0:
        raise ValueError("模型文件缺少可核验的 SHA256 或大小")
    return {
        "url": f"https://huggingface.co/{repo}/resolve/{revision}/{urllib.parse.quote(path, safe='/')}",
        "repo": repo,
        "path": path,
        "filename": Path(path).name,
        "size_bytes": size,
        "sha256": digest,
    }


def discover_model_sources(filename: str) -> list[dict]:
    """Find exact-name candidates; never silently select one of multiple hashes."""
    filename = Path(str(filename or "")).name
    if not filename or Path(filename).suffix.lower() not in {".safetensors", ".gguf"}:
        return []
    urls = {
        str(item.get("url") or "").strip()
        for item in _model_index()
        if Path(str(item.get("filename") or "")).name.casefold() == filename.casefold()
    }
    verified: dict[str, dict] = {}
    if len(urls) > 24:
        raise ValueError("同名候选过多，请填写工作流作者提供的准确地址")
    unresolved = False
    for url in sorted(urls):
        try:
            candidate = verify_hf_model_url(url, filename)
        except (OSError, ValueError, json.JSONDecodeError):
            unresolved = True
            continue
        verified.setdefault(candidate["sha256"], candidate)
    if unresolved and len(verified) == 1:
        raise ValueError("部分同名候选未能核验，无法确认唯一版本；请填写准确地址或稍后重试")
    return sorted(verified.values(), key=lambda item: (not item["repo"].startswith("Comfy-Org/"), item["repo"], item["url"]))


def _valid_saved_source(source: object, filename: str) -> bool:
    if not isinstance(source, dict):
        return False
    try:
        _repo, revision, path = _parse_hf_file_url(str(source.get("url") or ""))
        size = int(source.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        revision != "main"
        and Path(path).name.casefold() == Path(filename).name.casefold()
        and size > 0
        and re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or ""))
    )


def load_saved_source(store_path: Path, relative_path: str) -> dict | None:
    try:
        with _store_lock:
            data = json.loads(Path(store_path).read_text(encoding="utf-8"))
        source = data.get(relative_path) if isinstance(data, dict) else None
        return dict(source) if _valid_saved_source(source, Path(relative_path).name) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_verified_source(store_path: Path, relative_path: str, source: dict) -> None:
    if not _valid_saved_source(source, Path(relative_path).name):
        raise ValueError("模型下载来源未通过校验")
    with _store_lock:
        _save_verified_source(Path(store_path), relative_path, source)


def _save_verified_source(store_path: Path, relative_path: str, source: dict) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[relative_path] = {
        "url": source["url"],
        "sha256": source["sha256"],
        "size_bytes": int(source["size_bytes"]),
        "repo": source.get("repo", ""),
    }
    temporary = store_path.with_name(store_path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, store_path)
    finally:
        temporary.unlink(missing_ok=True)
