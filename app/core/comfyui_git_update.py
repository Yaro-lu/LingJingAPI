"""Fast-forward official Git installations without replacing user directories."""
import os
import re
import subprocess
from pathlib import Path
from app.core.comfyui_update_channels import GIT_CHANNELS, DOMESTIC_GIT_CHANNELS
from app.core.process_supervisor import ProcessSupervisor
from app.core.comfyui_git_dependencies import inspect_environment, dependency_transaction
from app.core.python_import_paths import ensure_portable_import_order

from app.core.comfyui_update import (
    ComfyUIUpdateError, fetch_latest_release, plan_dependency_overlay,
    read_current_comfyui_version,
)


def _git(core, *args, timeout=30, check=True):
    try:
        result = ProcessSupervisor(Path(core)).run('comfyui-git',
            ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'http.followRedirects=false', '-C', str(core), *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except FileNotFoundError as exc:
        raise ComfyUIUpdateError('未找到 Git，请安装 Git 后重试此 Git 版本的更新') from exc
    except subprocess.TimeoutExpired as exc:
        endpoint = next((arg for arg in args if str(arg).startswith('https://')), '')
        raise ComfyUIUpdateError(f'Git {args[0]} 超时（{timeout} 秒） {endpoint}') from exc
    if check and result.returncode:
        raise ComfyUIUpdateError(f'Git 更新停止：{(result.stderr or result.stdout).strip()[-1500:]}')
    return result


def check_git_installation(core):
    core = Path(core).resolve(strict=True)
    if Path(_git(core, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != core:
        raise ComfyUIUpdateError('ComfyUI 不是独立 Git 工作目录，已停止更新')
    remote = _git(core, 'remote', 'get-url', 'origin').stdout.strip()
    if not re.fullmatch(r'(?:https://github\.com/|git@github\.com:)(?:Comfy-Org|comfyanonymous)/ComfyUI(?:\.git)?/?', remote, re.I):
        raise ComfyUIUpdateError('这个 Git 目录不是官方 ComfyUI origin，请自行管理该分支')
    if _git(core, 'status', '--porcelain', '--untracked-files=no').stdout.strip():
        raise ComfyUIUpdateError('ComfyUI 有未提交修改，请先提交或自行备份处理；客户端不会覆盖修改')
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        path = Path(_git(core, 'rev-parse', '--git-path', marker).stdout.strip())
        if not path.is_absolute():
            path = core / path
        if path.exists():
            raise ComfyUIUpdateError('ComfyUI 正在合并或变基，请先完成该 Git 操作')
    return _git(core, 'rev-parse', 'HEAD').stdout.strip()


def update_git_comfyui(core, *, release=None, channel_callback=None, python_executable=None):
    if python_executable:
        ensure_portable_import_order(python_executable)
    core = Path(core).resolve(strict=True)
    original = check_git_installation(core)
    release = release or fetch_latest_release(channel_callback=channel_callback)
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', release.tag_name):
        raise ComfyUIUpdateError('官方版本标签格式无效')
    current = read_current_comfyui_version(core)
    if tuple(map(int, current.split('.'))) >= tuple(map(int, release.version.split('.'))):
        return {'status': 'up_to_date', 'version': current, 'source': getattr(release, 'source', 'github')}
    # Fetch into FETCH_HEAD only; never overwrite a user's tag or change origin.
    failures = []
    channels = GIT_CHANNELS
    if getattr(release, 'source', 'github') == 'domestic':
        channels = sorted(DOMESTIC_GIT_CHANNELS, key=lambda item: item[1] != release.source_git)
    for name, endpoint in channels:
        if channel_callback:
            channel_callback(f'获取 {release.tag_name}：{name}（{endpoint}）')
        try:
            _git(core, 'fetch', '--no-tags', endpoint,
                 f'refs/tags/{release.tag_name}', timeout=60)
            break
        except ComfyUIUpdateError as exc:
            failures.append(f'{name}（{endpoint}）：{exc}')
    else:
        raise ComfyUIUpdateError('Git 获取稳定版失败；' + '；'.join(failures))
    target = _git(core, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()
    if getattr(release, 'commit', '') and target != release.commit:
        raise ComfyUIUpdateError('镜像标签对应提交发生变化，已停止更新，请重新检查版本')
    target_version = _git(core, 'show', f'{target}:comfyui_version.py').stdout
    if read_current_comfyui_version(core, read_text=lambda _path: target_version) != release.version:
        raise ComfyUIUpdateError('获取到的核心版本与稳定版信息不一致，已停止更新')
    if _git(core, 'merge-base', '--is-ancestor', original, target, check=False).returncode:
        raise ComfyUIUpdateError('当前分支与官方稳定版不能快进合并，已保留现有分支；请手动处理分支差异')
    requirements = _git(core, 'show', f'{target}:requirements.txt').stdout
    old = core / 'requirements.txt'
    environment = inspect_environment(python_executable) if python_executable else {'versions': {}}
    plan = plan_dependency_overlay(old, Path('target-requirements.txt'),
        read_text=lambda path: old.read_text(encoding='utf-8-sig') if path == old else requirements,
        installed_versions=environment['versions'])
    if plan.full_environment_required:
        details = '; '.join((*plan.reasons, *plan.overlay_requirements))
        raise ComfyUIUpdateError('新版需要配套更新 Python 依赖，请使用匹配的完整环境包，或自行更新 Git 环境；核心尚未修改。' + details[:800])
    if plan.overlay_requirements and not python_executable:
        raise ComfyUIUpdateError('更新配套组件需要指定实际运行 ComfyUI 的 Python')
    # Do not allow a repository update to replace persistent user directories.
    touched = _git(core, 'diff', '--name-only', '-z', original, target).stdout.split('\0')
    protected = {'models', 'input', 'output', 'outputs', 'user', 'custom_nodes', 'temp', 'logs'}
    if any(name and name.split('/')[0].lower() in protected for name in touched):
        raise ComfyUIUpdateError('新版涉及模型、用户数据或自定义节点目录，请手动检查后更新；当前核心未修改')
    if check_git_installation(core) != original:
        raise ComfyUIUpdateError('准备更新期间 Git 状态已改变，请重试')
    with dependency_transaction(python_executable, plan.overlay_requirements, environment,
                                channel_callback or (lambda _text: None)):
        if check_git_installation(core) != original:
            raise ComfyUIUpdateError('准备组件期间 Git 状态发生变化，已停止更新')
        try:
            _git(core, 'merge', '--ff-only', '--no-edit', target, timeout=120)
        except ComfyUIUpdateError:
            # A process timeout can happen after the merge has committed.
            # Keep matching dependencies if HEAD already reached the target.
            if _git(core, 'rev-parse', 'HEAD').stdout.strip() != target:
                raise
    return {'status': 'updated', 'version': release.version, 'previous_commit': original,
            'commit': target, 'source': getattr(release, 'source', 'github')}
