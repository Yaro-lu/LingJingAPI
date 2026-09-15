"""Prepare verified ComfyUI stable releases without mutating the live install."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


COMFYUI_RELEASE_POLICY_PATH = Path(__file__).resolve().parents[1] / "comfyui_release.json"
CANONICAL_REPOSITORY_URL = "https://github.com/Comfy-Org/ComfyUI"
CANONICAL_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest"
)
_STABLE_SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ComfyUIUpdateError(RuntimeError):
    """Base class for controlled updater failures."""


class ReleaseValidationError(ComfyUIUpdateError):
    """The upstream release response is not an official stable release."""


class DownloadValidationError(ComfyUIUpdateError):
    """A release download violated the source or size policy."""


class ArchiveSecurityError(ComfyUIUpdateError):
    """A ZIP archive is unsafe or does not contain a complete ComfyUI core."""


class DependencyPolicyError(ComfyUIUpdateError):
    """A requirements change cannot be applied by the constrained updater."""


@dataclass(frozen=True)
class ValidatedRelease:
    release_id: int
    tag_name: str
    version: str
    repository_url: str
    api_url: str
    html_url: str
    zipball_url: str
    source: str = 'github'
    source_git: str = ''
    commit: str = ''


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    bytes_downloaded: int
    content_length: int | None
    final_url: str


@dataclass(frozen=True)
class ExtractionResult:
    root: Path
    archive_root: str
    member_count: int
    extracted_files: int
    extracted_bytes: int
    excluded_protected_paths: tuple[str, ...]


@dataclass(frozen=True)
class DependencyPlan:
    overlay_requirements: tuple[str, ...]
    full_environment_required: bool
    blocked_changes: tuple[str, ...]
    unsupported_changes: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PreparedUpdate:
    status: str
    base_dir: Path
    staging_core: Path
    dependency_overlay: Path | None
    overlay_requirements_file: Path | None
    overlay_command: tuple[str, ...]
    release_metadata: Mapping[str, object]
    dependency_plan: DependencyPlan
    cleanup_warnings: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "base_dir": str(self.base_dir),
            "staging_core": str(self.staging_core),
            "dependency_overlay": (
                str(self.dependency_overlay) if self.dependency_overlay else ""
            ),
            "overlay_requirements_file": (
                str(self.overlay_requirements_file) if self.overlay_requirements_file else ""
            ),
            "overlay_command": list(self.overlay_command),
            "release_metadata": dict(self.release_metadata),
            "dependency_plan": {
                "overlay_requirements": list(self.dependency_plan.overlay_requirements),
                "full_environment_required": self.dependency_plan.full_environment_required,
                "blocked_changes": list(self.dependency_plan.blocked_changes),
                "unsupported_changes": list(self.dependency_plan.unsupported_changes),
                "reasons": list(self.dependency_plan.reasons),
            },
            "cleanup_warnings": list(self.cleanup_warnings),
        }


def load_update_policy(
    path: Path = COMFYUI_RELEASE_POLICY_PATH,
    *,
    read_text: Callable[[Path], str] | None = None,
) -> dict[str, object]:
    reader = read_text or (lambda source: source.read_text(encoding="utf-8-sig"))
    try:
        payload = json.loads(reader(Path(path)))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComfyUIUpdateError(f"无法读取 ComfyUI 更新策略: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ComfyUIUpdateError("ComfyUI 更新策略版本无效")
    if (
        payload.get("repository_url") != CANONICAL_REPOSITORY_URL
        or payload.get("latest_release_url") != CANONICAL_LATEST_RELEASE_URL
        or payload.get("pypi_index_url") != "https://pypi.org/simple"
        or payload.get("zipball_host") != "api.github.com"
        or set(payload.get("download_redirect_hosts") or [])
        != {"api.github.com", "codeload.github.com"}
    ):
        raise ComfyUIUpdateError("ComfyUI 更新策略未锁定官方仓库")
    return payload


def _validate_policy_identity(policy: Mapping[str, object]) -> None:
    if (
        policy.get("repository_url") != CANONICAL_REPOSITORY_URL
        or policy.get("latest_release_url") != CANONICAL_LATEST_RELEASE_URL
        or policy.get("pypi_index_url") != "https://pypi.org/simple"
        or policy.get("zipball_host") != "api.github.com"
        or set(policy.get("download_redirect_hosts") or [])
        != {"api.github.com", "codeload.github.com"}
    ):
        raise ReleaseValidationError("ComfyUI 更新策略未锁定官方仓库")


def _strict_https_url(value: object, *, label: str) -> tuple[str, object]:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseValidationError(f"{label}无效")
    return url, parsed


def validate_latest_release(
    payload: Mapping[str, object],
    policy: Mapping[str, object] | None = None,
) -> ValidatedRelease:
    """Accept only the official GitHub latest stable semver release record."""
    policy = load_update_policy() if policy is None else policy
    _validate_policy_identity(policy)
    if not isinstance(payload, Mapping):
        raise ReleaseValidationError("GitHub 发布信息格式无效")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ReleaseValidationError("仅允许更新到 ComfyUI 稳定版")
    if payload.get("immutable") is not True:
        raise ReleaseValidationError("仅允许下载 GitHub 已锁定的不可变发布")

    tag_name = str(payload.get("tag_name") or "").strip()
    match = _STABLE_SEMVER.fullmatch(tag_name)
    if not match:
        raise ReleaseValidationError("ComfyUI 版本标签不是稳定语义版本")
    version = ".".join(match.groups())

    repository_url = str(policy.get("repository_url") or "").rstrip("/")
    expected_repo_path = urlsplit(repository_url).path.rstrip("/")
    api_url, api_parts = _strict_https_url(payload.get("url"), label="GitHub API 地址")
    html_url, html_parts = _strict_https_url(payload.get("html_url"), label="GitHub 项目地址")
    zipball_url, zip_parts = _strict_https_url(
        payload.get("zipball_url"), label="ComfyUI 下载地址"
    )

    release_id = payload.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise ReleaseValidationError("GitHub 发布 ID 无效")
    expected_api_path = f"/repos{expected_repo_path}/releases/{release_id}"
    expected_html_path = f"{expected_repo_path}/releases/tag/{tag_name}"
    expected_zip_path = f"/repos{expected_repo_path}/zipball/{tag_name}"
    if api_parts.hostname.casefold() != "api.github.com" or api_parts.path != expected_api_path:
        raise ReleaseValidationError("GitHub API 地址不属于官方 ComfyUI 仓库")
    if html_parts.hostname.casefold() != "github.com" or html_parts.path != expected_html_path:
        raise ReleaseValidationError("GitHub 项目地址不属于官方 ComfyUI 仓库")
    if (
        zip_parts.hostname.casefold() != str(policy.get("zipball_host") or "").casefold()
        or zip_parts.path != expected_zip_path
    ):
        raise ReleaseValidationError("ComfyUI 下载地址不属于官方 ComfyUI 仓库")

    return ValidatedRelease(
        release_id=release_id,
        tag_name=tag_name,
        version=version,
        repository_url=repository_url,
        api_url=api_url,
        html_url=html_url,
        zipball_url=zipball_url,
    )


def fetch_latest_release(
    policy: Mapping[str, object] | None = None,
    *,
    opener: Callable[..., object] | None = None,
    timeout: int = 20,
    channel_callback=None,
) -> ValidatedRelease:
    """Fetch the configured GitHub ``latest`` endpoint through an injectable opener."""
    if opener is None:
        from app.core.comfyui_update_channels import find_domestic_release
        if channel_callback:
            channel_callback('获取稳定版信息：GitHub 官方源（https://api.github.com）')
        try:
            return fetch_latest_release(policy, opener=urlopen, timeout=timeout)
        except ReleaseValidationError:
            raise
        except ComfyUIUpdateError as official_error:
            try:
                return find_domestic_release(channel_callback=channel_callback)
            except ComfyUIUpdateError as mirror_error:
                raise ComfyUIUpdateError(f'官方版本查询失败：{official_error}；{mirror_error}') from mirror_error
    policy = load_update_policy() if policy is None else policy
    _validate_policy_identity(policy)
    endpoint, endpoint_parts = _strict_https_url(
        policy.get("latest_release_url"), label="GitHub latest 地址"
    )
    if (
        endpoint_parts.hostname.casefold() != "api.github.com"
        or endpoint_parts.path
        != "/repos/Comfy-Org/ComfyUI/releases/latest"
    ):
        raise ReleaseValidationError("GitHub latest 地址不属于官方 ComfyUI 仓库")
    request = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "LingJing-ComfyUI-Updater/1.0",
        },
    )
    limit = int(policy.get("max_release_json_bytes") or 0)
    if not 1024 <= limit <= 4 * 1024 * 1024:
        raise ComfyUIUpdateError("发布信息大小策略无效")
    try:
        with opener(request, timeout=int(timeout)) as response:
            final_url = str(getattr(response, "geturl", lambda: endpoint)())
            if final_url != endpoint:
                raise ReleaseValidationError("GitHub latest 地址发生了未授权重定向")
            raw = response.read(limit + 1)
    except ComfyUIUpdateError:
        raise
    except (OSError, TimeoutError) as exc:
        raise ComfyUIUpdateError(f"无法获取 ComfyUI 稳定版信息: {exc}") from exc
    if len(raw) > limit:
        raise ReleaseValidationError("GitHub 发布信息超过大小上限")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("GitHub 发布信息不是有效 JSON") from exc
    return validate_latest_release(payload, policy)


def read_current_comfyui_version(
    comfyui_root: Path,
    *,
    read_text: Callable[[Path], str] | None = None,
) -> str:
    """Read a literal version assignment without importing or executing ComfyUI."""
    version_path = Path(comfyui_root) / "comfyui_version.py"
    reader = read_text or (lambda path: path.read_text(encoding="utf-8-sig"))
    try:
        source = reader(version_path)
        tree = ast.parse(source, filename=str(version_path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ComfyUIUpdateError(f"无法读取当前 ComfyUI 版本: {version_path}") from exc
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        value_node = statement.value
        if not any(
            isinstance(target, ast.Name)
            and target.id in {"__version__", "VERSION", "version"}
            for target in targets
        ):
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            continue
        match = _STABLE_SEMVER.fullmatch(str(value).strip())
        if match:
            return ".".join(match.groups())
    raise ComfyUIUpdateError("当前 comfyui_version.py 不包含有效稳定版本")


def _validate_download_redirect(url: str, policy: Mapping[str, object]) -> str:
    final_url, parsed = _strict_https_url(url, label="ComfyUI 下载重定向地址")
    allowed_hosts = {
        str(host).casefold()
        for host in (policy.get("download_redirect_hosts") or [])
        if str(host).strip()
    }
    host = parsed.hostname.casefold()
    official_path = parsed.path.casefold()
    path_valid = (
        host == "api.github.com"
        and official_path.startswith("/repos/comfy-org/comfyui/zipball/")
    ) or (
        host == "codeload.github.com"
        and official_path.startswith("/comfy-org/comfyui/")
    )
    if host not in allowed_hosts or not path_valid:
        raise DownloadValidationError("ComfyUI 下载发生了未授权重定向")
    return final_url


_RELEASE_ARCHIVE_NAME = re.compile(
    r"^\.comfyui-update-release-([0-9a-f]{32})\.zip(?:\.part)?$"
)
_REPARSE_POINT = 0x400
_STALE_ARCHIVE_AGE_SECONDS = 24 * 60 * 60


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _controlled_unlink(path: Path, *, label: str) -> str | None:
    """Best-effort unlink of one exact file without following filesystem links."""
    candidate = Path(path)
    try:
        if not os.path.lexists(str(candidate)):
            return None
        details = candidate.lstat()
        if candidate.is_symlink() or _is_reparse_point(candidate):
            return f"{label}未清理：拒绝删除链接或重解析点 {candidate}"
        if not stat.S_ISREG(details.st_mode):
            return f"{label}未清理：目标不是普通文件 {candidate}"
        candidate.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{label}未清理 {candidate}：{exc}"
    return None


def _attach_cleanup_notes(error: BaseException, warnings: list[str]) -> None:
    if not warnings:
        return
    note = "ComfyUI 更新临时文件清理警告：" + "；".join(warnings)
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)


def _cleanup_owned_archive(
    base: Path,
    artifact: Path,
    *,
    operation_id: str,
) -> str | None:
    """Delete only a direct-child archive whose name matches this transaction."""
    base_path = Path(base).resolve(strict=True)
    candidate = Path(os.path.abspath(str(artifact)))
    match = _RELEASE_ARCHIVE_NAME.fullmatch(candidate.name)
    if (
        candidate.parent != base_path
        or not match
        or match.group(1) != operation_id
    ):
        return f"拒绝清理未验证的 ComfyUI 更新压缩包：{candidate}"
    return _controlled_unlink(candidate, label="ComfyUI 更新压缩包")


def _cleanup_stale_release_archives(
    base: Path,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    """Remove only old, plain updater archives from the client's direct children."""
    base_path = Path(base).resolve(strict=True)
    current_time = time.time() if now is None else float(now)
    warnings: list[str] = []
    try:
        candidates = list(base_path.iterdir())
    except OSError as exc:
        return (f"无法检查历史 ComfyUI 更新压缩包：{exc}",)
    for candidate in candidates:
        if not _RELEASE_ARCHIVE_NAME.fullmatch(candidate.name):
            continue
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(f"无法检查历史 ComfyUI 更新压缩包 {candidate.name}：{exc}")
            continue
        if current_time - float(details.st_mtime) < _STALE_ARCHIVE_AGE_SECONDS:
            continue
        warning = _controlled_unlink(candidate, label="历史 ComfyUI 更新压缩包")
        if warning:
            warnings.append(warning)
    return tuple(warnings)


