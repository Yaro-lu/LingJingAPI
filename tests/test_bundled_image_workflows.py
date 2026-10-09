import json
import unittest
from pathlib import Path

from app.core.model_maintenance import MODEL_REQUIREMENTS


ROOT = Path(__file__).resolve().parents[1]


class BundledImageWorkflowContractTests(unittest.TestCase):
    @staticmethod
    def manifest(folder: str) -> dict:
        return json.loads(
            (ROOT / "workflows" / folder / "manifest.json").read_text(
                encoding="utf-8-sig"
            )
        )

    @staticmethod
    def workflow(folder: str, filename: str = "workflow.json") -> dict:
        return json.loads(
            (ROOT / "workflows" / folder / filename).read_text(
                encoding="utf-8-sig"
            )
        )

    def test_every_bundled_workflow_declares_capability_and_model_group(self):
        expected = {
            "flux_t2i_v1": ("text_to_image", "Flux2"),
            "flux2_klein_4b_v1": ("text_image_to_image", "Flux2 Klein 4B"),
            "z_image_t2i_v1": ("text_to_image", "Z-Image"),
            "wan_flf2v_v1": ("first_last_frame", "Wan2.1 FLF2V 14B"),
            "wan_vace_flf2v_1_3b": ("first_last_frame", "Wan2.1"),
            "llm_qwen3_text_gen": ("text_model", "Qwen3.5"),
        }
        for folder, pair in expected.items():
            with self.subTest(folder=folder):
                manifest = self.manifest(folder)
                self.assertEqual(
                    (manifest.get("capability"), manifest.get("model_group")),
                    pair,
                )

    def test_flux2_klein_4b_supports_text_and_reference_image_variants(self):
        manifest = self.manifest("flux2_klein_4b_v1")
        edit = self.workflow("flux2_klein_4b_v1")
        t2i = self.workflow("flux2_klein_4b_v1", "workflow_t2i.json")
        self.assertEqual(
            manifest["workflow_variants"],
            {
                "text_to_image": "workflow_t2i.json",
                "image_to_image": "workflow.json",
            },
        )
        edit_classes = {node["class_type"] for node in edit.values()}
        t2i_classes = {node["class_type"] for node in t2i.values()}
        self.assertIn("LoadImage", edit_classes)
        self.assertIn("ReferenceLatent", edit_classes)
        self.assertNotIn("LoadImage", t2i_classes)
        self.assertEqual(edit["15"]["inputs"]["steps"], 4)
        self.assertEqual(t2i["8"]["inputs"]["steps"], 4)
        self.assertEqual(t2i["10"]["inputs"]["width"], 768)

    def test_lightweight_repair_replaces_unusable_flux_editor_workflow(self):
        manifest = self.manifest("flux")
        graph = self.workflow("flux")
        self.assertEqual(manifest["model_group"], "Flux2 Klein 4B")
        self.assertEqual(manifest["capability"], "image_to_image")
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["input_schema"]["required"], ["image", "prompt"])
        self.assertTrue((ROOT / "workflows" / "flux" / "frontend_workflow.json").is_file())
        self.assertEqual(graph["20"]["class_type"], "ImageScaleBy")
        self.assertEqual(graph["20"]["inputs"]["scale_by"], 2.0)
        self.assertEqual(graph["19"]["inputs"]["images"], ["20", 0])
        original = self.workflow("flux2_klein_4b_v1")
        for node_id in ("4", "5", "6"):
            self.assertEqual(graph[node_id]["inputs"], original[node_id]["inputs"])

    def test_z_image_turbo_is_an_eight_step_768_text_to_image_graph(self):
        workflow = self.workflow("z_image_t2i_v1")
        self.assertEqual(
            workflow["1"]["inputs"]["unet_name"],
            "z_image_turbo_bf16.safetensors",
        )
        self.assertEqual(workflow["2"]["inputs"]["type"], "lumina2")
        self.assertEqual(workflow["2"]["inputs"]["device"], "cpu")
        self.assertEqual(workflow["6"]["inputs"]["width"], 768)
        self.assertEqual(workflow["8"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["8"]["inputs"]["cfg"], 1.0)

    def test_new_model_downloads_are_pinned_and_size_verified(self):
        expected_counts = {"Flux2 Klein 4B": 3, "Z-Image": 3}
        for group, count in expected_counts.items():
            with self.subTest(group=group):
                items = MODEL_REQUIREMENTS[group]["items"]
                self.assertEqual(len(items), count)
                for item in items:
                    self.assertIn("/resolve/", item["url"])
                    self.assertNotIn("/resolve/main/", item["url"])
                    self.assertGreater(item["size_bytes"], 0)
                    self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
