"""Stage audited ComfyUI wheels and roll back dependencies if Git activation fails."""
import importlib.metadata
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.core.comfyui_update import ComfyUIUpdateError, load_update_policy
from app.core.process_supervisor import ProcessSupervisor
from app.core.comfyui_update_worker import (
    _validate_dependency_overlay, _existing_distribution_entries, _overlay_distribution_name,
    _validate_dependency_entry,
)

PYPI_MIRROR = 'https://pypi.tuna.tsinghua.edu.cn/simple'
_INSPECT = """import importlib.metadata as m,json,sysconfig
from packaging.markers import default_environment
versions={}
for d in m.distributions():
 if d.metadata.get('Name'):
  versions.setdefault(d.metadata['Name'].lower().replace('_','-'), d.version)
print(json.dumps({'versions':versions,'site':sysconfig.get_path('purelib'),'markers':default_environment()}))"""


def inspect_environment(python):
    result = ProcessSupervisor(Path(python).parent).run('comfyui-dependency-inspect',
        [str(python), '-s', '-B', '-c', _INSPECT], capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30,
        env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1'})
    if result.returncode:
        raise ComfyUIUpdateError('无法检查 ComfyUI 实际 Python 依赖：' + result.stderr[-800:])
    try:
        data = json.loads(result.stdout)
        if not isinstance(data['versions'], dict) or not Path(data['site']).is_dir():
            raise ValueError('环境信息无效')
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise ComfyUIUpdateError('Python 依赖检查结果无效') from exc


def stage_wheels(python, requirements, stage, environment, progress):
    policy = load_update_policy()
    allowed = set(policy['overlay_packages'])
    prefixes = tuple(policy['overlay_transitive_prefixes'])
    pending, attempted = list(requirements), set()
    supervisor = ProcessSupervisor(stage)
    for _ in range(12):
        for raw in pending:
            req = Requirement(raw)
            name = canonicalize_name(req.name)
            specs = list(req.specifier)
            if (name not in allowed and not name.startswith(prefixes)) or req.url or req.extras or len(specs) != 1 or specs[0].operator != '==' or '*' in specs[0].version:
                raise ComfyUIUpdateError('配套依赖不是允许的固定版本：' + raw)
            if raw in attempted:
                raise ComfyUIUpdateError('配套依赖版本冲突：' + raw)
            attempted.add(raw)
        progress('从清华 PyPI 镜像准备配套组件：' + '、'.join(pending))
        result = supervisor.run('comfyui-dependency-download', [str(python), '-s', '-B', '-m', 'pip',
            '--isolated', 'install', '--index-url', PYPI_MIRROR, '--only-binary=:all:',
            '--no-deps', '--no-compile', '--no-cache-dir', '--disable-pip-version-check',
            '--no-input', '--target', str(stage), *pending], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600,
            env={**os.environ, 'PYTHONNOUSERSITE': '1', 'PYTHONUTF8': '1'})
        if result.returncode:
            raise ComfyUIUpdateError('国内镜像配套组件下载失败，尚未修改环境：' + (result.stderr or result.stdout)[-1500:])
        distributions = list(importlib.metadata.distributions(path=[str(stage)]))
        versions = {**environment['versions'], **{canonicalize_name(d.metadata['Name']): d.version for d in distributions}}
        pending = []
        for dist in distributions:
            for raw in dist.requires or []:
                req = Requirement(raw)
                if req.marker and not req.marker.evaluate({**environment['markers'], 'extra': ''}):
                    continue
                name = canonicalize_name(req.name)
                if not req.url and not req.extras and name in versions and req.specifier.contains(versions[name], prereleases=False):
                    continue
                if name not in allowed and not name.startswith(prefixes):
                    raise ComfyUIUpdateError(f'{dist.metadata["Name"]} 需要尚未满足的依赖 {raw}；环境尚未修改')
                pending.append(str(Requirement(raw.split(';', 1)[0])))
        pending = sorted(set(pending))
        if not pending:
            for raw in requirements:
                req = Requirement(raw)
                if not req.specifier.contains(versions.get(canonicalize_name(req.name), '0')):
                    raise ComfyUIUpdateError('下载组件版本校验失败：' + raw)
            return
    raise ComfyUIUpdateError('配套组件依赖链过深，环境尚未修改')


@contextmanager
def dependency_transaction(python, requirements, environment, progress=lambda text: None):
    if not requirements:
        yield
        return
    root = Path(tempfile.mkdtemp(prefix='LingJingAPI-dependencies-')).resolve()
    operation = uuid.uuid4().hex
    stage = root / ('.comfyui-update-overlay-' + operation)
    stage.mkdir()
    site = Path(environment['site']).resolve(strict=True)
    activation = None
    installed, saved, preserve = [], [], False
    try:
        stage_wheels(python, requirements, stage, environment, progress)
        _, entries = _validate_dependency_overlay(root, stage, operation_id=operation)
        # Incoming files and backups must share the destination volume, so each
        # activation/rollback move is atomic even when the download TEMP is on C:.
        activation = Path(tempfile.mkdtemp(prefix='.comfyui-update-dependencies-', dir=site.parent))
        incoming = activation / 'incoming'
        shutil.copytree(stage, incoming)
        entries = list(incoming.iterdir())
        backup = activation / 'backup'
        backup.mkdir()
        names = {entry.name for entry in entries}
        distributions = {_overlay_distribution_name(entry.name) for entry in entries} - {None}
        old = [entry for entry in _existing_distribution_entries(site, entries)
               if entry.name in names or _overlay_distribution_name(entry.name) in distributions]
        old = {entry.name: entry for entry in old}
        for entry in entries:
            existing = site / entry.name
            if existing.exists():
                _validate_dependency_entry(existing, label='旧版组件')
                old[entry.name] = existing
        progress('组件已准备，正在替换配套依赖并更新核心')
        (activation / 'recovery.json').write_text(json.dumps({'site': str(site), 'old': list(old), 'new': sorted(names)}, ensure_ascii=False), encoding='utf-8')
        for entry in old.values():
            destination = backup / entry.name
            os.replace(entry, destination)
            saved.append((destination, entry))
        for entry in entries:
            destination = site / entry.name
            os.replace(entry, destination)
            installed.append((destination, entry))
        yield
    except BaseException as exc:
        errors = []
        for source, destination in reversed(installed):
            try:
                os.replace(source, destination)
            except OSError as error:
                errors.append(str(error))
        for source, destination in reversed(saved):
            try:
                os.replace(source, destination)
            except OSError as error:
                errors.append(str(error))
        if errors:
            preserve = True
            raise ComfyUIUpdateError(f'更新失败且部分组件恢复失败，请保留备份 {activation}：' + '; '.join(errors)) from exc
        raise
    finally:
        if activation is not None and not preserve:
            shutil.rmtree(activation, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