def download_release_archive(
    release: ValidatedRelease,
    target: Path,
    *,
    policy: Mapping[str, object] | None = None,
    opener: Callable[..., object] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    timeout: int = 60,
    chunk_size: int = 1024 * 1024,
    channel_callback=None,
) -> DownloadResult:
    """Download to ``.part``, report real bytes and calculate SHA256 in one pass."""
    if opener is None:
        from app.core.comfyui_update_channels import download_domestic_archive
        if release.source != 'domestic':
            try:
                return download_release_archive(release, target, policy=policy, opener=urlopen,
                    progress_callback=progress_callback, timeout=timeout, chunk_size=chunk_size)
            except DownloadValidationError:
                raise
            except ComfyUIUpdateError as exc:
                if channel_callback:
                    channel_callback(f'官方下载失败，切换国内镜像：{exc}')
        return download_domestic_archive(release, target, policy=policy,
            progress_callback=progress_callback, channel_callback=channel_callback)
    policy = load_update_policy() if policy is None else policy
    target = Path(target)
    partial = Path(f"{target}.part")
    if target.exists() or partial.exists():
        raise DownloadValidationError("ComfyUI 下载目标已存在")
    if not 4096 <= int(chunk_size) <= 16 * 1024 * 1024:
        # Tests may intentionally choose tiny chunks; keep injection useful.
        if int(chunk_size) <= 0:
            raise ValueError("chunk_size 必须大于 0")
    max_bytes = int(policy.get("max_archive_bytes") or 0)
    if max_bytes <= 0:
        raise ComfyUIUpdateError("ComfyUI 压缩包大小策略无效")
    request = Request(
        release.zipball_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "LingJing-ComfyUI-Updater/1.0",
        },
    )
    digest = hashlib.sha256()
    downloaded = 0
    content_length: int | None = None
    final_url = release.zipball_url
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener(request, timeout=int(timeout)) as response:
            final_url = _validate_download_redirect(
                str(getattr(response, "geturl", lambda: release.zipball_url)()), policy
            )
            raw_length = str(getattr(response, "headers", {}).get("Content-Length", "")).strip()
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise DownloadValidationError("ComfyUI 下载大小响应无效") from exc
                if content_length < 0 or content_length > max_bytes:
                    raise DownloadValidationError("ComfyUI 压缩包超过下载大小上限")
            if progress_callback:
                progress_callback(0, content_length)
            with partial.open("xb") as handle:
                while True:
                    chunk = response.read(int(chunk_size))
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise DownloadValidationError("ComfyUI 压缩包超过下载大小上限")
                    digest.update(chunk)
                    handle.write(chunk)
                    if progress_callback:
                        progress_callback(downloaded, content_length)
        if content_length is not None and downloaded != content_length:
            raise DownloadValidationError("ComfyUI 下载不完整")
        partial.replace(target)
    except ComfyUIUpdateError as exc:
        cleanup_warnings: list[str] = []
        warning = _controlled_unlink(partial, label="ComfyUI 下载临时文件")
        if warning:
            cleanup_warnings.append(warning)
        _attach_cleanup_notes(exc, cleanup_warnings)
        raise
    except (OSError, TimeoutError) as exc:
        error = ComfyUIUpdateError(f"下载 ComfyUI 稳定版失败: {exc}")
        cleanup_warnings = []
        warning = _controlled_unlink(partial, label="ComfyUI 下载临时文件")
        if warning:
            cleanup_warnings.append(warning)
        _attach_cleanup_notes(error, cleanup_warnings)
        raise error from exc
    except Exception as exc:
        cleanup_warnings = []
        warning = _controlled_unlink(partial, label="ComfyUI 下载临时文件")
        if warning:
            cleanup_warnings.append(warning)
        _attach_cleanup_notes(exc, cleanup_warnings)
        raise
    return DownloadResult(
        path=target,
        sha256=digest.hexdigest(),
        bytes_downloaded=downloaded,
        content_length=content_length,
        final_url=final_url,
    )


