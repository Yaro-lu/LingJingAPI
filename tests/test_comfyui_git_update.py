import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core import comfyui_git_update as module


class GitCoreUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote, self.core = self.root / 'remote', self.root / 'core'
        self.remote.mkdir()
        self.git(self.remote, 'init', '-b', 'main')
        self.git(self.remote, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.remote, 'config', 'user.name', 'Fixture')
        (self.remote / 'comfyui_version.py').write_text('__version__ = "0.0.1"\n')
        (self.remote / 'requirements.txt').write_text('torch\n')
        self.commit('first')
        self.git(self.remote, 'tag', 'v0.0.1')
        self.git(self.root, 'clone', str(self.remote), str(self.core))
        self.git(self.core, 'remote', 'set-url', 'origin', 'https://github.com/Comfy-Org/ComfyUI.git')
        (self.remote / 'comfyui_version.py').write_text('__version__ = "0.0.2"\n')
        self.commit('second')
        self.git(self.remote, 'tag', 'v0.0.2')
        self.release = SimpleNamespace(tag_name='v0.0.2', version='0.0.2')

    def git(self, path, *args):
        result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.git(self.remote, 'add', '.')
        self.git(self.remote, 'commit', '-m', message)

    def update(self):
        real_git = module._git
        def local_fetch(core, *args, **kwargs):
            if args[0] == 'fetch':
                args = ('fetch', '--no-tags', str(self.remote), 'refs/tags/v0.0.2')
            return real_git(core, *args, **kwargs)
        with mock.patch.object(module, '_git', side_effect=local_fetch):
            return module.update_git_comfyui(self.core, release=self.release)

    def test_fast_forward_preserves_branch_and_user_files(self):
        assets = self.core / 'custom_nodes/user_node.py'
        assets.parent.mkdir()
        assets.write_text('user node')
        result = self.update()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.git(self.core, 'branch', '--show-current'), 'main')
        self.assertEqual(assets.read_text(), 'user node')
        self.assertIn('0.0.2', (self.core / 'comfyui_version.py').read_text())

    def test_dirty_checkout_stops_before_fetch(self):
        (self.core / 'requirements.txt').write_text('torch\nuser-change\n')
        with self.assertRaisesRegex(module.ComfyUIUpdateError, '未提交修改'):
            self.update()
        self.assertIn('user-change', (self.core / 'requirements.txt').read_text())

    def test_fetch_timeout_falls_back_and_preserves_origin(self):
        real_git = module._git
        urls = []
        def local_fetch(core, *args, **kwargs):
            if args[0] == 'fetch':
                urls.append(args[2])
                if len(urls) == 1:
                    raise module.ComfyUIUpdateError('Git fetch timeout')
                args = ('fetch', '--no-tags', str(self.remote), 'refs/tags/v0.0.2')
            return real_git(core, *args, **kwargs)
        progress = mock.Mock()
        with mock.patch.object(module, '_git', side_effect=local_fetch):
            result = module.update_git_comfyui(self.core, release=self.release, channel_callback=progress)
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(urls, [channel[1] for channel in module.GIT_CHANNELS[:2]])
        self.assertEqual(self.git(self.core, 'remote', 'get-url', 'origin'), urls[0])
        self.assertEqual(progress.call_count, 2)

    def test_mismatched_version_from_transport_is_rejected(self):
        self.release.version = '0.0.3'
        with self.assertRaisesRegex(module.ComfyUIUpdateError, '版本.*不一致'):
            self.update()
        self.assertIn('0.0.1', (self.core / 'comfyui_version.py').read_text())

    def test_diverged_branch_stops_without_reset(self):
        self.git(self.core, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.core, 'config', 'user.name', 'Fixture')
        (self.core / 'my-fix.txt').write_text('mine')
        self.git(self.core, 'add', '.')
        self.git(self.core, 'commit', '-m', 'user branch')
        original = self.git(self.core, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(module.ComfyUIUpdateError, '快进'):
            self.update()
        self.assertEqual(self.git(self.core, 'rev-parse', 'HEAD'), original)

    def test_dependency_changes_stop_before_merge(self):
        (self.remote / 'requirements.txt').write_text('torch==2.99\n')
        self.commit('dependency update')
        self.git(self.remote, 'tag', '-f', 'v0.0.2')
        with self.assertRaisesRegex(module.ComfyUIUpdateError, '依赖'):
            self.update()
        self.assertIn('0.0.1', (self.core / 'comfyui_version.py').read_text())

    def test_allowed_components_use_actual_environment_and_transaction(self):
        (self.remote / 'requirements.txt').write_text('torch\nav>=17\ncomfyui-frontend-package==2.0.0\n')
        self.commit('components')
        self.git(self.remote, 'tag', '-f', 'v0.0.2')
        real_git = module._git
        def local_fetch(core, *args, **kwargs):
            if args[0] == 'fetch':
                args = ('fetch', '--no-tags', str(self.remote), 'refs/tags/v0.0.2')
            return real_git(core, *args, **kwargs)
        env = {'versions': {'av': '18.0.0'}, 'site': str(self.root)}
        with mock.patch.object(module, '_git', side_effect=local_fetch), \
             mock.patch.object(module, 'inspect_environment', return_value=env), \
             mock.patch.object(module, 'dependency_transaction') as transaction:
            result = module.update_git_comfyui(self.core, release=self.release, python_executable='runtime-python')
        self.assertEqual(result['status'], 'updated')
        transaction.assert_called_once_with('runtime-python', ('comfyui-frontend-package==2.0.0',), env, mock.ANY)
        transaction.return_value.__enter__.assert_called_once()
        transaction.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_nonofficial_origin_is_not_updated(self):
        self.git(self.core, 'remote', 'set-url', 'origin', 'https://example.invalid/fork.git')
        with self.assertRaisesRegex(module.ComfyUIUpdateError, '不是官方'):
            self.update()

    def test_latest_version_needs_no_fetch(self):
        release = SimpleNamespace(tag_name='v0.0.1', version='0.0.1')
        result = module.update_git_comfyui(self.core, release=release)
        self.assertEqual(result['status'], 'up_to_date')

    def test_gui_routes_git_update_without_using_archive_replacement(self):
        from app.gui import main_gateway
        app = object.__new__(main_gateway.GatewayApp)
        app._shutting_down = False
        app._last_health = {}
        app._reserve_runtime_maintenance = mock.Mock(return_value=(True, True, ''))
        app._quiesce_comfyui_for_update = mock.Mock(return_value='')
        app._stop_runtime_for_maintenance = mock.Mock(return_value='')
        app._end_runtime_maintenance = mock.Mock()
        app._post_to_ui = lambda callback: callback()
        app._close_maintenance_dialog = mock.Mock()
        app._set_comfyui_update_progress = mock.Mock()
        app._create_runtime_progress_dialog = mock.Mock(return_value={'popup': mock.Mock()})
        base = self.root / 'gui'
        core = base / 'runtime/ComfyUI'
        core.mkdir(parents=True)
        (core / '.git').mkdir()
        (core / 'comfyui_version.py').write_text('__version__ = "0.0.1"')
        with mock.patch.object(main_gateway, 'BASE_DIR', base), \
             mock.patch.object(main_gateway, 'update_git_comfyui', return_value={'status': 'updated', 'version': '0.0.2'}) as update, \
             mock.patch.object(main_gateway, 'prepare_comfyui_update') as archive, \
             mock.patch.object(main_gateway.threading.Thread, 'start', lambda thread: thread.run()), \
             mock.patch.object(main_gateway.messagebox, 'showinfo') as info:
            app._start_comfyui_update()
        update.assert_called_once_with(core, python_executable=mock.ANY, channel_callback=mock.ANY)
        archive.assert_not_called()
        info.assert_called_once()
        app._end_runtime_maintenance.assert_called_once_with(restart=True)


if __name__ == '__main__':
    unittest.main()
