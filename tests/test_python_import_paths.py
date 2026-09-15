import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
from app.core.python_import_paths import ensure_portable_import_order


class ImportOrderTests(unittest.TestCase):
    def test_updated_packages_precede_legacy_and_repeated_call_is_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'python313._pth'
            config.write_text('python313.zip\n.\n../ComfyUI\n../../.venv/Lib/site-packages\nimport site\n')
            self.assertTrue(ensure_portable_import_order(root / 'python.exe'))
            lines = config.read_text().splitlines()
            self.assertLess(lines.index('Lib/site-packages'), lines.index('../../.venv/Lib/site-packages'))
            self.assertIn('../ComfyUI', lines)
            self.assertFalse(ensure_portable_import_order(root / 'python.exe'))

    def test_existing_late_local_path_is_moved_without_duplication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'python313._pth'
            config.write_text('.\n../../.venv/Lib/site-packages\nLib/site-packages\nimport site\n')
            ensure_portable_import_order(root / 'python.exe')
            lines = config.read_text().splitlines()
            self.assertEqual(lines.count('Lib/site-packages'), 1)
            self.assertLess(lines.index('Lib/site-packages'), lines.index('../../.venv/Lib/site-packages'))

    def test_standard_python_is_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(ensure_portable_import_order(Path(folder) / 'python.exe'))
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_inspector_uses_first_distribution_on_import_path(self):
        from app.core.comfyui_git_dependencies import _INSPECT
        distributions = [SimpleNamespace(metadata={'Name': 'comfy-kitchen'}, version='0.2.16'),
                         SimpleNamespace(metadata={'Name': 'comfy-kitchen'}, version='0.2.33')]
        output = io.StringIO()
        with mock.patch('importlib.metadata.distributions', return_value=distributions), redirect_stdout(output):
            exec(_INSPECT, {})
        self.assertEqual(json.loads(output.getvalue())['versions']['comfy-kitchen'], '0.2.16')