_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def _safe_zip_parts(name: str) -> tuple[str, ...]:
    raw = str(name or "")
    portable = raw.replace("\\", "/")
    if (
        not portable
        or "\x00" in portable
        or portable.startswith("/")
        or re.match(r"^[A-Za-z]:", portable)
    ):
        raise ArchiveSecurityError(f"压缩包包含不安全路径: {raw}")
    parts = PurePosixPath(portable).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ArchiveSecurityError(f"压缩包包含不安全路径: {raw}")
    for part in parts:
        if (
            ":" in part
            or part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise ArchiveSecurityError(f"压缩包包含 Windows 不安全路径: {raw}")
    return tuple(parts)


def _zip_member_is_link_or_special(info: zipfile.ZipInfo) -> bool:
    if info.create_system != 3:
        return False
    file_type = (info.external_attr >> 16) & 0o170000
    return file_type not in {0, stat.S_IFREG, stat.S_IFDIR}


def safe_extract_release_archive(
    archive: Path,
    destination: Path,
    *,
    policy: Mapping[str, object] | None = None,
    zip_factory: Callable[..., zipfile.ZipFile] = zipfile.ZipFile,
    chunk_size: int = 1024 * 1024,
) -> ExtractionResult:
    """Strip GitHub's single root and extract only replaceable ComfyUI core files."""
    policy = load_update_policy() if policy is None else policy
    archive = Path(archive)
    destination = Path(destination)
    if destination.exists():
        raise ArchiveSecurityError(f"ComfyUI 暂存目录已存在: {destination}")
    if int(chunk_size) <= 0:
        raise ValueError("chunk_size 必须大于 0")
    try:
        max_members = int(policy.get("max_archive_members") or 0)
        max_member_bytes = int(policy.get("max_member_bytes") or 0)
        max_total_bytes = int(policy.get("max_extracted_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise ArchiveSecurityError("ComfyUI 解压限制策略无效") from exc
    if min(max_members, max_member_bytes, max_total_bytes) <= 0:
        raise ArchiveSecurityError("ComfyUI 解压限制策略无效")

    protected = {
        str(value).replace("\\", "/").strip("/").casefold()
        for value in (policy.get("protected_paths") or [])
        if str(value).strip()
    }
    required = tuple(str(value).replace("\\", "/").strip("/") for value in (
        policy.get("required_core_paths") or []
    ))
    scratch = destination.parent / f".{destination.name}.extract-{uuid.uuid4().hex}"
    extracted_files = 0
    extracted_bytes = 0
    archive_root = ""
    excluded: set[str] = set()
    try:
        with zip_factory(archive, "r") as bundle:
            members = bundle.infolist()
            if not members:
                raise ArchiveSecurityError("ComfyUI 压缩包为空")
            if len(members) > max_members:
                raise ArchiveSecurityError(f"ComfyUI 压缩包条目数量超过上限: {max_members}")
            declared_total = 0
            prepared: list[tuple[zipfile.ZipInfo, tuple[str, ...], bool]] = []
            roots: set[str] = set()
            seen: set[str] = set()
            for info in members:
                if info.flag_bits & 0x1:
                    raise ArchiveSecurityError(f"压缩包包含加密条目: {info.filename}")
                if _zip_member_is_link_or_special(info):
                    raise ArchiveSecurityError(f"压缩包包含链接或特殊文件: {info.filename}")
                parts = _safe_zip_parts(info.filename)
                roots.add(parts[0])
                is_directory = info.is_dir() or info.filename.endswith(("/", "\\"))
                if not is_directory and len(parts) < 2:
                    raise ArchiveSecurityError("ComfyUI 压缩包必须包含单一根目录")
                relative = parts[1:]
                if relative:
                    folded = "/".join(relative).casefold()
                    if folded in seen:
                        raise ArchiveSecurityError(
                            f"压缩包包含大小写重复路径: {'/'.join(relative)}"
                        )
                    seen.add(folded)
                if info.file_size < 0 or info.file_size > max_member_bytes:
                    raise ArchiveSecurityError(
                        f"压缩包单个条目超过大小上限: {info.filename}"
                    )
                declared_total += int(info.file_size)
                if declared_total > max_total_bytes:
                    raise ArchiveSecurityError(
                        f"ComfyUI 压缩包解压大小超过上限: {max_total_bytes}"
                    )
                prepared.append((info, relative, is_directory))
            if len(roots) != 1:
                raise ArchiveSecurityError("ComfyUI 压缩包必须包含单一根目录")
            archive_root = next(iter(roots))

            scratch.mkdir(parents=True, exist_ok=False)
            for info, relative, is_directory in prepared:
                if not relative:
                    continue
                protected_key = relative[0].casefold()
                if protected_key in protected:
                    excluded.add(relative[0])
                    continue
                target = scratch.joinpath(*relative)
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                member_bytes = 0
                with bundle.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(int(chunk_size))
                        if not chunk:
                            break
                        member_bytes += len(chunk)
                        extracted_bytes += len(chunk)
                        if member_bytes > info.file_size or extracted_bytes > max_total_bytes:
                            raise ArchiveSecurityError(
                                f"压缩包实际解压大小超过声明: {info.filename}"
                            )
                        output.write(chunk)
                if member_bytes != info.file_size:
                    raise ArchiveSecurityError(f"压缩包条目不完整: {info.filename}")
                extracted_files += 1
        missing = [relative for relative in required if not (scratch / relative).is_file()]
        if missing:
            raise ArchiveSecurityError(
                f"ComfyUI 核心目录结构不完整，缺少: {', '.join(missing)}"
            )
        for relative in protected:
            if (scratch / relative).exists():
                raise ArchiveSecurityError(f"暂存目录包含受保护路径: {relative}")
        scratch.replace(destination)
    except ArchiveSecurityError:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise ArchiveSecurityError(f"无法安全解压 ComfyUI 稳定版: {exc}") from exc
    return ExtractionResult(
        root=destination,
        archive_root=archive_root,
        member_count=len(members),
        extracted_files=extracted_files,
        extracted_bytes=extracted_bytes,
        excluded_protected_paths=tuple(sorted(excluded, key=str.casefold)),
    )


def _normalise_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _parse_requirements_text(text: str, *, label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line_number, original in enumerate(str(text).splitlines(), start=1):
        line = re.sub(r"\s+#.*$", "", original).strip()
        if not line or line.startswith("#"):
            continue
        if (
            line.startswith("-")
            or "\\" in line
            or " @ " in line
            or "://" in line
            or line.startswith(("git+", "file:"))
        ):
            raise DependencyPolicyError(
                f"{label} 第 {line_number} 行包含不安全 requirements 指令"
            )
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9,._-]+\])?\s*(.*)",
            line,
        )
        if not match:
            raise DependencyPolicyError(f"{label} 第 {line_number} 行格式无效")
        name = _normalise_package_name(match.group(1))
        if name in parsed:
            raise DependencyPolicyError(f"{label} 包含重复依赖: {name}")
        parsed[name] = line
    return parsed


