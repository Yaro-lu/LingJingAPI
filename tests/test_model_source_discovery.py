import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import model_source_discovery as discovery


class ModelSourceDiscoveryTests(unittest.TestCase):
    def test_similar_names_are_not_candidates_and_equal_hashes_collapse(self):
        rows = [
            {"filename": "model.safetensors", "url": "first"},
            {"filename": "model.safetensors", "url": "second"},
            {"filename": "model-fp8.safetensors", "url": "wrong"},
        ]
        with mock.patch.object(discovery, '_model_index', return_value=rows), mock.patch.object(
            discovery, 'verify_hf_model_url', return_value={'url': 'pinned', 'repo': 'author/repo', 'sha256': 'b'*64, 'size_bytes': 1}
        ) as verify:
            found = discovery.discover_model_sources('model.safetensors')
        self.assertEqual(len(found), 1)
        self.assertEqual({call.args[0] for call in verify.call_args_list}, {'first', 'second'})

    def test_unverifiable_candidate_must_not_turn_ambiguous_name_into_unique_match(self):
        rows = [{'filename': 'model.safetensors', 'url': value} for value in ('a', 'b')]
        with mock.patch.object(discovery, '_model_index', return_value=rows), mock.patch.object(
            discovery, 'verify_hf_model_url', side_effect=[{'sha256': 'b'*64}, OSError('offline')]
        ):
            with self.assertRaisesRegex(ValueError, '无法确认唯一版本'):
                discovery.discover_model_sources('model.safetensors')

    def test_verifies_exact_filename_and_returns_pinned_lfs_identity(self):
        raw = "https://huggingface.co/Comfy-Org/example/blob/main/vae/model.safetensors"
        revision = "a" * 40
        digest = "b" * 64

        def fake_get(url, *, host):
            self.assertEqual(host, "huggingface.co")
            if url.endswith("/api/models/Comfy-Org/example"):
                return {"sha": revision}
            self.assertIn(f"/tree/{revision}/vae?expand=true", url)
            return [{"path": "vae/model.safetensors", "size": 42, "lfs": {"oid": digest}}]

        with mock.patch.object(discovery, "_get_json", side_effect=fake_get):
            result = discovery.verify_hf_model_url(raw, "model.safetensors")
        self.assertEqual(result["url"], f"https://huggingface.co/Comfy-Org/example/resolve/{revision}/vae/model.safetensors")
        self.assertEqual(result["size_bytes"], 42)
        self.assertEqual(result["sha256"], digest)

    def test_rejects_wrong_filename_and_non_hf_address_before_network(self):
        with mock.patch.object(discovery, "_get_json") as get_json:
            with self.assertRaises(ValueError):
                discovery.verify_hf_model_url(
                    "https://huggingface.co/org/repo/resolve/main/wrong.safetensors",
                    "needed.safetensors",
                )
            with self.assertRaises(ValueError):
                discovery.verify_hf_model_url(
                    "https://example.com/org/repo/resolve/main/needed.safetensors",
                    "needed.safetensors",
                )
        get_json.assert_not_called()

    def test_ambiguous_same_name_different_hashes_remain_two_choices(self):
        rows = [
            {"filename": "model.safetensors", "url": "https://huggingface.co/one/a/resolve/main/model.safetensors"},
            {"filename": "model.safetensors", "url": "https://huggingface.co/two/b/resolve/main/model.safetensors"},
        ]
        def fake_verify(url, _name):
            return {"url": url, "repo": url.split("/")[3], "sha256": ("a" if "/one/" in url else "b") * 64, "size_bytes": 100}
        with (
            mock.patch.object(discovery, "_model_index", return_value=rows),
            mock.patch.object(discovery, "verify_hf_model_url", side_effect=fake_verify),
        ):
            found = discovery.discover_model_sources("model.safetensors")
        self.assertEqual(len(found), 2)

    def test_saved_source_requires_pinned_url_and_valid_digest(self):
        source = {
            "url": f"https://huggingface.co/org/repo/resolve/{'a' * 40}/model.safetensors",
            "size_bytes": 123,
            "sha256": "b" * 64,
            "repo": "org/repo",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "model_sources.json"
            discovery.save_verified_source(path, "vae/model.safetensors", source)
            self.assertEqual(discovery.load_saved_source(path, "vae/model.safetensors")["sha256"], "b" * 64)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["vae/model.safetensors"]["url"] = "https://huggingface.co/org/repo/resolve/main/model.safetensors"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(discovery.load_saved_source(path, "vae/model.safetensors"))


if __name__ == "__main__":
    unittest.main()
