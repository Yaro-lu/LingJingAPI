import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.gui import main_gateway as gui
from app.core.model_source_discovery import load_saved_source


class InlineThread:
    def __init__(self, target, **kwargs): self.target = target
    def start(self): self.target()


class SourceUiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.app = object.__new__(gui.GatewayApp)
        self.app.after = lambda _, callback: callback()
        self.app._model_download_view_exists = lambda _: True
        self.app._set_model_download_status = mock.Mock()
        self.app._refresh_model_download_views = mock.Mock()
        self.app._start_model_download = mock.Mock()
        self.control = {'state': 'idle', 'target': self.base/'model.safetensors',
                        'item': {'path': 'vae/model.safetensors'},
                        'show_source_choices': mock.Mock(), 'set_sources': mock.Mock()}
        self.source = {'url': 'https://huggingface.co/author/repo/resolve/'+'a'*40+'/model.safetensors',
                       'size_bytes': 123, 'sha256': 'b'*64, 'repo': 'author/repo'}
        self.enterContext(mock.patch.object(gui, 'BASE_DIR', self.base))
        self.enterContext(mock.patch.object(gui.threading, 'Thread', InlineThread))

    def test_unique_source_is_cached_and_starts_download(self):
        with mock.patch.object(gui, 'discover_model_sources', return_value=[self.source]):
            self.app._resolve_model_source(self.control)
        self.app._start_model_download.assert_called_once_with(self.control)
        saved = load_saved_source(self.base/'runtime/model_sources.json', 'vae/model.safetensors')
        self.assertEqual(saved['sha256'], self.source['sha256'])
        self.assertEqual(len(self.control['urls']), 2)

    def test_different_versions_require_choice_then_use_selected_hash(self):
        other = {**self.source, 'sha256': 'c'*64, 'repo': 'other/repo'}
        with mock.patch.object(gui, 'discover_model_sources', return_value=[self.source, other]):
            self.app._resolve_model_source(self.control)
        self.app._start_model_download.assert_not_called()
        candidates, selected = self.control['show_source_choices'].call_args.args
        self.assertEqual(len(candidates), 2)
        selected(other)
        self.assertEqual(self.control['item']['sha256'], 'c'*64)
        self.app._start_model_download.assert_called_once()

    def test_declared_address_is_verified_before_index_search(self):
        self.control['item']['url'] = self.source['url']
        with mock.patch.object(gui, 'discover_model_sources') as search, mock.patch.object(gui, 'verify_hf_model_url', return_value=self.source) as verify:
            self.app._resolve_model_source(self.control)
        search.assert_not_called()
        verify.assert_called_once_with(self.source['url'], 'model.safetensors')

    def test_unknown_model_offers_manual_input_and_no_download_until_verified(self):
        with mock.patch.object(gui, 'discover_model_sources', return_value=[]):
            self.app._resolve_model_source(self.control)
        self.app._start_model_download.assert_not_called()
        _, selected = self.control['show_source_choices'].call_args.args
        with mock.patch.object(gui, 'verify_hf_model_url', side_effect=ValueError('wrong file')):
            selected('https://huggingface.co/a/b/resolve/main/wrong.safetensors')
        self.app._start_model_download.assert_not_called()
        with mock.patch.object(gui, 'verify_hf_model_url', return_value=self.source):
            selected(self.source['url'])
        self.app._start_model_download.assert_called_once()

    def test_close_before_resolution_completes_never_starts_download(self):
        self.app._model_download_view_exists = lambda _: False
        with mock.patch.object(gui, 'discover_model_sources', return_value=[self.source]):
            self.app._resolve_model_source(self.control)
        self.app._start_model_download.assert_not_called()

    def test_manual_address_entry_does_not_wait_for_model_index(self):
        with mock.patch.object(gui, 'discover_model_sources') as search:
            self.app._resolve_model_source(self.control, manual=True)
        search.assert_not_called()
        self.control['show_source_choices'].assert_called_once()
        self.app._start_model_download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