def plan_dependency_overlay(
    current_requirements: Path,
    target_requirements: Path,
    *,
    policy: Mapping[str, object] | None = None,
    read_text: Callable[[Path], str] | None = None,
    installed_versions: Mapping[str, str] | None = None,
) -> DependencyPlan:
    """Diff requirements and permit only pinned, audited ComfyUI distribution wheels."""
    policy = load_update_policy() if policy is None else policy
    reader = read_text or (lambda path: path.read_text(encoding="utf-8-sig"))
    try:
        current = _parse_requirements_text(
            reader(Path(current_requirements)), label="当前 requirements"
        )
        target = _parse_requirements_text(
            reader(Path(target_requirements)), label="新版 requirements"
        )
    except DependencyPolicyError:
        raise
    except (OSError, UnicodeError) as exc:
        raise DependencyPolicyError(f"无法读取 ComfyUI requirements: {exc}") from exc

    allowed = {
        _normalise_package_name(str(value))
        for value in (policy.get("overlay_packages") or [])
    }
    blocked = {
        _normalise_package_name(str(value))
        for value in (policy.get("blocked_runtime_packages") or [])
    }
    blocked_prefixes = tuple(
        _normalise_package_name(str(value))
        for value in (policy.get("blocked_package_prefixes") or [])
    )
    overlay: list[str] = []
    blocked_changes: list[str] = []
    unsupported: list[str] = []
    reasons: list[str] = []
    for name in sorted(set(current) | set(target)):
        before = re.sub(r"\s+", "", current.get(name, "")).casefold()
        after = re.sub(r"\s+", "", target.get(name, "")).casefold()
        if before == after:
            continue
        if name in blocked or any(name.startswith(prefix) for prefix in blocked_prefixes):
            blocked_changes.append(name)
            reasons.append(f"{name} 涉及 GPU/运行时依赖，必须更新完整环境")
            continue
        if name in target and installed_versions and name in installed_versions:
            from packaging.requirements import Requirement, InvalidRequirement
            try:
                requirement = Requirement(target[name])
                if not requirement.extras and not requirement.marker and not requirement.url and requirement.specifier.contains(installed_versions[name], prereleases=False):
                    continue
            except (InvalidRequirement, ValueError):
                pass
        if name in allowed and name in target:
            pin = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*"
                r"([0-9A-Za-z][0-9A-Za-z._+-]*)",
                target[name],
            )
            if not pin or _normalise_package_name(pin.group(1)) != name:
                unsupported.append(name)
                reasons.append(f"{name} 未使用精确 == 固定版本，必须更新完整环境")
                continue
            overlay.append(target[name])
            continue
        unsupported.append(name)
        reasons.append(f"{name} 不在受控 overlay 白名单，必须更新完整环境")
    requires_full = bool(blocked_changes or unsupported)
    return DependencyPlan(
        overlay_requirements=tuple(overlay if not requires_full else ()),
        full_environment_required=requires_full,
        blocked_changes=tuple(blocked_changes),
        unsupported_changes=tuple(unsupported),
        reasons=tuple(reasons),
    )


