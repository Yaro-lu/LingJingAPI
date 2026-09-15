"""An isolated, exit-managed browser for native ComfyUI serialization only."""
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.core.process_supervisor import ProcessSupervisor


def find_browser():
    candidates = []
    for root in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES'),
                 os.environ.get('LOCALAPPDATA')):
        if root:
            candidates.extend(Path(root) / relative for relative in (
                'Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError('后台转换需要 Microsoft Edge 或 Google Chrome，请安装后重试，或导入 API 格式工作流')


@contextmanager
def background_browser(url, *, temporary_root=None):
    browser = find_browser()
    # The caller can select its installed runtime directory. Codex verification
    # supplies an explicit project tmp; never reuse a personal browser profile.
    root = Path(temporary_root) if temporary_root else Path(tempfile.gettempdir()) / 'LingJingAPI-conversion'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='conversion-', dir=root, ignore_cleanup_errors=True) as folder:
        profile = Path(folder)
        supervisor = ProcessSupervisor(profile)
        with (profile / 'browser.log').open('wb') as log:
            try:
                process = supervisor.launch('workflow-conversion', [
                    str(browser), '--headless=new', '--remote-debugging-port=0',
                    '--remote-debugging-address=127.0.0.1',
                    '--user-data-dir=' + str(profile / 'profile'),
                    '--disk-cache-dir=' + str(profile / 'cache'),
                    '--no-first-run', '--no-default-browser-check',
                    '--disable-background-networking', '--disable-component-update',
                    '--disable-sync', url,
                ], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env={**os.environ, 'TEMP': folder, 'TMP': folder})
                yield process
            finally:
                error = supervisor.terminate('workflow-conversion', timeout=5)
                if error:
                    raise ValueError('后台转换进程清理失败：' + error)
