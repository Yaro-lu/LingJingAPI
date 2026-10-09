"""Local, user-owned cloud model settings and execution adapters.

The gateway credential never grants access to these settings.  Only the desktop
process writes this file; generation requests can use enabled profiles but cannot
read or replace their credentials or service addresses.
"""

from __future__ import annotations

import configparser
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import requests

from app.core.secret_store import protect_text, unprotect_text


DEFAULTS = {
    "deepseek": ("文字 · DeepSeek", "https://api.deepseek.com", "deepseek-flash"),
    "seedream": ("图片 · 火山方舟 Seedream", "https://ark.cn-beijing.volces.com/api/v3", ""),
    "seedance": ("视频 · 火山方舟 Seedance", "https://ark.cn-beijing.volces.com/api/v3", ""),
    "dreamina": ("图片/视频 · 即梦 CLI", "", ""),
}
_UNSET = object()
_MAX_RESPONSE = 8 * 1024 * 1024


def _safe_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("第三方服务地址必须是不含账号、参数的 HTTPS URL")
    return value


def _safe_model(value: str) -> str:
    value = str(value or "").strip()
    if len(value) > 300 or any(ord(c) < 32 for c in value):
        raise ValueError("模型 ID 无效")
    return value


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    name: str
    base_url: str
    model_id: str
    enabled: bool = False
    protected_key: str = ""
    cli_path: str = ""
    cli_bound: bool = False

    @property
    def configured(self) -> bool:
        if not self.enabled:
            return False
        if self.provider_id == "dreamina":
            return bool(resolve_dreamina_cli(self.cli_path) and self.cli_bound)
        return bool(self.base_url and self.model_id and self.protected_key)

    def public_status(self) -> dict:
        return {
            "id": self.provider_id, "name": self.name,
            "model_id": self.model_id, "enabled": self.enabled,
            "key_saved": bool(self.protected_key), "configured": self.configured,
            "cli_found": bool(resolve_dreamina_cli(self.cli_path)) if self.provider_id == "dreamina" else None,
            "cli_bound": self.cli_bound if self.provider_id == "dreamina" else None,
        }


