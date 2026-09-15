"""Domestic hosted ComfyUI repositories, independent of GitHub proxy services."""
import hashlib
import re
import tempfile
from pathlib import Path

DOMESTIC_GIT_CHANNELS = (
    ('GitCode 国内镜像', 'https://gitcode.com/gh_mirrors/co/ComfyUI.git'),
    ('Gitee 国内镜像', 'https://gitee.com/mirrors/comfyui.git'),
)
GIT_CHANNELS = (('GitHub 官方源', 'https://github.com/Comfy-Org/ComfyUI.git'), *DOMESTIC_GIT_CHANNELS)
_TAG = re.compile(r'([0-9a-f]{40})\s+refs/tags/(v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))(\^\{\})?')


def mirror_releases(core, endpoint):
    from app.core.comfyui_git_update import _git
    from app.core.comfyui_update import ValidatedRelease, ComfyUIUpdateError
    if endpoint not in {url for _, url in DOMESTIC_GIT_CHANNELS}:
        raise ComfyUIUpdateError('未授权的国内镜像地址')
    refs = _git(core, 'ls-remote', '--tags', endpoint, timeout=25).stdout
    tags, peeled = {}, {}
    for line in refs.splitlines():
        match = _TAG.fullmatch(line.strip())
        if match:
            commit, tag, major, minor, patch, suffix = match.groups()
            (peeled if suffix else tags)[tag] = (commit, (int(major), int(minor), int(patch)))
    releases = []
    for tag, (commit, version) in tags.items():
        commit = peeled.get(tag, (commit,))[0]
        releases.append(ValidatedRelease(
            release_id=0, tag_name=tag, version='.'.join(map(str, version)),
            repository_url=endpoint.removesuffix('.git'), api_url='', html_url=endpoint.removesuffix('.git'),
            zipball_url='', source='domestic', source_git=endpoint, commit=commit))
    return sorted(releases, key=lambda release: tuple(map(int, release.version.split('.'))), reverse=True)


def find_domestic_release(*, tag=None, channel_callback=None):
    from app.core.comfyui_update import ComfyUIUpdateError
    errors = []
    with tempfile.TemporaryDirectory(prefix='LingJingAPI-mirror-query-') as folder:
        for name, endpoint in DOMESTIC_GIT_CHANNELS:
            if channel_callback:
                channel_callback(f'查询镜像版本：{name}（{endpoint}）')
            try:
                candidates = mirror_releases(Path(folder), endpoint)
                release = next((item for item in candidates if tag is None or item.tag_name == tag), None)
                if release is None:
                    raise ComfyUIUpdateError(f'镜像尚未同步版本 {tag}' if tag else '镜像没有可用的版本标签')
                return release
            except ComfyUIUpdateError as exc:
                errors.append(f'{name}（{endpoint}）：{exc}')
    raise ComfyUIUpdateError('国内镜像版本查询失败；' + '；'.join(errors))


def download_domestic_archive(release, target, *, policy=None, progress_callback=None, channel_callback=None):
    """Export the pinned mirror commit into the existing safe archive pipeline."""
    from app.core.comfyui_git_update import _git
    from app.core.comfyui_update import ComfyUIUpdateError, DownloadValidationError, DownloadResult, load_update_policy, read_current_comfyui_version
    policy = policy or load_update_policy()
    target = Path(target)
    partial = Path(str(target) + '.part')
    if target.exists() or partial.exists():
        raise DownloadValidationError('ComfyUI 下载目标已存在')
    pinned = release if release.source == 'domestic' else find_domestic_release(tag=release.tag_name, channel_callback=channel_callback)
    if pinned.source_git not in {url for _, url in DOMESTIC_GIT_CHANNELS} or not re.fullmatch(r'[0-9a-f]{40}', pinned.commit):
        raise DownloadValidationError('国内镜像提交信息无效')
    target.parent.mkdir(parents=True, exist_ok=True)
    channels = sorted(DOMESTIC_GIT_CHANNELS, key=lambda item: item[1] != pinned.source_git)
    failures = []
    try:
        with tempfile.TemporaryDirectory(prefix='LingJingAPI-mirror-core-') as folder:
            core = Path(folder)
            _git(core, 'init')
            for name, endpoint in channels:
                if channel_callback:
                    channel_callback(f'获取 {pinned.tag_name}：{name}（{endpoint}）')
                try:
                    _git(core, 'fetch', '--depth=1', '--no-tags', endpoint, f'refs/tags/{pinned.tag_name}', timeout=60)
                    break
                except ComfyUIUpdateError as exc:
                    failures.append(f'{name}（{endpoint}）：{exc}')
            else:
                raise ComfyUIUpdateError('国内镜像下载失败；' + '；'.join(failures))
            commit = _git(core, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()
            if commit != pinned.commit:
                raise DownloadValidationError('镜像标签对应提交发生变化，已停止更新，请重新检查版本')
            version_text = _git(core, 'show', f'{commit}:comfyui_version.py').stdout
            if read_current_comfyui_version(core, read_text=lambda _path: version_text) != release.version:
                raise DownloadValidationError('镜像核心版本与目标版本不一致')
            if progress_callback:
                progress_callback(0, None)
            _git(core, 'archive', '--format=zip', '--prefix=ComfyUI/', '--output=' + str(partial.resolve()), commit, timeout=60)
        size = partial.stat().st_size
        if size <= 0 or size > int(policy['max_archive_bytes']):
            raise DownloadValidationError('镜像核心压缩包超过大小限制')
        with partial.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        partial.replace(target)
        if progress_callback:
            progress_callback(size, size)
        return DownloadResult(target, digest, size, size, pinned.source_git)
    finally:
        partial.unlink(missing_ok=True)
