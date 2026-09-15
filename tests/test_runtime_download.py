import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.core import runtime_download as downloads


DATA = downloads.MAGIC + b"runtime-package-fixture"
SHA = hashlib.sha256(DATA).hexdigest()


class Response(io.BytesIO):
    def __init__(self, data=DATA, status=200, offset=0, total=len(DATA)):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}
        if status == 206:
            self.headers["Content-Range"] = f"bytes {offset}-{offset + len(data) - 1}/{total}"


class SequentialPool:
    def __init__(self, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def map(self, function, values): return map(function, values)


class RuntimeDownloadTests(unittest.TestCase):
    def download(self, root, opener, url=downloads.RUNTIME_RELEASE_URL):
        target = Path(root) / "runtime.7z"
        downloads.download_runtime_package(url, target, size=len(DATA), sha256=SHA,
                                           open_response=opener, progress=lambda *args: None)
        self.assertEqual(target.read_bytes(), DATA)

    def test_custom_url_is_never_forwarded_to_public_proxies(self):
        address = "https://private.example/runtime.7z?token=private"
        self.assertEqual(downloads.runtime_download_sources(address), (("自定义下载源", address),))
        with self.assertRaises(ValueError):
            downloads.runtime_download_sources("http://unsafe.example/runtime.7z")

    def test_selects_fastest_valid_probe_and_sends_no_credentials(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            return Response()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(downloads, "ThreadPoolExecutor", SequentialPool), \
             mock.patch.object(downloads.time, "monotonic", side_effect=[0, 3, 3, 4, 4, 6]):
            self.download(root, opener)
        self.assertEqual(len(calls), 4)
        self.assertIn("ghproxy.net", calls[-1].full_url)
        for request in calls:
            self.assertFalse(request.has_header("Authorization"))
            self.assertFalse(request.has_header("Cookie"))

    def test_all_routes_404_does_not_create_package(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(IOError, "404"):
                self.download(root, lambda *args, **kwargs: Response(b"Not Found", status=404))
            self.assertFalse((Path(root) / "runtime.7z").exists())

    def test_html_success_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(IOError, "7z"):
                self.download(root, lambda *args, **kwargs: Response(b"x" * len(DATA)))
            self.assertFalse((Path(root) / "runtime.7z").exists())

    def test_interrupted_route_resumes_on_next_route(self):
        calls = []
        class Interrupted(Response):
            def read(self, size):
                if self.tell(): raise OSError("connection lost")
                return super().read(9)
        def opener(request, timeout):
            calls.append(request)
            if request.get_header("Range") == f"bytes=0-{len(DATA)-1}":
                return Response()
            if "gh-proxy.com" in request.full_url:
                return Interrupted()
            self.assertEqual(request.get_header("Range"), "bytes=9-")
            return Response(DATA[9:], status=206, offset=9)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(downloads, "ThreadPoolExecutor", SequentialPool), \
             mock.patch.object(downloads.time, "monotonic", side_effect=[0, 1, 1, 3, 3, 6]):
            self.download(root, opener)
        self.assertEqual(len(calls), 5)

    def test_bad_hash_is_discarded_before_next_source(self):
        def opener(request, timeout):
            if request.get_header("Range"):
                self.assertEqual(request.get_header("Range"), f"bytes=0-{len(DATA)-1}")
                return Response()
            return Response(b"x" * len(DATA) if "gh-proxy.com" in request.full_url else DATA)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(downloads, "ThreadPoolExecutor", SequentialPool), \
             mock.patch.object(downloads.time, "monotonic", side_effect=[0, 1, 1, 3, 3, 6]):
            self.download(root, opener)

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "runtime.7z.part").write_bytes(DATA[:9])
            self.download(root, lambda *args, **kwargs: Response(), "https://custom.example/runtime.7z")

    def test_complete_partial_cache_works_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "runtime.7z.part").write_bytes(DATA)
            opener = mock.Mock(side_effect=AssertionError("network must not be used"))
            self.download(root, opener)
            opener.assert_not_called()

    def test_bad_resume_range_cannot_overwrite_partial(self):
        with tempfile.TemporaryDirectory() as root:
            partial = Path(root) / "runtime.7z.part"
            partial.write_bytes(DATA[:9])
            with self.assertRaisesRegex(IOError, "断点位置"):
                self.download(root, lambda *args, **kwargs: Response(DATA[8:], status=206, offset=8),
                              "https://custom.example/runtime.7z")
            self.assertEqual(partial.read_bytes(), DATA[:9])

    def test_gui_download_installs_only_after_verified_transfer_without_opening_ui(self):
        import threading
        from app.gui import main_gateway
        app = object.__new__(main_gateway.GatewayApp)
        app._shutting_down = False
        app.after = lambda delay, callback: callback()
        app._create_runtime_progress_dialog = mock.Mock(return_value={"popup": None})
        app._close_maintenance_dialog = mock.Mock()
        app._set_runtime_progress = mock.Mock()
        app._extract_runtime = mock.Mock()
        app._show_runtime_download_fallback = mock.Mock()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(main_gateway, "BASE_DIR", Path(root)), \
             mock.patch.object(main_gateway, "RUNTIME_PACKAGE_SIZE", len(DATA)), \
             mock.patch.object(main_gateway, "RUNTIME_PACKAGE_SHA256", SHA), \
             mock.patch.object(main_gateway, "_open_download_request", return_value=Response()), \
             mock.patch.object(threading.Thread, "start", lambda thread: thread.run()):
            app._download_runtime("https://custom.example/runtime.7z")
            app._extract_runtime.assert_called_once_with(
                Path(root) / "cache" / main_gateway.RUNTIME_PACKAGE_NAME, repair_confirmed=False)
            app._show_runtime_download_fallback.assert_not_called()
