import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from app.core import comfyui_git_dependencies as dependencies
from app.core.comfyui_update import plan_dependency_overlay, ComfyUIUpdateError


class GitDependencyTests(unittest.TestCase):
    def plan(self, old, new, versions):
        return plan_dependency_overlay(Path('old'), Path('new'),
            read_text=lambda path: old if path.name == 'old' else new,
            installed_versions=versions)

    def test_installed_av_satisfies_new_requirement_without_reinstall(self):
        plan = self.plan('av>=16.0.0\ncomfyui-frontend-package==1.0.0',
                         'av>=17.0.0\ncomfyui-frontend-package==1.1.0', {'av': '18.0.0'})
        self.assertFalse(plan.full_environment_required)
        self.assertEqual(plan.overlay_requirements, ('comfyui-frontend-package==1.1.0',))

    def test_missing_or_old_av_is_still_reported(self):
        for versions in ({}, {'av': '16.0.0'}):
            self.assertTrue(self.plan('av>=16', 'av>=17', versions).full_environment_required)

    def test_gpu_requirement_change_is_not_bypassed(self):
        self.assertTrue(self.plan('torch>=2.8', 'torch>=2.9', {'torch': '2.9.1'}).full_environment_required)

    def stage(self, _python, _requirements, stage, _environment, _progress):
        self.package(stage, '2.0.0', 'new')

    def package(self, root, version, text):
        package = root / 'comfyui_frontend_package'
        package.mkdir()
        (package / '__init__.py').write_text(text)
        meta = root / f'comfyui_frontend_package-{version}.dist-info'
        meta.mkdir()
        (meta / 'METADATA').write_text(f'Name: comfyui-frontend-package\nVersion: {version}\n')
        (meta / 'RECORD').write_text('comfyui_frontend_package/__init__.py,,\n')

    def test_component_swap_is_rolled_back_when_core_update_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)
            self.package(site, '1.0.0', 'old')
            with mock.patch.object(dependencies, 'stage_wheels', self.stage):
                with self.assertRaisesRegex(RuntimeError, 'merge failed'):
                    with dependencies.dependency_transaction('python', ['comfyui-frontend-package==2.0.0'], {'site': str(site)}):
                        self.assertEqual((site / 'comfyui_frontend_package/__init__.py').read_text(), 'new')
                        raise RuntimeError('merge failed')
            self.assertEqual((site / 'comfyui_frontend_package/__init__.py').read_text(), 'old')
            self.assertTrue((site / 'comfyui_frontend_package-1.0.0.dist-info').is_dir())
            self.assertFalse((site / 'comfyui_frontend_package-2.0.0.dist-info').exists())

    def test_success_preserves_unrelated_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)
            self.package(site, '1.0.0', 'old')
            (site / 'torch').mkdir()
            (site / 'torch/unchanged').write_text('GPU')
            with mock.patch.object(dependencies, 'stage_wheels', self.stage):
                with dependencies.dependency_transaction('python', ['comfyui-frontend-package==2.0.0'], {'site': str(site)}):
                    pass
            self.assertEqual((site / 'comfyui_frontend_package/__init__.py').read_text(), 'new')
            self.assertFalse((site / 'comfyui_frontend_package-1.0.0.dist-info').exists())
            self.assertEqual((site / 'torch/unchanged').read_text(), 'GPU')

    def test_download_failure_never_modifies_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)
            self.package(site, '1.0.0', 'old')
            with mock.patch.object(dependencies, 'stage_wheels', side_effect=ComfyUIUpdateError('download failed')):
                with self.assertRaisesRegex(ComfyUIUpdateError, 'download failed'):
                    with dependencies.dependency_transaction('python', ['comfyui-frontend-package==2.0.0'], {'site': str(site)}):
                        self.fail('must not activate core')
            self.assertEqual((site / 'comfyui_frontend_package/__init__.py').read_text(), 'old')

    def test_component_requiring_new_torch_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            def download(*args, **kwargs):
                self.package(stage, '2.0.0', 'new')
                meta = stage / 'comfyui_frontend_package-2.0.0.dist-info/METADATA'
                meta.write_text(meta.read_text() + 'Requires-Dist: torch>=9.0\n')
                return SimpleNamespace(returncode=0)
            with mock.patch.object(dependencies.ProcessSupervisor, 'run', side_effect=download):
                with self.assertRaisesRegex(ComfyUIUpdateError, 'torch>=9.0'):
                    dependencies.stage_wheels('python', ['comfyui-frontend-package==2.0.0'], stage,
                        {'versions': {'torch': '2.9.1'}, 'markers': {}}, lambda text: None)