class ProviderSettings:
    def __init__(self, runtime_dir: Path):
        self.path = Path(runtime_dir) / "providers.local.txt"

    def list_profiles(self) -> dict[str, ProviderProfile]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self.path, encoding="utf-8-sig")
        except configparser.Error:
            parser = configparser.ConfigParser(interpolation=None)
        profiles = {}
        for provider_id, (name, base_url, model_id) in DEFAULTS.items():
            section = parser[f"provider:{provider_id}"] if parser.has_section(f"provider:{provider_id}") else {}
            raw_url = str(section.get("base_url", base_url)).strip()
            try:
                safe_url = _safe_base_url(raw_url) if provider_id != "dreamina" else ""
            except ValueError:
                safe_url = base_url
            profiles[provider_id] = ProviderProfile(
                provider_id, name, safe_url,
                _safe_model(section.get("model_id", model_id)),
                str(section.get("enabled", "false")).casefold() in {"true", "1", "yes"},
                str(section.get("api_key", "")),
                str(section.get("cli_path", "")) if provider_id == "dreamina" else "",
                _dreamina_binding_marker(self.path.parent).is_file() if provider_id == "dreamina" else False,
            )
        return profiles

    def get(self, provider_id: str) -> ProviderProfile:
        try:
            return self.list_profiles()[provider_id]
        except KeyError:
            raise ValueError("未知第三方模型") from None

    def update(self, provider_id: str, *, enabled=_UNSET, base_url=_UNSET,
               model_id=_UNSET, api_key=_UNSET, cli_path=_UNSET) -> ProviderProfile:
        profiles = self.list_profiles()
        if provider_id not in profiles:
            raise ValueError("未知第三方模型")
        values = {}
        if enabled is not _UNSET:
            values["enabled"] = bool(enabled)
        if base_url is not _UNSET and provider_id != "dreamina":
            values["base_url"] = _safe_base_url(base_url)
        if model_id is not _UNSET:
            values["model_id"] = _safe_model(model_id)
        if api_key is not _UNSET and provider_id != "dreamina":
            key = str(api_key or "").strip()
            if len(key) > 10000:
                raise ValueError("API Key 长度异常")
            values["protected_key"] = protect_text(key)
        if cli_path is not _UNSET and provider_id == "dreamina":
            path = str(cli_path or "").strip()
            if path and (not Path(path).is_absolute() or Path(path).name.casefold() != "dreamina.exe"):
                raise ValueError("请选择 dreamina.exe 的绝对路径")
            values["cli_path"] = path
        profiles[provider_id] = replace(profiles[provider_id], **values)
        parser = configparser.ConfigParser(interpolation=None)
        for key, profile in profiles.items():
            parser[f"provider:{key}"] = {
                "enabled": str(profile.enabled).lower(),
                "base_url": profile.base_url,
                "model_id": profile.model_id,
                "api_key": profile.protected_key,
                "cli_path": profile.cli_path,
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write("# LingJingAPI 第三方模型；API Key 由当前 Windows 用户的 DPAPI 加密。\n")
                parser.write(handle)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return profiles[provider_id]


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "provider_error", status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


def _json_request(method: str, url: str, key: str, payload: dict | None = None,
                  timeout: int = 120) -> dict:
    try:
        response = requests.request(
            method, url, json=payload,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=(10, timeout), allow_redirects=False,
            stream=True,
        )
        with response:
            if 300 <= response.status_code < 400:
                raise ProviderError("服务端返回了未允许的跳转", "provider_redirect")
            if response.status_code in (401, 403):
                raise ProviderError("第三方 API Key 无效或没有调用权限", "provider_auth", 401)
            if response.status_code >= 400:
                raise ProviderError(f"第三方服务返回 HTTP {response.status_code}", "provider_http")
            chunks = []
            length = 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > _MAX_RESPONSE:
                    raise ProviderError("第三方响应过大", "provider_response_too_large")
                chunks.append(chunk)
            result = json.loads(b"".join(chunks))
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError("第三方网络请求失败或响应格式无效", "provider_network") from exc
    if not isinstance(result, dict):
        raise ProviderError("第三方响应格式无效", "provider_response")
    return result


def _image_reference(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) > 28 * 1024 * 1024 or not (value.startswith("https://") or value.startswith("data:image/")):
        raise ProviderError("参考图需要 HTTPS 地址或 data:image 数据", "invalid_image", 422)
    return value


def resolve_dreamina_cli(configured: str = "") -> str:
    if configured:
        path = Path(configured)
        return str(path) if path.is_file() and path.name.casefold() == "dreamina.exe" else ""
    return shutil.which("dreamina.exe") or ""


def _dreamina_binding_marker(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "dreamina-cli" / "home" / ".dreamina_cli" / "oauth-binding-confirmed.json"


def _confirm_dreamina_binding(runtime_dir: Path) -> None:
    marker = _dreamina_binding_marker(runtime_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"confirmed_at": int(time.time())}), encoding="utf-8")
    temporary.replace(marker)


