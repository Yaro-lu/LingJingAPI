import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.gui import main_gateway as gui


class Response(io.BytesIO):
    def __init__(self, payload, status=200, **headers):
        super().__init__(payload)
        self.status = status
        self.headers = headers


class ModelDownloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name)/'model.safetensors'
        self.app = object.__new__(gui.GatewayApp)
        self.app.after = lambda _, f: f()
        self.app._finish_model_download = mock.Mock()
        self.app._fail_model_download = mock.Mock()
        self.app._set_model_download_status = mock.Mock()
        self.app._update_model_download_progress = mock.Mock()
        self.url = 'https://huggingface.co/org/repo/resolve/main/model.safetensors'
        self.digest = hashlib.sha256(b'model').hexdigest()
        self.control = {'target': self.target, 'url': self.url, 'item': {'size_bytes': 5, 'sha256': self.digest}}
        self.enterContext(mock.patch.object(gui.time, 'sleep'))

    def partial(self, payload=b'mo', digest=None):
        part = self.target.with_suffix('.safetensors.part')
        part.write_bytes(payload)
        part.with_suffix('.part.json').write_text(json.dumps({
            'url': self.url, 'expected_size': 5, 'expected_sha256': digest or self.digest, 'validator': '"old"'}))

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        self.partial()
        def open_request(request, **kwargs):
            self.assertEqual(request.get_header('Range'), 'bytes=2-')
            return Response(b'model', **{'Content-Length': '5', 'ETag': '"new"'})
        with mock.patch.object(gui, '_open_download_request', side_effect=open_request):
            self.app._download_model_file(self.control)
        self.assertEqual(self.target.read_bytes(), b'model')
        self.app._finish_model_download.assert_called_once()

    def test_hash_mismatch_never_promotes_file(self):
        with mock.patch.object(gui, '_open_download_request', return_value=Response(b'wrong', **{'Content-Length': '5'})):
            self.app._download_model_file(self.control)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix('.safetensors.part').exists())
        self.app._finish_model_download.assert_not_called()
        self.assertIn('SHA256', self.app._fail_model_download.call_args.args[1])

    def test_partial_from_different_identity_is_discarded_before_request(self):
        self.partial(digest='f'*64)
        def open_request(request, **kwargs):
            self.assertIsNone(request.get_header('Range'))
            return Response(b'model', **{'Content-Length': '5'})
        with mock.patch.object(gui, '_open_download_request', side_effect=open_request):
            self.app._download_model_file(self.control)
        self.assertEqual(self.target.read_bytes(), b'model')

    def test_invalid_range_is_rejected_then_restarted_safely(self):
        self.partial()
        calls = []
        def open_request(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                return Response(b'del', status=206, **{'Content-Length': '3', 'Content-Range': 'bytes 1-3/5'})
            self.assertIsNone(request.get_header('Range'))
            return Response(b'model', **{'Content-Length': '5'})
        with mock.patch.object(gui, '_open_download_request', side_effect=open_request):
            self.app._download_model_file(self.control)
        self.assertEqual(self.target.read_bytes(), b'model')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
