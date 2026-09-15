import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core import comfyui_update as updater
from app.core import comfyui_update_channels as channels
from app.core import comfyui_git_update as git_updater
import test_comfyui_git_update as git_fixtures
from test_comfyui_update import Response


class DomesticChannelTests(unittest.TestCase):
    def release(self, **changes):
        values = dict(release_id=0, tag_name='v0.0.2', version='0.0.2',
            repository_url=channels.DOMESTIC_GIT_CHANNELS[0][1].removesuffix('.git'),
            api_url='', html_url='', zipball_url='', source='domestic',
            source_git=channels.DOMESTIC_GIT_CHANNELS[0][1], commit='a' * 40)
        values.update(changes)
        return updater.ValidatedRelease(**values)

    def test_tags_ignore_nightly_and_use_annotated_commit(self):
        refs = '\n'.join([
            'a'*40 + '\trefs/tags/v0.0.2', 'b'*40 + '\trefs/tags/v0.0.2^{}',
            'c'*40 + '\trefs/tags/v0.0.1', 'd'*40 + '\trefs/tags/v9.0.0-rc1'])
        with mock.patch.object(git_updater, '_git', return_value=SimpleNamespace(stdout=refs)):
            result = channels.mirror_releases(Path.cwd(), channels.DOMESTIC_GIT_CHANNELS[0][1])
        self.assertEqual([r.version for r in result], ['0.0.2', '0.0.1'])
        self.assertEqual(result[0].commit, 'b'*40)
        self.assertEqual(result[0].source, 'domestic')
        self.assertEqual(result[0].api_url, '')

    def test_official_query_failure_uses_domestic_repository(self):
        with mock.patch.object(updater, 'urlopen', side_effect=TimeoutError('timeout')) as http, \
             mock.patch.object(channels, 'find_domestic_release', return_value=self.release()) as domestic:
            result = updater.fetch_latest_release()
        http.assert_called_once()
        domestic.assert_called_once()
        self.assertEqual(result.source, 'domestic')

    def test_invalid_official_metadata_stops_without_using_mirror(self):
        with mock.patch.object(updater, 'urlopen', return_value=Response(b'{}', url=updater.CANONICAL_LATEST_RELEASE_URL)), \
             mock.patch.object(channels, 'find_domestic_release') as domestic:
            with self.assertRaises(updater.ReleaseValidationError):
                updater.fetch_latest_release()
        domestic.assert_not_called()

    def test_gitcode_failure_uses_gitee_and_never_github(self):
        with mock.patch.object(channels, 'mirror_releases', side_effect=[updater.ComfyUIUpdateError('offline'), [self.release()]]) as query:
            channels.find_domestic_release()
        self.assertEqual([call.args[1] for call in query.call_args_list],
                         [url for _, url in channels.DOMESTIC_GIT_CHANNELS])

    def test_missing_tag_does_not_silently_downgrade(self):
        with mock.patch.object(channels, 'mirror_releases', return_value=[self.release()]):
            with self.assertRaisesRegex(updater.ComfyUIUpdateError, '尚未同步版本 v0.0.3'):
                channels.find_domestic_release(tag='v0.0.3')

    def fixture(self):
        fixture = git_fixtures.GitCoreUpdateTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        commit = fixture.git(fixture.remote, 'rev-parse', 'v0.0.2')
        real_git = git_updater._git
        def local(core, *args, **kwargs):
            if args[0] == 'fetch':
                self.assertIn(args[-2], [url for _, url in channels.DOMESTIC_GIT_CHANNELS])
                args = (*args[:-2], str(fixture.remote), args[-1])
            return real_git(core, *args, **kwargs)
        return fixture, self.release(commit=commit), local

    def test_package_exports_pinned_domestic_commit_without_http(self):
        fixture, release, local = self.fixture()
        target = fixture.root / 'core.zip'
        with mock.patch.object(git_updater, '_git', side_effect=local), \
             mock.patch.object(updater, 'urlopen', side_effect=AssertionError('Domestic download must not use HTTP/GitHub')):
            result = updater.download_release_archive(release, target)
        with zipfile.ZipFile(target) as bundle:
            self.assertIn(b'0.0.2', bundle.read('ComfyUI/comfyui_version.py'))
            self.assertFalse(any('/.git/' in name for name in bundle.namelist()))
        self.assertEqual(result.final_url, release.source_git)
        self.assertFalse(Path(str(target) + '.part').exists())

    def test_changed_mirror_commit_is_rejected(self):
        fixture, release, local = self.fixture()
        target = fixture.root / 'bad.zip'
        with mock.patch.object(git_updater, '_git', side_effect=local):
            with self.assertRaisesRegex(updater.DownloadValidationError, '提交发生变化'):
                updater.download_release_archive(self.release(), target)
        self.assertFalse(target.exists())
        self.assertFalse(Path(str(target) + '.part').exists())

    def test_official_archive_stream_failure_cleans_partial_before_mirror(self):
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError('interrupted')
                return super().read(3)
        release = self.release(source='github', zipball_url='https://api.github.com/repos/Comfy-Org/ComfyUI/zipball/v0.0.2')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'core.zip'
            def fallback(*args, **kwargs):
                self.assertFalse(Path(str(target) + '.part').exists())
                return 'mirror-result'
            with mock.patch.object(updater, 'urlopen', return_value=Interrupted(b'bad', url=release.zipball_url)), \
                 mock.patch.object(channels, 'download_domestic_archive', side_effect=fallback) as download:
                self.assertEqual(updater.download_release_archive(release, target), 'mirror-result')
            download.assert_called_once()