def dreamina_environment(runtime_dir: Path) -> dict[str, str]:
    home = Path(runtime_dir) / "dreamina-cli" / "home"
    app_data = home / "AppData" / "Roaming"
    local_data = home / "AppData" / "Local"
    temp = local_data / "Temp"
    for directory in (home, app_data, local_data, temp):
        directory.mkdir(parents=True, exist_ok=True)
    # A narrow environment prevents the child from receiving gateway/admin keys.
    allowed = ("PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC", "LANG",
               "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    source = {key.upper(): value for key, value in os.environ.items()}
    env = {key: source[key.upper()] for key in allowed if key.upper() in source}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(app_data),
               LOCALAPPDATA=str(local_data), TEMP=str(temp), TMP=str(temp),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local" / "share"))
    return env


def _cli_call(profile: ProviderProfile, runtime_dir: Path, args: list[str], timeout: int = 120) -> str:
    executable = resolve_dreamina_cli(profile.cli_path)
    if not executable:
        raise ProviderError("未找到 dreamina.exe，请先安装即梦 CLI 并在模型管理中指定路径", "cli_missing", 409)
    try:
        result = subprocess.run(
            [executable, *args], env=dreamina_environment(runtime_dir),
            cwd=str(Path(runtime_dir)), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError("即梦 CLI 启动失败或等待超时", "cli_failed") from exc
    output = f"{result.stdout or ''}\n{result.stderr or ''}"[-100_000:]
    diagnostic = output
    if re.search(r"not logged in|login required|token.{0,20}(expired|invalid)|请先登录|登录.{0,8}(失效|过期)", diagnostic, re.I):
        raise ProviderError("即梦登录已失效，请重新绑定账号", "cli_login_required", 409)
    if "AigcComplianceConfirmationRequired" in diagnostic:
        raise ProviderError("请先到即梦网页端完成该模型的首次使用确认", "cli_confirmation_required", 409)
    if result.returncode and not re.search(r"submit_id|gen_status", output):
        raise ProviderError("即梦 CLI 返回失败；请检查登录状态和额度", "cli_failed")
    return output


def start_dreamina_login(profile: ProviderProfile, runtime_dir: Path) -> dict[str, str]:
    output = _cli_call(profile, runtime_dir, ["login", "--headless"], 60)
    def field(name: str) -> str:
        match = re.search(rf"\b{name}\s*[:=]\s*['\"]?([^\s'\"]+)", output, re.I)
        return match.group(1) if match else ""
    reused = bool(re.search(r"已复用当前本地 OAuth 登录态|reused? (?:the )?(?:current )?local OAuth", output, re.I))
    if reused:
        _confirm_dreamina_binding(runtime_dir)
    return {"verification_uri": field("verification_uri"), "user_code": field("user_code"),
            "device_code": field("device_code"),
            "reused": reused}


def finish_dreamina_login(profile: ProviderProfile, runtime_dir: Path, device_code: str) -> bool:
    if not re.fullmatch(r"[\w.-]{1,300}", str(device_code or "")):
        raise ProviderError("即梦授权码无效", "cli_invalid_code", 422)
    output = _cli_call(profile, runtime_dir,
                       ["login", "checklogin", f"--device_code={device_code}", "--poll=30"], 45)
    if re.search(r"pending|awaiting|not authorized|待授权|等待授权|尚未授权|未完成授权", output, re.I):
        return False
    if not re.search(r"success|authorized|authenticated|logged[ -]?in|登录成功|授权成功|已登录|绑定成功", output, re.I):
        raise ProviderError("即梦 CLI 未确认授权成功，请稍后重试", "cli_login_unconfirmed", 409)
    _confirm_dreamina_binding(runtime_dir)
    return True


def _cli_result(text: str, kind: str) -> tuple[str, str, str]:
    # CLI prints JSON as well as explanatory lines.  Extract the final JSON line
    # where possible; fall back to labelled values in older releases.
    objects = []
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            item, _ = decoder.raw_decode(text[index:])
            if isinstance(item, dict):
                objects.append(item)
        except ValueError:
            continue
    def values(names: set[str]) -> list[str]:
        found = []
        def walk(node):
            if isinstance(node, list):
                for child in node:
                    walk(child)
            elif isinstance(node, dict):
                for key, child in node.items():
                    if re.search(r"input|prompt|reference|thumbnail|preview|cover", key, re.I):
                        continue
                    if key in names and isinstance(child, str):
                        found.append(child)
                    elif key in names and isinstance(child, list):
                        found.extend(value for value in child if isinstance(value, str))
                    if isinstance(child, (dict, list)):
                        walk(child)
        for item in objects:
            walk(item)
        return found
    task_id = (values({"submit_id", "submitId"}) or [""])[-1]
    status = (values({"gen_status", "status", "task_status"}) or [""])[-1].casefold()
    url_key = "image_url" if kind == "image" else "video_url"
    urls = values({url_key, "image_urls" if kind == "image" else "video_urls",
                   "imageUrl" if kind == "image" else "videoUrl", "url"})
    url = next((value for value in urls if value.startswith("https://")), "")
    if not task_id:
        found = re.search(r"\bsubmit_?id\s*[:=]\s*['\"]?([\w-]+)", text, re.I)
        task_id = found.group(1) if found else ""
    if not status:
        found = re.search(r"\bgen_status\s*[:=]\s*['\"]?([\w-]+)", text, re.I)
        status = found.group(1).casefold() if found else ""
    if not url:
        found = re.search(rf"\b{url_key}\s*[:=]\s*['\"]?(https://[^\s'\"]+)", text, re.I)
        url = found.group(1) if found else ""
    return task_id, status, url


def execute_provider(profile: ProviderProfile, body: dict, runtime_dir: Path,
                     cancel_event=None) -> list[dict]:
    if not profile.configured:
        raise ProviderError("该第三方模型尚未配置或启用", "provider_not_configured", 409)
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ProviderError("请输入提示词", "prompt_required", 422)
    if len(prompt) > 100_000:
        raise ProviderError("提示词过长", "prompt_too_long", 422)
    if cancel_event is not None and cancel_event.is_set():
        return []
    if profile.provider_id == "dreamina":
        return _execute_dreamina(profile, body, runtime_dir, cancel_event)
    key = unprotect_text(profile.protected_key)
    if profile.provider_id == "deepseek":
        messages = []
        raw_messages = body.get("messages")
        if isinstance(raw_messages, list):
            for message in raw_messages:
                if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                    continue
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    messages.append({"role": message["role"], "content": content})
        if not messages:
            if body.get("system_prompt"):
                messages.append({"role": "system", "content": str(body["system_prompt"])})
            messages.append({"role": "user", "content": prompt})
        if sum(len(item["content"]) for item in messages) > 100_000:
            raise ProviderError("对话内容过长", "prompt_too_long", 422)
        payload = {"model": profile.model_id, "messages": messages, "stream": False}
        response_format = body.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
            messages.insert(0, {"role": "system", "content": "Return exactly one valid JSON object, with no Markdown or explanation."})
        result = _json_request("POST", f"{profile.base_url}/chat/completions", key, payload)
        try:
            return [{"type": "text", "text": str(result["choices"][0]["message"]["content"])}]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("DeepSeek 未返回文字", "provider_response") from None
    if profile.provider_id == "seedream":
        payload = {"model": profile.model_id, "prompt": prompt, "response_format": "url"}
        for name in ("size", "seed", "watermark"):
            if body.get(name) is not None:
                payload[name] = body[name]
        image = _image_reference(body.get("image"))
        if image:
            payload["image"] = image
        result = _json_request("POST", f"{profile.base_url}/images/generations", key, payload, 180)
        outputs = [{"type": "image", "external_url": str(item["url"])}
                   for item in result.get("data", []) if isinstance(item, dict) and item.get("url")]
        if not outputs:
            raise ProviderError("Seedream 未返回图片地址", "provider_response")
        return outputs
    if profile.provider_id == "seedance":
        content = [{"type": "text", "text": prompt}]
        for name, role in (("image", "image"), ("start_image", "first_frame"), ("end_image", "last_frame")):
            image = _image_reference(body.get(name))
            if image:
                content.append({"type": "image_url", "role": role, "image_url": {"url": image}})
        payload = {"model": profile.model_id, "content": content}
        for name in ("duration", "resolution", "ratio", "seed"):
            if body.get(name) is not None:
                payload[name] = body[name]
        result = _json_request("POST", f"{profile.base_url}/contents/generations/tasks", key, payload)
        task_id = str(result.get("id") or result.get("task_id") or "")
        if not task_id or not re.fullmatch(r"[\w-]{1,150}", task_id):
            raise ProviderError("Seedance 未返回有效任务编号", "provider_response")
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            if cancel_event is not None:
                if cancel_event.wait(3):
                    return []
            else:
                time.sleep(3)
            status = _json_request("GET", f"{profile.base_url}/contents/generations/tasks/{task_id}", key)
            phase = str(status.get("status") or "").casefold()
            if phase in {"failed", "error", "cancelled"}:
                raise ProviderError("Seedance 视频生成失败", "provider_failed")
            if phase in {"succeeded", "completed", "success"}:
                content = status.get("content") or status.get("output") or {}
                url = content.get("video_url") or content.get("url") if isinstance(content, dict) else ""
                url = url or status.get("video_url") or status.get("url")
                if not url:
                    raise ProviderError("Seedance 未返回视频地址", "provider_response")
                return [{"type": "video", "external_url": str(url)}]
        raise ProviderError("Seedance 等待超时", "provider_timeout")
    raise ProviderError("未知第三方模型", "provider_unknown", 404)


def _dreamina_input(value: str, folder: Path, name: str) -> Path:
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)",
                         str(value or ""), re.I)
    if not match or len(match.group(2)) > 28 * 1024 * 1024:
        raise ProviderError("即梦参考图需要 PNG/JPEG/WebP 的 data:image 数据", "invalid_image", 422)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        raise ProviderError("即梦参考图编码无效", "invalid_image", 422) from None
    if not data or len(data) > 20 * 1024 * 1024:
        raise ProviderError("即梦参考图超出 20 MB", "invalid_image", 422)
    kind = match.group(1).casefold()
    signatures = {"png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                  "jpeg": data.startswith(b"\xff\xd8\xff"),
                  "webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP"}
    if not signatures[kind]:
        raise ProviderError("即梦参考图文件格式无效", "invalid_image", 422)
    path = folder / f"{name}.{'jpg' if kind == 'jpeg' else kind}"
    path.write_bytes(data)
    return path


