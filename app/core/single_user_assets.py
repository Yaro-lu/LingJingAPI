"""Small on-disk helpers for the single-user gateway; no additional service."""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import tempfile
import threading
import uuid

_lock = threading.RLock()

def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def gateway_id(runtime_dir):
    path = Path(runtime_dir) / "gateway_identity.json"
    with _lock:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["id"]
        identity = uuid.uuid4().hex
        _atomic_json(path, {"id":identity})
        return identity

def record_path(runtime_dir, task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        return None
    return Path(runtime_dir) / "task-results" / (task_id + ".json")

def save_record(runtime_dir, record):
    path = record_path(runtime_dir, str(record.get("task_id") or record.get("id") or ""))
    if path is None: return
    # Persist only task/result metadata, never credentials or request bodies.
    fields = ("id", "task_id", "workflow_id", "workflow_name", "status", "outputs", "text", "error", "updated_at", "started_at", "elapsed")
    with _lock: _atomic_json(path, {k:record[k] for k in fields if k in record})

def load_record(runtime_dir, task_id):
    path = record_path(runtime_dir, task_id)
    try:
        if not path or path.stat().st_size > 8 * 1024 * 1024: return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data.get("task_id", data.get("id")) == task_id else {}
    except (OSError, ValueError, TypeError): return {}

def private_host(host):
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or any(ip in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    except ValueError: return False

def lan_urls(port):
    addresses = set()
    preferred = None
    try:
        addresses.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))  # route lookup only; no packets are sent
            preferred = sock.getsockname()[0]
            addresses.add(preferred)
    except OSError: pass
    return [f"http://{ip}:{port}" for ip in sorted(addresses, key=lambda ip: (ip != preferred, ip)) if private_host(ip) and not ip.startswith("127.")]

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


def _video_first_frame(source):
    from PIL import Image
    try:
        import av
    except ImportError:
        av = None
    if av is not None:
        with av.open(str(source)) as container:
            stream = next(iter(container.streams.video), None)
            if stream is None:
                raise ValueError("视频没有画面")
            frame = next(container.decode(stream), None)
            if frame is None:
                raise ValueError("无法读取视频首帧")
            return frame.to_image()
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("视频预览需要运行环境中的 av 或 FFmpeg")
    result = subprocess.run(
        [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source),
         "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
         "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True, timeout=20, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with Image.open(io.BytesIO(result.stdout)) as frame:
        return frame.copy()


def image_preview(source, cache_dir=None):
    from PIL import Image, ImageOps
    source = Path(source)
    stat = source.stat()
    key = hashlib.sha256(f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:webp640-v1".encode()).hexdigest()
    root = Path(cache_dir) if cache_dir else Path(os.environ.get("XDG_CACHE_HOME") or tempfile.gettempdir()) / "LingJingAPI-previews"
    target = root / (key + ".webp")
    with _lock:
        if target.exists(): return target
        root.mkdir(parents=True, exist_ok=True)
        with (_video_first_frame(source) if source.suffix.lower() in VIDEO_EXTENSIONS else Image.open(source)) as original:
            original.thumbnail((640,640))
            preview = ImageOps.exif_transpose(original).convert("RGB")
            temporary = target.with_suffix(".tmp")
            try:
                preview.save(temporary, "WEBP", quality=72, method=4)
                temporary.replace(target)
            finally: temporary.unlink(missing_ok=True)
    return target
