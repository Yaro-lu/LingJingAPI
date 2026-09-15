"""Keep portable Python's own updated packages ahead of legacy environment paths."""
import os
import uuid
from pathlib import Path


def ensure_portable_import_order(python):
    configs = list(Path(python).resolve().parent.glob('python*._pth'))
    if len(configs) != 1:
        return False
    config = configs[0]
    if config.is_symlink():
        raise ValueError('便携 Python 路径配置不能是链接')
    original = config.read_text(encoding='utf-8-sig')
    lines = original.splitlines()
    normalized = lambda line: line.strip().replace('\\', '/').rstrip('/').casefold()
    own = 'lib/site-packages'
    lines = [line for line in lines if normalized(line) != own]
    index = next((i for i, line in enumerate(lines)
                  if not line.lstrip().startswith('#') and
                  ('site-packages' in normalized(line) or normalized(line) == 'import site')), len(lines))
    lines.insert(index, 'Lib/site-packages')
    updated = '\n'.join(lines) + '\n'
    if updated == original:
        return False
    temporary = config.with_name(config.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(updated)
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
    return True
