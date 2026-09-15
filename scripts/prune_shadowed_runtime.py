"""Remove obsolete, fully shadowed dependency copies from an isolated stage."""
import argparse
import csv
import json
import re
import shutil
from email.parser import Parser
from pathlib import Path


def distributions(site):
    result = {}
    owners = {}
    for info in site.glob('*.dist-info'):
        metadata, record = info / 'METADATA', info / 'RECORD'
        if not metadata.is_file() or not record.is_file():
            continue
        name = Parser().parsestr(metadata.read_text(encoding='utf-8')).get('Name', '')
        name = re.sub(r'[-_.]+', '-', name).lower()
        if not name:
            continue
        tops = set()
        for row in csv.reader(record.read_text(encoding='utf-8').splitlines()):
            if not row:
                continue
            parts = row[0].replace('\\', '/').split('/')
            if not parts or parts[0] in {'', '.', '..'} or ':' in parts[0]:
                continue
            top = parts[0]
            if top.endswith('.dist-info'):
                continue
            tops.add(top)
            owners.setdefault(top, set()).add(name)
        result[name] = (info, tops)
    return result, owners


def prune(stage):
    stage = Path(stage).resolve(strict=True)
    own = stage / 'runtime/python/Lib/site-packages'
    legacy = stage / '.venv/Lib/site-packages'
    updated, _ = distributions(own)
    previous, owners = distributions(legacy)
    removed = []
    for name in sorted(updated.keys() & previous.keys()):
        info, old_tops = previous[name]
        _, new_tops = updated[name]
        # Namespace packages shared by distributions are left intact.
        removable = [top for top in old_tops if top in new_tops and owners.get(top) == {name}
                     and (own / top).exists()]
        for target in [legacy / top for top in removable] + ([info] if set(removable) == old_tops else []):
            resolved = target.resolve()
            if not resolved.is_relative_to(legacy.resolve()) or target.is_symlink():
                raise ValueError('Unsafe staged dependency path')
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()
            removed.append(str(target.relative_to(stage)).replace('\\', '/'))
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    removed = prune(args.stage)
    Path(args.report).write_text(json.dumps(removed, indent=2), encoding='utf-8')
    print(f'Removed {len(removed)} obsolete dependency entries from the isolated stage')