_OVERLAY_NAME = re.compile(r"^\.comfyui-update-overlay-([0-9a-f]{32})$")
PIP_BOOTSTRAP_CODE = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "sys.argv[0]='pip';"
    "runpy.run_module('pip',run_name='__main__')"
)


def build_offline_overlay_pip_command(
    python_executable: Path,
    requirements_file: Path,
    wheelhouse: Path,
    overlay_dir: Path,
    *,
    base_dir: Path,
) -> list[str]:
    """Build a fixed bootstrap argv for the isolated bundled pip package.

    ``wheelhouse`` is retained as the legacy parameter name, but now denotes
    the fixed ``app/updater_runtime`` package root.  The path is passed as a
    separate argument and is never interpolated into executable Python code.
    """
    base = Path(base_dir).resolve(strict=False)
    overlay = Path(overlay_dir).resolve(strict=False)
    requirements = Path(requirements_file).resolve(strict=False)
    python = Path(python_executable).resolve(strict=False)
    updater_root = Path(os.path.abspath(str(Path(wheelhouse))))
    match = _OVERLAY_NAME.fullmatch(overlay.name)
    if not match or overlay.parent != base:
        raise DependencyPolicyError("overlay 目录必须是客户端目录的受控直接子目录")
    operation_id = match.group(1)
    if (
        requirements.parent != base
        or requirements.name != f".comfyui-update-requirements-{operation_id}.txt"
    ):
        raise DependencyPolicyError("overlay requirements 路径与更新操作不匹配")
    if python != (base / "runtime" / "python" / "python.exe").resolve(strict=False):
        raise DependencyPolicyError("overlay 只能使用客户端便携 Python")
    expected_updater_root = base / "app" / "updater_runtime"
    pip_main = updater_root / "pip" / "__main__.py"
    if updater_root != expected_updater_root or not pip_main.is_file():
        raise DependencyPolicyError("内置 pip bootstrap 路径无效")
    for protected_path in (updater_root, updater_root / "pip", pip_main):
        attributes = 0
        try:
            attributes = int(getattr(protected_path.lstat(), "st_file_attributes", 0) or 0)
        except OSError as exc:
            raise DependencyPolicyError("内置 pip bootstrap 不完整") from exc
        if protected_path.is_symlink() or attributes & 0x400:
            raise DependencyPolicyError("内置 pip bootstrap 不允许链接或重解析点")
    return [
        str(python),
        "-c",
        PIP_BOOTSTRAP_CODE,
        str(updater_root),
        "--isolated",
        "install",
        "--index-url",
        "https://pypi.org/simple",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--no-input",
        "--requirement",
        str(requirements),
        "--target",
        str(overlay),
    ]


