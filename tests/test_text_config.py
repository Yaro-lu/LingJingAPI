import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config


class TextConfigTests(unittest.TestCase):
    def test_default_file_is_created_only_when_professional_settings_are_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = Config(base)

            text_path = base / "runtime" / "config.local.txt"
            self.assertFalse(text_path.exists())

            self.assertEqual(config.ensure_file(), text_path)
            self.assertTrue(text_path.is_file())

    def test_professional_settings_are_saved_as_readable_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = Config(base)
            config.set("server.port", 19001)
            config.set("comfyui.vram_mode", "low")
            config.set("comfyui.launch_args", ["--extra-model-paths-config", "E:\\中文 模型\\路径.txt"])
            config.save()

            text_path = base / "runtime" / "config.local.txt"
            self.assertTrue(text_path.is_file())
            self.assertFalse((base / "runtime" / "config.local.json").exists())
            content = text_path.read_text(encoding="utf-8")
            self.assertIn("[server]", content)
            self.assertIn("port = 19001", content)
            self.assertIn("[comfyui]", content)
            self.assertIn("vram_mode = low", content)
            self.assertIn(
                'launch_args = ["--extra-model-paths-config", "E:\\\\中文 模型\\\\路径.txt"]',
                content,
            )

            loaded = Config(base)
            self.assertEqual(loaded.server_port, 19001)
            self.assertEqual(loaded.get("comfyui.vram_mode"), "low")
            self.assertEqual(
                loaded.get("comfyui.launch_args"),
                ["--extra-model-paths-config", "E:\\中文 模型\\路径.txt"],
            )

    def test_legacy_json_is_migrated_once_to_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runtime = base / "runtime"
            runtime.mkdir()
            legacy = runtime / "config.local.json"
            legacy.write_text(
                json.dumps({"server": {"port": 19002}, "comfyui": {"vram_mode": "high"}}),
                encoding="utf-8",
            )

            config = Config(base)

            self.assertEqual(config.server_port, 19002)
            self.assertEqual(config.get("comfyui.vram_mode"), "high")
            self.assertTrue((runtime / "config.local.txt").is_file())

    def test_directory_mappings_persist_and_support_chinese_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "客户端"
            target = Path(tmp) / "外部资源" / "模型目录"
            target.mkdir(parents=True)
            config = Config(base)

            saved = config.set_directory_mapping("models", target)

            self.assertEqual(saved, target.resolve())
            loaded = Config(base)
            self.assertEqual(loaded.models_dir, target.resolve())
            content = (base / "runtime" / "config.local.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("[directories]", content)
            self.assertIn(f"models = {target.resolve()}", content)

    def test_parent_storage_directory_adapts_only_requested_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            root = Path(tmp) / "外部资源"
            for name in ("models", "workflows", "outputs"):
                (root / name).mkdir(parents=True)
            image = root / "outputs" / "keep.png"
            image.write_bytes(b"original")
            config = Config(base)
            for name in ("models", "workflows", "outputs"):
                self.assertEqual(config.set_directory_mapping(name, root), (root / name).resolve())
            self.assertEqual(image.read_bytes(), b"original")
            nested = root / "other" / "ComfyUI" / "models"
            nested.mkdir(parents=True)
            self.assertEqual(config.set_directory_mapping("models", root / "other"), nested.resolve())
            self.assertEqual(config.outputs_dir, (root / "outputs").resolve())

    def test_missing_mapped_directory_is_cleared_and_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            target = Path(tmp) / "external-outputs"
            target.mkdir()
            config = Config(base)
            config.set_directory_mapping("outputs", target)
            target.rmdir()

            loaded = Config(base)

            self.assertEqual(loaded.outputs_dir, (base / "outputs").resolve())
            self.assertEqual(loaded.get("directories.outputs"), "")
            reloaded = Config(base)
            self.assertEqual(reloaded.get("directories.outputs"), "")

    def test_directory_mapping_rejects_a_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "client"
            config = Config(base)

            with self.assertRaises(ValueError):
                config.set_directory_mapping("logs", Path(tmp) / "missing")


if __name__ == "__main__":
    unittest.main()
