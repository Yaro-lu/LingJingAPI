import json
import re
import unittest
from pathlib import Path

from app.core.model_maintenance import MODEL_REQUIREMENTS


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / "workflows"
NEW_VIDEO_WORKFLOWS = {
    "ltx2_3_flf2v_v1": {
        "model_group": "LTX-2.3",
        "required_nodes": {
            "LoadImage",
            "CheckpointLoaderSimple",
            "LTXAVTextEncoderLoader",
            "LTXVAudioVAELoader",
            "CLIPTextEncode",
            "LTXVConditioning",
            "EmptyLTXVLatentVideo",
            "LTXVEmptyLatentAudio",
            "LTXVAddGuide",
            "CFGGuider",
            "RandomNoise",
            "SamplerEulerAncestral",
            "ManualSigmas",
            "LTXVConcatAVLatent",
            "SamplerCustomAdvanced",
            "LTXVSeparateAVLatent",
            "LTXVCropGuides",
            "VAEDecodeTiled",
            "LTXVAudioVAEDecode",
            "CreateVideo",
            "SaveVideo",
        },
    },
    "wan2_1_fun_inp_1_3b": {
        "model_group": "Wan2.1 Fun 1.3B",
        "required_nodes": {
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "CLIPVisionLoader",
            "CLIPTextEncode",
            "LoadImage",
            "CLIPVisionEncode",
            "SkipLayerGuidanceDiT",
            "ModelSamplingSD3",
            "UNetTemporalAttentionMultiply",
            "CFGZeroStar",
            "WanFunInpaintToVideo",
            "KSampler",
            "VAEDecode",
            "CreateVideo",
            "SaveVideo",
        },
    },
}
PINNED_REVISION_RE = re.compile(r"/resolve/([0-9a-f]{40})/")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} 顶层必须是 JSON 对象")
    return value


def _nodes_of_type(workflow: dict, class_type: str) -> list[dict]:
    return [
        node
        for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]


def _model_filenames(workflow: dict) -> set[str]:
    names: set[str] = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            if isinstance(value, str) and value.lower().endswith(".safetensors"):
                names.add(Path(value.replace("\\", "/")).name)
    return names


class BundledVideoWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifests = {}
        cls.workflows = {}
        for workflow_id in NEW_VIDEO_WORKFLOWS:
            folder = WORKFLOWS_DIR / workflow_id
            cls.manifests[workflow_id] = _read_json(folder / "manifest.json")
            cls.workflows[workflow_id] = _read_json(folder / "workflow.json")

    def test_new_workflow_manifests_and_api_graphs_are_complete(self):
        for workflow_id, expected in NEW_VIDEO_WORKFLOWS.items():
            with self.subTest(workflow_id=workflow_id):
                manifest = self.manifests[workflow_id]
                workflow = self.workflows[workflow_id]
                self.assertEqual(manifest.get("id"), workflow_id)
                self.assertEqual(manifest.get("type"), "video.first_last_to_video")
                self.assertEqual(manifest.get("engine"), "comfyui")
                self.assertEqual(manifest.get("capability"), "first_last_frame")
                self.assertEqual(manifest.get("model_group"), expected["model_group"])
                self.assertTrue(str(manifest.get("name") or "").strip())
                self.assertTrue(str(manifest.get("description") or "").strip())

                schema = manifest.get("input_schema")
                self.assertIsInstance(schema, dict)
                self.assertTrue(
                    {"prompt", "start_image", "end_image"}.issubset(
                        set(schema.get("required") or [])
                    )
                )

                self.assertTrue(workflow)
                node_types = {
                    node.get("class_type")
                    for node in workflow.values()
                    if isinstance(node, dict)
                }
                self.assertTrue(expected["required_nodes"].issubset(node_types))
                self.assertEqual(len(_nodes_of_type(workflow, "LoadImage")), 2)
                self.assertEqual(len(_nodes_of_type(workflow, "SaveVideo")), 1)

    def test_every_api_link_references_an_existing_node(self):
        for workflow_id, workflow in self.workflows.items():
            node_ids = set(workflow)
            for node_id, node in workflow.items():
                with self.subTest(workflow_id=workflow_id, node_id=node_id):
                    self.assertIsInstance(node, dict)
                    self.assertTrue(str(node.get("class_type") or "").strip())
                    inputs = node.get("inputs")
                    self.assertIsInstance(inputs, dict)
                    for input_name, value in inputs.items():
                        if not (
                            isinstance(value, list)
                            and len(value) == 2
                            and isinstance(value[0], str)
                            and isinstance(value[1], int)
                        ):
                            continue
                        self.assertIn(
                            value[0],
                            node_ids,
                            f"{workflow_id}:{node_id}.{input_name} 引用了不存在的节点 {value[0]}",
                        )
                        self.assertGreaterEqual(value[1], 0)

    def test_start_end_and_prompt_nodes_are_unambiguous(self):
        for workflow_id, workflow in self.workflows.items():
            with self.subTest(workflow_id=workflow_id):
                image_titles = [
                    str(node.get("_meta", {}).get("title") or "").lower()
                    for node in _nodes_of_type(workflow, "LoadImage")
                ]
                self.assertEqual(sum("start" in title for title in image_titles), 1)
                self.assertEqual(sum("end" in title for title in image_titles), 1)

                prompt_titles = [
                    str(node.get("_meta", {}).get("title") or "").lower()
                    for node in _nodes_of_type(workflow, "CLIPTextEncode")
                ]
                self.assertEqual(sum("positive" in title for title in prompt_titles), 1)
                self.assertEqual(sum("negative" in title for title in prompt_titles), 1)

    def test_new_model_groups_have_performance_and_pinned_download_contracts(self):
        for workflow_id, expected in NEW_VIDEO_WORKFLOWS.items():
            group_name = expected["model_group"]
            with self.subTest(workflow_id=workflow_id, group=group_name):
                spec = MODEL_REQUIREMENTS[group_name]
                self.assertTrue(str(spec.get("title") or "").strip())

                performance = spec.get("performance")
                self.assertIsInstance(performance, dict)
                for key in ("level", "minimum", "recommended", "preset", "notes"):
                    self.assertTrue(
                        str(performance.get(key) or "").strip(),
                        f"{group_name}.performance.{key} 不能为空",
                    )

                items = spec.get("items")
                self.assertIsInstance(items, list)
                self.assertTrue(items)
                declared_names = set()
                for item in items:
                    path = str(item.get("path") or "").strip()
                    url = str(item.get("url") or "").strip()
                    declared_names.add(Path(path).name)
                    self.assertTrue(path)
                    self.assertIsNotNone(PINNED_REVISION_RE.search(url), url)
                    self.assertNotIn("/resolve/main/", url)
                    self.assertGreater(int(item.get("size_bytes") or 0), 0)
                    self.assertRegex(str(item.get("sha256") or ""), SHA256_RE)

                self.assertEqual(
                    _model_filenames(self.workflows[workflow_id]),
                    declared_names,
                )

    def test_ltx_uses_the_official_eight_step_sigma_schedule(self):
        workflow = self.workflows["ltx2_3_flf2v_v1"]
        sigma_nodes = _nodes_of_type(workflow, "ManualSigmas")
        self.assertEqual(len(sigma_nodes), 1)
        raw_sigmas = str(sigma_nodes[0].get("inputs", {}).get("sigmas") or "")
        sigmas = [float(value.strip()) for value in raw_sigmas.split(",") if value.strip()]
        self.assertEqual(len(sigmas) - 1, 8)
        self.assertEqual(sigmas[0], 1.0)
        self.assertEqual(sigmas[-1], 0.0)

    def test_wan_fun_defaults_to_81_frames_at_16_fps(self):
        workflow = self.workflows["wan2_1_fun_inp_1_3b"]
        conditioning = _nodes_of_type(workflow, "WanFunInpaintToVideo")
        video = _nodes_of_type(workflow, "CreateVideo")
        self.assertEqual(len(conditioning), 1)
        self.assertEqual(len(video), 1)
        self.assertEqual(conditioning[0]["inputs"]["length"], 81)
        self.assertEqual(conditioning[0]["inputs"]["width"], 480)
        self.assertEqual(conditioning[0]["inputs"]["height"], 768)
        self.assertEqual(video[0]["inputs"]["fps"], 16)

    def test_every_bundled_manifest_declares_resolvable_model_dependencies(self):
        manifests = sorted(WORKFLOWS_DIR.glob("*/manifest.json"))
        self.assertTrue(manifests)
        for path in manifests:
            with self.subTest(manifest=path.parent.name):
                manifest = _read_json(path)
                model_group = str(manifest.get("model_group") or "").strip()
                if model_group:
                    self.assertIn(model_group, MODEL_REQUIREMENTS)
                else:
                    # Imported workflows resolve model files from graph metadata.
                    from app.core.workflow_adaptation import graph_hash
                    graph = _read_json(path.parent / "workflow.json")
                    models = manifest.get("dependencies", {}).get("models", [])
                    self.assertTrue(models, f"{path} 缺少模型依赖")
                    self.assertEqual({item["name"] for item in models}, _model_filenames(graph))
                    self.assertEqual(manifest.get("api_mapping_status"), "ready")
                    self.assertEqual(manifest.get("api_graph_hash"), graph_hash(graph))


if __name__ == "__main__":
    unittest.main()
