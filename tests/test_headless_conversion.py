"""Real headless smoke test is opt-in; the fixture never runs ComfyUI inference."""
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.core import comfyui_conversion as conversion
from app.core import headless_conversion as headless


class HeadlessLifecycleTests(unittest.TestCase):
    def test_context_is_closed_on_timeout(self):
        cleaned = []
        @contextmanager
        def launch(*args, **kwargs):
            try:
                yield mock.Mock(poll=lambda: None)
            finally:
                cleaned.append(True)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps([
            '/extensions/lingjing_native_conversion/lingjing_conversion.js']).encode()
        with mock.patch.object(conversion, 'urlopen', return_value=response), \
             mock.patch.object(conversion, 'background_browser', launch), \
             self.assertRaisesRegex(ValueError, '超时'):
            conversion.convert_with_comfyui({}, 'http://127.0.0.1:8188', timeout=0)
        self.assertEqual(cleaned, [True])

    @unittest.skipUnless(os.environ.get('LINGJING_TEST_HEADLESS') == '1', 'opt-in isolated browser smoke')
    def test_real_headless_blank_startup_progress_and_return(self):
        script = (Path(__file__).parents[1] / 'app/comfy_bridge/web/lingjing_conversion.js').read_bytes()
        app_script = b'''export const app = {
          graph: { _nodes: [] },
          registerExtension(ext) { setTimeout(() => ext.setup(), 0); },
          async loadGraphData(workflow) { this.workflow=workflow; return true; },
          async graphToPrompt() { return {output: {'1': {class_type: 'Fixture', inputs: {text: this.workflow.nodes[0].widgets_values[0]}}}}; },
          queuePrompt() { throw new Error('must not generate'); }
        };'''
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                routes = {
                    '/extensions': ('application/json', b'["/extensions/lingjing_native_conversion/lingjing_conversion.js"]'),
                    '/': ('text/html', b'<script type="module" src="/extensions/lingjing_native_conversion/lingjing_conversion.js"></script>'),
                    '/scripts/app.js': ('text/javascript', app_script),
                    '/extensions/lingjing_native_conversion/lingjing_conversion.js': ('text/javascript', script),
                }
                kind, body = routes.get(self.path, ('text/plain', b''))
                self.send_response(200 if self.path in routes else 404)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                stages = []
                graph = conversion.convert_with_comfyui(
                    {'nodes': [{'widgets_values': ['blank-startup']}], 'links': []},
                    f'http://127.0.0.1:{server.server_port}', temporary_root=folder,
                    progress=stages.append, timeout=40)
                self.assertEqual(graph['1']['inputs']['text'], 'blank-startup')
                self.assertEqual(stages, ['starting', 'loading', 'serializing', 'converted'])
                self.assertEqual(list(Path(folder).iterdir()), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
