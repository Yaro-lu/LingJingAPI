"""One-use loopback bridge to ComfyUI's actual frontend graphToPrompt."""
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from app.core.workflow_adaptation import validate_graph
from app.core.headless_conversion import background_browser
from contextlib import ExitStack

BRIDGE_NAME = 'lingjing_native_conversion'
MAX_BYTES = 64 * 1024 * 1024


def install_frontend_bridge(core):
    """Install only the client-owned extension; never touch third-party nodes."""
    core = Path(core)
    if not (core / 'main.py').is_file():
        raise ValueError('请先安装 ComfyUI 运行环境')
    target = core / 'custom_nodes' / BRIDGE_NAME
    for path in (core, target.parent, target, target / 'web'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('原生转换扩展目录不能是链接')
    marker = target / '.lingjing-owned'
    if target.exists() and (not marker.is_file() or marker.read_text(encoding='utf-8') != BRIDGE_NAME):
        raise ValueError('原生转换扩展目录已被其他内容占用，请检查 custom_nodes/' + BRIDGE_NAME)
    source = Path(__file__).resolve().parents[1] / 'comfy_bridge'
    contents = {'__init__.py': (source / '__init__.py').read_bytes(),
                'web/lingjing_conversion.js': (source / 'web/lingjing_conversion.js').read_bytes(),
                '.lingjing-owned': BRIDGE_NAME.encode()}
    target.mkdir(parents=True, exist_ok=True)
    # Mark ownership first so an interrupted first install can be resumed.
    if not marker.exists():
        marker.write_text(BRIDGE_NAME, encoding='utf-8')
    for relative, data in contents.items():
        destination = target / relative
        if destination.is_symlink():
            raise ValueError('原生转换扩展文件不能是链接')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and destination.read_bytes() == data:
            continue
        temporary = destination.with_name(destination.name + '.' + secrets.token_hex(8) + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def convert_with_comfyui(workflow, comfy_url, *, launch_browser=None, temporary_root=None,
                        progress=lambda stage: None, cancelled=lambda: False, timeout=180):
    parts = urlsplit(comfy_url)
    if parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost'} or parts.username or parts.password:
        raise ValueError('原生转换目前需要本机 HTTP ComfyUI 地址')
    origin = f'{parts.scheme}://{parts.netloc}'
    try:
        with urlopen(comfy_url.rstrip('/') + '/extensions', timeout=3) as response:
            extensions = json.loads(response.read(4 * 1024 * 1024))
    except Exception as exc:
        raise ValueError('请先启动本机 ComfyUI，再导入普通工作流') from exc
    if not isinstance(extensions, list) or not any(f'/{BRIDGE_NAME}/' in str(item) for item in extensions):
        raise ValueError('已准备原生转换扩展，请在客户端停止并重新启动 ComfyUI，然后重新导入')
    encoded = json.dumps(workflow, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_BYTES:
        raise ValueError('工作流超过原生转换大小上限')
    token, done, result = secrets.token_urlsafe(32), threading.Event(), {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, status, data=b''):
            self.send_response(status)
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, auth=True):
            expected_host = f'127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') == expected_host
                    and self.headers.get('Origin', origin) == origin
                    and (not auth or secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)))

        def do_OPTIONS(self):
            self.reply(204 if self.allowed(False) else 403)

        def do_GET(self):
            if not self.allowed():
                self.reply(403)
            elif self.path != '/workflow' or done.is_set():
                self.reply(404)
            else:
                self.reply(200, encoded)

        def do_POST(self):
            if not self.allowed():
                self.reply(403)
                return
            if self.path not in {'/result', '/progress'} or done.is_set():
                self.reply(404)
                return
            if self.path == '/progress':
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024:
                        raise ValueError('进度消息大小无效')
                    payload = json.loads(self.rfile.read(length))
                    stage = payload.get('stage')
                    if stage not in {'loading', 'serializing'}:
                        raise ValueError('未知转换阶段')
                    progress(stage)
                    self.reply(200, b'{}')
                except (ValueError, TypeError, AttributeError):
                    self.reply(422, b'{}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BYTES:
                    raise ValueError('转换结果大小无效')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError('转换结果格式无效')
                if payload.get('error'):
                    raise ValueError(str(payload['error'])[:1500])
                graph = validate_graph(payload.get('output'))
                result['graph'] = graph
                self.reply(200, b'{}')
            except (ValueError, TypeError) as exc:
                result['error'] = str(exc)
                self.reply(422, b'{}')
            finally:
                done.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = ExitStack()
    try:
        url = comfy_url.rstrip('/') + f'/#lingjing_port={server.server_port}&lingjing_token={token}'
        if cancelled():
            raise ValueError('客户端正在退出，已取消转换')
        progress('starting')
        process = None
        if launch_browser is not None:
            # Injectable transport for bridge tests; production owns a headless job.
            launch_browser(url)
        else:
            process = session.enter_context(background_browser(url, temporary_root=temporary_root))
        deadline = time.monotonic() + timeout
        while not done.wait(0.2):
            if cancelled():
                raise ValueError('客户端正在退出，已取消转换')
            if process is not None and process.poll() is not None:
                raise ValueError('后台转换浏览器意外退出，请更新 Edge/Chrome 后重试，或导入 API 格式工作流')
            if time.monotonic() >= deadline:
                raise ValueError('ComfyUI 后台转换超时，请确认 ComfyUI 可用、所需节点已安装，或导入 API 格式工作流')
        if result.get('error'):
            raise ValueError('ComfyUI 原生转换失败：' + result['error'])
        progress('converted')
        return result['graph']
    finally:
        try:
            session.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
