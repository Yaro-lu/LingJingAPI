import json
import unittest
from pathlib import Path

from app.core.model_maintenance import MODEL_REQUIREMENTS


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_DIR = ROOT / "workflows" / "wan_flf2v_v1"
VACE_DIR = ROOT / "workflows" / "wan_vace_flf2v_1_3b"


class BundledWanWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_manifest = json.loads(
            (ORIGINAL_DIR / "manifest.json").read_text(encoding="utf-8-sig")
        )
        cls.original_workflow = json.loads(
            (ORIGINAL_DIR / "workflow.json").read_text(encoding="utf-8-sig")
        )
        cls.vace_manifest = json.loads(
            (VACE_DIR / "manifest.json").read_text(encoding="utf-8-sig")
        )
        cls.vace_workflow = json.loads(
            (VACE_DIR / "workflow.json").read_text(encoding="utf-8-sig")
        )

    def test_original_and_vace_are_two_unique_workflows(self):
        self.assertEqual(self.original_manifest["id"], "wan_flf2v_v1")
        self.assertEqual(self.vace_manifest["id"], "wan_vace_flf2v_1_3b")
        self.assertNotEqual(self.original_manifest["id"], self.vace_manifest["id"])
        self.assertEqual(self.original_manifest["model_group"], "Wan2.1 FLF2V 14B")
        self.assertEqual(self.vace_manifest["model_group"], "Wan2.1")

    def test_original_14b_graph_and_visual_workflow_are_restored(self):
        package_text = json.dumps(self.original_workflow, ensure_ascii=False)
        self.assertIn("wan2.1_flf2v_720p_14B_fp16.safetensors", package_text)
        self.assertTrue((ORIGINAL_DIR / "wan2.1_flf2v_720_f16.json").is_file())
        self.assertNotIn(
            "WanVaceToVideo",
            [node.get("class_type") for node in self.original_workflow.values()],
        )

    def test_vace_remains_the_lightweight_first_last_frame_graph(self):
        classes = [node.get("class_type") for node in self.vace_workflow.values()]
        self.assertIn("WanVaceToVideo", classes)
        self.assertIn("RepeatImageBatch", classes)
        self.assertIn("TrimVideoLatent", classes)
        self.assertIn(
            "wan2.1_vace_1.3B_fp16.safetensors",
            json.dumps(self.vace_workflow, ensure_ascii=False),
        )

    def test_installer_never_deletes_the_restored_original_workflow(self):
        installer = (ROOT / "installer" / "LingJingAPI.iss").read_text(encoding="utf-8-sig")
        self.assertNotIn(
            r'{app}\workflows\wan_flf2v_v1\wan2.1_flf2v_720_f16.json',
            installer,
        )

    def test_wan_model_groups_have_separate_download_contracts(self):
        vace_names = {
            item["path"] for item in MODEL_REQUIREMENTS["Wan2.1"]["items"]
        }
        original_names = {
            item["path"]
            for item in MODEL_REQUIREMENTS["Wan2.1 FLF2V 14B"]["items"]
        }
        self.assertEqual(len(vace_names), 3)
        self.assertEqual(len(original_names), 4)
        self.assertIn(
            "diffusion_models/wan2.1_vace_1.3B_fp16.safetensors",
            vace_names,
        )
        self.assertIn(
            "diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
            original_names,
        )
        self.assertIn("clip_vision/clip_vision_h.safetensors", original_names)


if __name__ == "__main__":
    unittest.main()
