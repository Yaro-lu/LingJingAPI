import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest import mock

from app.core import comfyui_conversion as module


class NativeConversionTests(unittest.TestCase):
    def extensions(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps([f'/extensions/{module.BRIDGE_NAME}/lingjing_conversion.js']).encode()
        return mock.patch.object(module, 'urlopen', return_value=response)

    def test_real_loopback_result_requires_token_and_returns_valid_graph(self):
        workflow = {'nodes': [{'id': 1, 'type': 'CustomWidget'}], 'links': []}
        output = {'1': {'class_type': 'CustomWidget', 'inputs': {'text': 'correct'}}}
        def browser(url):
            values = parse_qs(urlsplit(url).fragment)
            base = 'http://127.0.0.1:' + values['lingjing_port'][0]
            headers = {'Authorization': 'Bearer ' + values['lingjing_token'][0], 'Origin': 'http://127.0.0.1:8188'}
            with self.assertRaises(HTTPError) as exc:
                urlopen(base + '/workflow')
            self.assertEqual(exc.exception.code, 403)
            with urlopen(Request(base + '/workflow', headers=headers)) as response:
                self.assertEqual(json.load(response), workflow)
            wrong_origin = {**headers, 'Origin': 'https://example.invalid'}
            with self.assertRaises(HTTPError):
                urlopen(Request(base + '/workflow', headers=wrong_origin))
            with urlopen(Request(base + '/result', data=json.dumps({'output': output}).encode(), headers=headers)) as response:
                self.assertEqual(response.status, 200)
            return True
        with self.extensions():
            result = module.convert_with_comfyui(workflow, 'http://127.0.0.1:8188', launch_browser=browser)
        self.assertEqual(result, output)

    def test_native_error_is_reported_without_accepting_partial_graph(self):
        def browser(url):
            values = parse_qs(urlsplit(url).fragment)
            base = 'http://127.0.0.1:' + values['lingjing_port'][0]
            headers = {'Authorization': 'Bearer ' + values['lingjing_token'][0]}
            with self.assertRaises(HTTPError):
                urlopen(Request(base + '/result', data=b'{"error":"missing node"}', headers=headers))
        with self.extensions(), self.assertRaisesRegex(ValueError, 'missing node'):
            module.convert_with_comfyui({}, 'http://127.0.0.1:8188', launch_browser=browser)

    def test_timeout_cancellation_and_missing_extension_are_actionable(self):
        with self.extensions(), self.assertRaisesRegex(ValueError, '超时'):
            module.convert_with_comfyui({}, 'http://127.0.0.1:8188', launch_browser=lambda _: True, timeout=0)
        with self.extensions(), self.assertRaisesRegex(ValueError, '取消'):
            module.convert_with_comfyui({}, 'http://127.0.0.1:8188', launch_browser=lambda _: True, cancelled=lambda: True)
        with self.extensions() as call:
            call.return_value.read.return_value = b'[]'
            with self.assertRaisesRegex(ValueError, '重新启动'):
                module.convert_with_comfyui({}, 'http://127.0.0.1:8188', launch_browser=mock.Mock())

    def test_installer_is_repeatable_and_preserves_other_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            core = Path(temporary)
            (core / 'main.py').write_text('')
            target = module.install_frontend_bridge(core)
            other = target.parent / 'user.py'
            other.write_text('original')
            module.install_frontend_bridge(core)
            self.assertEqual(other.read_text(), 'original')
            self.assertIn('graphToPrompt', (target / 'web/lingjing_conversion.js').read_text(encoding='utf-8'))
            (target / '.lingjing-owned').unlink()
            with self.assertRaisesRegex(ValueError, '占用'):
                module.install_frontend_bridge(core)

    def test_gateway_uses_native_only_after_rule_conversion_fails(self):
        from app.gui import main_gateway
        app = object.__new__(main_gateway.GatewayApp)
        app._shutting_down = False
        output = {'1': {'class_type': 'Custom', 'inputs': {}}}
        with mock.patch.object(main_gateway, 'install_frontend_bridge'), mock.patch.object(main_gateway, 'convert_with_comfyui', return_value=output) as convert:
            self.assertEqual(app._convert_front_workflow_to_api({'nodes': [{'id': 1, 'type': 'Custom', 'widgets_values': ['value']}], 'links': []}), output)
            convert.assert_called_once()
            convert.reset_mock()
            app._convert_front_workflow_to_api({'nodes': [{'id': 1, 'type': 'CLIPTextEncode', 'widgets_values': ['value']}], 'links': []})
            convert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