_STAGING_NAME = re.compile(r"^\.comfyui-update-staging-([0-9a-f]{32})$")


def prepare_comfyui_update(
    base_dir: Path,
    *,
    release_payload: Mapping[str, object] | None = None,
    operation_id: str | None = None,
    policy: Mapping[str, object] | None = None,
    opener: Callable[..., object] | None = None,
    progress_callback: Callable[[int, int | None], None] | None = None,
    channel_callback=None,
) -> PreparedUpdate:
    """Prepare core and dependency overlay inputs; never alter the live ComfyUI tree."""
    policy = load_update_policy() if policy is None else policy
    base = Path(base_dir).resolve(strict=True)
    if not base.is_dir() or base == Path(base.anchor):
        raise ComfyUIUpdateError("客户端目录无效")
    live_core = base / "runtime" / "ComfyUI"
    if not live_core.is_dir():
        raise ComfyUIUpdateError("本地 ComfyUI 核心目录不存在")
    if os.path.lexists(str(live_core / ".git")):
        raise ComfyUIUpdateError(
            "检测到 Git 管理的 ComfyUI；为保护用户自有版本，客户端不会自动覆盖"
        )
    current_version = read_current_comfyui_version(live_core)
    operation = str(operation_id or uuid.uuid4().hex).casefold()
    if not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise ComfyUIUpdateError("ComfyUI 更新操作 ID 无效")
    staging = base / f".comfyui-update-staging-{operation}"
    overlay = base / f".comfyui-update-overlay-{operation}"
    requirements_file = base / f".comfyui-update-requirements-{operation}.txt"
    archive = base / f".comfyui-update-release-{operation}.zip"
    archive_partial = Path(f"{archive}.part")
    cleanup_warnings = list(_cleanup_stale_release_archives(base))
    for path in (staging, overlay, requirements_file, archive, Path(f"{archive}.part")):
        if os.path.lexists(str(path)):
            error = ComfyUIUpdateError(f"ComfyUI 更新暂存路径已存在: {path.name}")
            _attach_cleanup_notes(error, cleanup_warnings)
            raise error

    try:
        release = (
            validate_latest_release(release_payload, policy)
            if release_payload is not None
            else fetch_latest_release(policy, opener=opener, channel_callback=channel_callback)
        )
    except Exception as exc:
        _attach_cleanup_notes(exc, cleanup_warnings)
        raise

    if tuple(map(int, release.version.split("."))) <= tuple(
        map(int, current_version.split("."))
    ):
        empty_plan = DependencyPlan((), False, (), (), ())
        return PreparedUpdate(
            status="up_to_date",
            base_dir=base,
            staging_core=staging,
            dependency_overlay=None,
            overlay_requirements_file=None,
            overlay_command=(),
            release_metadata={
                "repository_url": release.repository_url,
                "tag_name": release.tag_name,
                "version": release.version,
                "release_id": release.release_id,
                "zipball_url": release.zipball_url,
                "archive_sha256": "",
                "archive_size": 0,
                "current_version": current_version,
                "immutable": release.source == 'github',
                "source": release.source,
                "source_git": release.source_git,
                "commit": release.commit,
            },
            dependency_plan=empty_plan,
            cleanup_warnings=tuple(cleanup_warnings),
        )

    try:
        download = download_release_archive(
            release,
            archive,
            policy=policy,
            opener=opener,
            progress_callback=progress_callback,
            channel_callback=channel_callback,
        )
        safe_extract_release_archive(archive, staging, policy=policy)
        dependency_plan = plan_dependency_overlay(
            live_core / "requirements.txt",
            staging / "requirements.txt",
            policy=policy,
        )
        overlay_command: tuple[str, ...] = ()
        dependency_overlay: Path | None = None
        overlay_requirements_file: Path | None = None
        if dependency_plan.full_environment_required:
            status = "full_environment_required"
            shutil.rmtree(staging, ignore_errors=True)
        elif dependency_plan.overlay_requirements:
            status = "overlay_build_required"
            dependency_overlay = overlay
            overlay_requirements_file = requirements_file
            requirements_file.write_text(
                "\n".join(dependency_plan.overlay_requirements) + "\n",
                encoding="utf-8",
            )
            overlay_command = tuple(
                build_offline_overlay_pip_command(
                    base / "runtime" / "python" / "python.exe",
                    requirements_file,
                    base / "app" / "updater_runtime",
                    overlay,
                    base_dir=base,
                )
            )
        else:
            status = "ready"
        metadata = {
            "repository_url": release.repository_url,
            "tag_name": release.tag_name,
            "version": release.version,
            "release_id": release.release_id,
            "zipball_url": release.zipball_url,
            "archive_sha256": download.sha256,
            "archive_size": download.bytes_downloaded,
            "current_version": current_version,
            "immutable": release.source == 'github',
            "source": release.source,
            "source_git": release.source_git,
            "commit": release.commit,
        }
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(overlay, ignore_errors=True)
        warning = _controlled_unlink(
            requirements_file,
            label="ComfyUI overlay requirements 临时文件",
        )
        if warning:
            cleanup_warnings.append(warning)
        for artifact in (archive, archive_partial):
            warning = _cleanup_owned_archive(
                base,
                artifact,
                operation_id=operation,
            )
            if warning:
                cleanup_warnings.append(warning)
        _attach_cleanup_notes(exc, cleanup_warnings)
        raise

    for artifact in (archive, archive_partial):
        warning = _cleanup_owned_archive(
            base,
            artifact,
            operation_id=operation,
        )
        if warning:
            cleanup_warnings.append(warning)
    return PreparedUpdate(
        status=status,
        base_dir=base,
        staging_core=staging,
        dependency_overlay=dependency_overlay,
        overlay_requirements_file=overlay_requirements_file,
        overlay_command=overlay_command,
        release_metadata=metadata,
        dependency_plan=dependency_plan,
        cleanup_warnings=tuple(cleanup_warnings),
    )