def _execute_dreamina(profile: ProviderProfile, body: dict, runtime_dir: Path, cancel_event) -> list[dict]:
    kind = str(body.get("kind") or "image").casefold()
    if kind not in {"image", "video"}:
        raise ProviderError("即梦仅支持图片或视频", "invalid_kind", 422)
    with tempfile.TemporaryDirectory(prefix="dreamina-input-", dir=runtime_dir) as folder_name:
        folder = Path(folder_name)
        image = _dreamina_input(body["image"], folder, "image") if body.get("image") else None
        first = _dreamina_input(body["start_image"], folder, "first") if body.get("start_image") else None
        last = _dreamina_input(body["end_image"], folder, "last") if body.get("end_image") else None
        if kind == "image":
            command = "image2image" if image else "text2image"
        else:
            command = "frames2video" if first and last else "image2video" if first or image else "text2video"
        args = [command, f"--prompt={body['prompt']}", "--poll=0"]
        if command == "image2image":
            args.append(f"--images={image}")
        elif command == "image2video":
            args.append(f"--image={first or image}")
        elif command == "frames2video":
            args.extend((f"--first={first}", f"--last={last}"))
        if kind == "image":
            args.append(f"--resolution_type={body.get('resolution_type') or '2k'}")
        else:
            args.append(f"--video_resolution={body.get('resolution') or '720p'}")
            if body.get("duration"):
                try:
                    args.append(f"--duration={int(body['duration'])}")
                except (TypeError, ValueError):
                    raise ProviderError("视频时长必须是整数秒", "invalid_duration", 422) from None
        if body.get("model_version"):
            args.append(f"--model_version={_safe_model(body['model_version'])}")
        if body.get("ratio") and command in {"text2image", "image2image", "text2video"}:
            args.append(f"--ratio={body['ratio']}")
        submitted = _cli_call(profile, runtime_dir, args)
    task_id, status, url = _cli_result(submitted, kind)
    if not task_id and not url:
        raise ProviderError("即梦未返回任务 ID，请先检查即梦任务记录，避免重复提交", "cli_ambiguous")
    deadline = time.monotonic() + 1800
    while not url and time.monotonic() < deadline:
        if status in {"fail", "failed", "error", "cancelled"}:
            raise ProviderError("即梦生成失败，请检查账号额度和任务记录", "cli_failed")
        if cancel_event is not None:
            if cancel_event.wait(4):
                return []
        else:
            time.sleep(4)
        queried = _cli_call(profile, runtime_dir, ["query_result", f"--submit_id={task_id}"], 45)
        _, status, url = _cli_result(queried, kind)
    if not url:
        raise ProviderError("即梦结果查询超时，可用任务 ID 在即梦 CLI 查询", "cli_timeout")
    return [{"type": kind, "external_url": url}]
