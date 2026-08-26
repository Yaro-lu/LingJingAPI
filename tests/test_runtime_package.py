import json
import tempfile
import unittest
from pathlib import Path

from app.core.runtime_package import (
    RUNTIME_PACKAGE_NAME,
    RUNTIME_PACKAGE_SIZE,
    RUNTIME_PACKAGE_SHA256,
    RUNTIME_RELEASE_URL,
    REQUIRED_RUNTIME_PATHS,
    SevenZipProgressParser,
    archive_extract_command,
    archive_list_command,
    install_staged_runtime,
    invalid_archive_entries,
    load_runtime_release_manifest,
    missing_archive_entries,
    missing_runtime_paths,
    parse_archive_members,
    read_sha256_sidecar,
    resolve_runtime_download_url,
    sha256_file,
    validate_staged_runtime,
    verify_runtime_package,
    verify_sha256,
)


class RuntimePackageContractTests(unittest.TestCase):
    def test_seven_zip_progress_parser_handles_cr_and_split_chunks(self):
        parser = SevenZipProgressParser()

        self.assertEqual(parser.feed(b"Scanning\r  0%\r 1"), [0])
        self.assertEqual(parser.feed(b"0%\rnoise\r 99%\r100%\r"), [10, 99, 100])
        self.assertEqual(parser.feed(b" 250%\r"), [])
        self.assertEqual(parser.feed(b"77%"), [])
        self.assertEqual(parser.finish(), [77])

    def test_seven_zip_extract_command_enables_streamed_progress(self):
        command = archive_extract_command(
            ("7z", "7z.exe"),
            Path("runtime.7z"),
            Path("staging"),
        )

        self.assertIn("-bsp1", command)
        self.assertIn("-bso0", command)
        self.assertIn("-bse2", command)
        self.assertIn("-sccUTF-8", command)

    def test_seven_zip_list_command_uses_machine_readable_output(self):
        command = archive_list_command(
            ("7z", "7z.exe"),
            Path("runtime.7z"),
        )

        self.assertEqual(
            command,
            ["7z.exe", "l", "-slt", "-sccUTF-8", "runtime.7z"],
        )

    def test_source_controlled_release_manifest_is_consistent(self):
        manifest = load_runtime_release_manifest()

        self.assertEqual(manifest["package_name"], RUNTIME_PACKAGE_NAME)
        self.assertEqual(manifest["size_bytes"], RUNTIME_PACKAGE_SIZE)
        self.assertEqual(manifest["sha256"], RUNTIME_PACKAGE_SHA256)
        self.assertEqual(manifest["download_url"], RUNTIME_RELEASE_URL)
        self.assertTrue(RUNTIME_RELEASE_URL.endswith(f"/{RUNTIME_PACKAGE_NAME}"))

    def test_release_manifest_rejects_url_for_a_different_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_release.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.3",
                        "release_tag": "v1.2.3",
                        "package_name": "runtime-nvidia-rtx20plus-cu130-v1.2.3.7z",
                        "size_bytes": 123,
                        "sha256": "a" * 64,
                        "download_url": "https://example.com/wrong.7z",
                        "homepage_url": "https://example.com/project",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "下载地址与发布标签或包文件名不匹配"):
                load_runtime_release_manifest(path)

    def test_release_manifest_rejects_package_version_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_release.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.3",
                        "release_tag": "v1.2.3",
                        "package_name": "runtime-nvidia-rtx20plus-cu130-v9.9.9.7z",
                        "size_bytes": 123,
                        "sha256": "a" * 64,
                        "download_url": (
                            "https://example.com/v1.2.3/"
                            "runtime-nvidia-rtx20plus-cu130-v9.9.9.7z"
                        ),
                        "homepage_url": "https://example.com/project",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "文件名与版本不匹配"):
                load_runtime_release_manifest(path)

    def test_release_manifest_rejects_download_url_for_wrong_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_release.json"
            package_name = "runtime-nvidia-rtx20plus-cu130-v1.2.3.7z"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.3",
                        "release_tag": "v1.2.3",
                        "package_name": package_name,
                        "size_bytes": 123,
                        "sha256": "a" * 64,
                        "download_url": f"https://example.com/v9.9.9/{package_name}",
                        "homepage_url": "https://example.com/project",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "发布标签或包文件名不匹配"):
                load_runtime_release_manifest(path)

    def test_download_url_has_fixed_default_and_controlled_overrides(self):
        self.assertEqual(resolve_runtime_download_url({}, {}), RUNTIME_RELEASE_URL)
        self.assertEqual(
            resolve_runtime_download_url(
                {"runtime": {"download_url": "https://mirror.example/runtime"}},
                {},
            ),
            f"https://mirror.example/runtime/{RUNTIME_PACKAGE_NAME}",
        )
        self.assertEqual(
            resolve_runtime_download_url(
                {
                    "runtime": {
                        "download_url": (
                            "https://mirror.example/{release_tag}/{package_name}"
                        )
                    }
                },
                {},
            ),
            RUNTIME_RELEASE_URL.replace(
                "https://github.com/Yaro-lu/LingJingAPI/releases/download",
                "https://mirror.example",
            ),
        )

    def test_download_url_rejects_unsafe_protocol(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            resolve_runtime_download_url(
                {"runtime": {"download_url": "file:///tmp/runtime.7z"}},
                {},
            )

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            resolve_runtime_download_url(
                {"runtime": {"download_url": "http://mirror.example/runtime.7z"}},
                {},
            )

    def test_archive_requires_portable_dependencies_and_no_nested_root(self):
        members = [path.as_posix() for path in REQUIRED_RUNTIME_PATHS]
        self.assertEqual(missing_archive_entries(members), [])

        nested = [f"package/{member}" for member in members]
        self.assertEqual(
            set(missing_archive_entries(nested)),
            {path.as_posix() for path in REQUIRED_RUNTIME_PATHS},
        )

    def test_runtime_layout_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS[:-1]:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixture")
            self.assertEqual(missing_runtime_paths(base), ["bin/cloudflared.exe"])

    def test_runtime_layout_rejects_empty_core_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()

            self.assertEqual(
                set(missing_runtime_paths(base)),
                {path.as_posix() for path in REQUIRED_RUNTIME_PATHS},
            )

    def test_archive_rejects_traversal_and_project_or_user_data(self):
        members = [
            ".venv/Lib/site-packages/torch/__init__.py",
            "runtime/python/python.exe",
            "runtime/ComfyUI/main.py",
            "runtime/ComfyUI/models/secret.safetensors",
            "bin/cloudflared.exe",
            "../outside.txt",
            "runtime/account_session.json",
            "app/gui/main_gateway.py",
        ]
        self.assertEqual(
            invalid_archive_entries(members),
            [
                "runtime/ComfyUI/models/secret.safetensors",
                "../outside.txt",
                "runtime/account_session.json",
                "app/gui/main_gateway.py",
            ],
        )

    def test_7z_technical_listing_handles_solid_entries_and_spaces(self):
        listing = (
            "Path = D:\\cache\\runtime.7z\n"
            "Type = 7z\n"
            "Physical Size = 2026780758\n"
            "\n"
            "----------\n"
            "Path = runtime\\python\\python.exe\n"
            "Size = 105816\n"
            "Packed Size = \n"
            "\n"
            "Path = runtime\\ComfyUI\\main.py\n"
            "Size = 24470\n"
            "Packed Size = \n"
            "\n"
            "Path = .venv\\Lib\\site-packages\\torch\\__init__.py\n"
            "Size = 105847\n"
            "Packed Size = \n"
            "\n"
            "Path = bin\\cloudflared.exe\n"
            "Size = 54166424\n"
            "Packed Size = \n"
            "\n"
            "Path = runtime\\ComfyUI\\models\\my model.bin\n"
            "Size = 456\n"
            "Packed Size = \n"
        )

        members = parse_archive_members(("7z", "7z.exe"), listing)
        self.assertEqual(
            members,
            [
                "runtime/python/python.exe",
                "runtime/ComfyUI/main.py",
                ".venv/Lib/site-packages/torch/__init__.py",
                "bin/cloudflared.exe",
                "runtime/ComfyUI/models/my model.bin",
            ],
        )
        self.assertEqual(missing_archive_entries(members), [])

    def test_sha256_sidecar_is_parsed_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / "runtime.7z"
            package.write_bytes(b"portable runtime fixture")
            expected = sha256_file(package)
            sidecar = base / "runtime.7z.sha256"
            sidecar.write_text(f"{expected}  {package.name}\n", encoding="utf-8")

            self.assertEqual(read_sha256_sidecar(sidecar), expected)
            self.assertEqual(verify_sha256(package, sidecar), (True, expected, expected))

    def test_runtime_package_requires_pinned_hash_even_with_matching_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            package = base / RUNTIME_PACKAGE_NAME
            package.write_bytes(b"tampered runtime")
            sidecar = Path(f"{package}.sha256")
            actual = sha256_file(package)
            sidecar.write_text(f"{actual}  {package.name}\n", encoding="utf-8")

            valid, expected, observed = verify_runtime_package(package, sidecar)

            self.assertFalse(valid)
            self.assertEqual(expected, RUNTIME_PACKAGE_SHA256)
            self.assertEqual(observed, actual)

    def test_staged_runtime_rejects_extra_roots_and_file_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for relative in REQUIRED_RUNTIME_PATHS:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"runtime")
            (staging / "app" / "malicious.py").parent.mkdir(parents=True)
            (staging / "app" / "malicious.py").write_text("bad", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "不允许的路径"):
                validate_staged_runtime(staging)
            (staging / "app" / "malicious.py").unlink()
            (staging / "app").rmdir()
            with self.assertRaisesRegex(ValueError, "文件数量"):
                validate_staged_runtime(staging, max_files=1)

    def test_staged_runtime_install_replaces_only_environment_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            staging = Path(tmp) / "staging"
            (base / "models").mkdir(parents=True)
            (base / "models" / "keep.bin").write_bytes(b"model")
            (base / "runtime" / "python").mkdir(parents=True)
            (base / "runtime" / "python" / "old.txt").write_text("old", encoding="utf-8")
            (base / ".venv" / "Scripts").mkdir(parents=True)
            (base / ".venv" / "Scripts" / "python.exe").write_bytes(b"broken-launcher")
            (base / ".venv" / "pyvenv.cfg").write_text("stale", encoding="utf-8")
            for relative in REQUIRED_RUNTIME_PATHS:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"new-runtime")

            install_staged_runtime(staging, base)

            self.assertEqual((base / "models" / "keep.bin").read_bytes(), b"model")
            self.assertFalse((base / "runtime" / "python" / "old.txt").exists())
            self.assertFalse((base / ".venv" / "Scripts").exists())
            self.assertFalse((base / ".venv" / "pyvenv.cfg").exists())
            self.assertEqual(
                (base / "runtime" / "python" / "python.exe").read_bytes(),
                b"new-runtime",
            )


if __name__ == "__main__":
    unittest.main()
