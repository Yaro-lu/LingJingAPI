import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.workflow_adaptation import (
    analyze_workflow, convert_editor_workflow, graph_hash, infer_output_type,
    make_mapping, mapping_fields, prepare_mapped_graph, validate_graph, video_timing,
)
from app.workflow_registry import WorkflowRegistry


def example_graph():
    return {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "positive"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "negative"}},
        "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0], "cfg": 7.0, "seed": 0}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "output"}},
    }


class WorkflowAdaptationTests(unittest.TestCase):
    def test_h3_duration_upload_and_fps_keep_original_alignment(self):
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "old.png"}},
            "2": {"class_type": "PrimitiveFloat", "inputs": {"value": 5}, "_meta": {"title": "Float (duration)"}},
            "3": {"class_type": "ComfyMathExpression", "inputs": {"values.a": ["2", 0], "expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"}},
            "4": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"first_frame": ["1", 0], "length": ["3", 1]}},
            "5": {"class_type": "CreateVideo", "inputs": {"fps": 24, "images": ["4", 0]}},
            "6": {"class_type": "SaveVideo", "inputs": {"video": ["5", 0]}},
            "7": {"class_type": "BasicScheduler", "inputs": {"denoise": 1}},
        }
        original = copy.deepcopy(graph)
        mapping = analyze_workflow(graph)
        fields = mapping_fields(mapping)
        by_name = {field["name"]: field for field in fields}
        self.assertEqual(by_name["duration"]["type"], "number")
        self.assertEqual(by_name["image"]["label"], "首帧图片")
        self.assertEqual(by_name["denoise"]["type"], "number")
        # Old image schemas may contain the server's local filename choices.
        by_name["image"]["options"] = ["old.png"]
        by_name["image"]["default"] = "old.png"
        mapping = make_mapping(graph, fields, "video")
        upload = mock.Mock(return_value="uploaded.png")
        result = prepare_mapped_graph(graph, mapping, {"image": "data:image/png;base64,YQ==", "duration": 3.5, "fps": 30, "denoise": 0.6}, upload)
        self.assertEqual(result["1"]["inputs"]["image"], "uploaded.png")
        self.assertEqual(result["2"]["inputs"]["value"], 3.5)
        self.assertIn("a * 30", result["3"]["inputs"]["expression"])
        self.assertIn("% 17", result["3"]["inputs"]["expression"])
        self.assertEqual(result["4"]["inputs"]["length"], ["3", 1])
        self.assertEqual(graph, original)
        timing = video_timing(graph)
        self.assertEqual((timing["duration"], timing["fps"], timing["frame_step"], timing["frame_offset"]), (5, 24, 17, 5))

    def test_classification_uses_declared_type_and_real_output(self):
        self.assertEqual(infer_output_type(example_graph(), "image.text_to_image"), "image")
        graph = {"1": {"class_type": "TextGenerate", "inputs": {}}, "2": {"class_type": "PreviewAny", "inputs": {"source": ["1", 0]}}}
        self.assertEqual(infer_output_type(graph), "text")
        self.assertEqual(infer_output_type({"1": {"class_type": "SaveVideo", "inputs": {}}}), "video")

    def test_rules_use_connections_not_titles_for_positive_negative(self):
        llm = mock.Mock()
        result = analyze_workflow(example_graph(), llm=llm)
        self.assertEqual(result["api_mapping_status"], "ready")
        self.assertEqual(result["api_bindings"]["negative_prompt"], [{"node_id": "2", "input": "text"}])
        llm.assert_not_called()

    def test_exact_injection_preserves_graph_constants_and_zero(self):
        graph = example_graph()
        original = copy.deepcopy(graph)
        mapping = analyze_workflow(graph)
        patched = prepare_mapped_graph(graph, mapping, {"prompt": "new", "cfg": 0, "seed": 0})
        self.assertEqual(patched["1"]["inputs"]["text"], "new")
        self.assertEqual(patched["2"]["inputs"]["text"], "negative")
        self.assertEqual(patched["3"]["inputs"]["cfg"], 0)
        self.assertEqual(graph, original)

    def test_empty_mapping_and_false_are_supported(self):
        graph = {"1": {"class_type": "CustomFlag", "inputs": {"flag": True}}, "2": {"class_type": "SaveImage", "inputs": {}}}
        mapping = make_mapping(graph, [{"name": "flag", "type": "boolean", "targets": [{"node_id": "1", "input": "flag"}]}], "image")
        self.assertIs(prepare_mapped_graph(graph, mapping, {"flag": False})["1"]["inputs"]["flag"], False)
        empty = make_mapping(graph, [], "image")
        self.assertEqual(prepare_mapped_graph(graph, empty, {}), graph)

    def test_two_images_upload_to_independent_targets(self):
        graph = {str(i): {"class_type": "LoadImage", "inputs": {"image": f"{i}.png"}} for i in (1, 2)}
        graph["3"] = {"class_type": "SaveImage", "inputs": {}}
        mapping = analyze_workflow(graph)
        upload = mock.Mock(side_effect=lambda name, value: name + ".png")
        patched = prepare_mapped_graph(graph, mapping, {"image": "data:a", "image_2": "data:b"}, upload)
        self.assertEqual(patched["1"]["inputs"]["image"], "image.png")
        self.assertEqual(patched["2"]["inputs"]["image"], "image_2.png")
        self.assertEqual(upload.call_count, 2)

    def test_invalid_request_and_stale_mapping_are_rejected(self):
        graph = example_graph()
        mapping = analyze_workflow(graph)
        for body in ({"cfg": "not number"}, {"surprise": 3}, {"cfg": True}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                prepare_mapped_graph(graph, mapping, body)
        graph["1"]["inputs"]["text"] = "changed"
        with self.assertRaisesRegex(ValueError, "映射失效"):
            prepare_mapped_graph(graph, mapping, {})

    def test_llm_runs_once_and_failure_is_actionable(self):
        graph = {"1": {"class_type": "UnknownPrompt", "inputs": {"query": "hi"}}, "2": {"class_type": "SaveImage", "inputs": {}}}
        for response in ("not json", '{"error":"unclear"}', '{"fields":[],"output_type":"image"}'):
            llm = mock.Mock(return_value=response)
            result = analyze_workflow(graph, llm=llm)
            self.assertEqual(result["api_mapping_status"], "needs_review")
            self.assertIn("手动填写", result["api_mapping_error"])
            llm.assert_called_once()
        llm = mock.Mock(return_value=json.dumps({"output_type": "image", "fields": [
            {"name": "prompt", "type": "text", "targets": [{"node_id": "1", "input": "query"}]}]}))
        result = analyze_workflow(graph, llm=llm)
        self.assertEqual(result["api_mapping_status"], "ready")

    def test_malformed_graph_bindings_and_ranges_are_rejected(self):
        for graph in ({"1": {"class_type": 42}}, {"1": {"class_type": "X", "inputs": {"v": ["missing", 0]}}}):
            with self.assertRaises(ValueError):
                validate_graph(graph)
        graph = example_graph()
        for field in (
            {"name": "a", "type": "text", "targets": [{"node_id": "3", "input": "positive"}]},
            {"name": "a", "type": "text", "targets": [{"node_id": "4", "input": "filename_prefix"}]},
            {"name": "a", "type": "number", "minimum": 9, "maximum": 1, "targets": [{"node_id": "3", "input": "cfg"}]},
        ):
            with self.assertRaises(ValueError):
                make_mapping(graph, [field], "image")

    def test_llm_cannot_change_known_fields_or_select_internal_candidates(self):
        graph = example_graph()
        graph["5"] = {"class_type": "Unknown", "inputs": {"query": "hello"}}
        draft = analyze_workflow(graph)
        fields = mapping_fields(draft)
        extra_query = {"name": "query", "type": "text", "targets": [{"node_id": "5", "input": "query"}]}
        merged = analyze_workflow(graph, llm=lambda _: json.dumps({"output_type": "image", "fields": [extra_query]}))
        self.assertEqual(merged["api_mapping_status"], "ready")
        self.assertEqual(len(mapping_fields(merged)), len(fields) + 1)
        changed = copy.deepcopy(fields)
        next(f for f in changed if f["name"] == "cfg")["default"] = 999
        result = analyze_workflow(graph, llm=lambda _: json.dumps({"output_type": "image", "fields": changed}))
        self.assertEqual(result["api_mapping_status"], "needs_review")
        graph["6"] = {"class_type": "SomeModelLoader", "inputs": {"model": "secret-path"}}
        extra = {"name": "internal", "type": "text", "targets": [{"node_id": "6", "input": "model"}]}
        result = analyze_workflow(graph, llm=lambda _: json.dumps({"output_type": "image", "fields": fields + [extra]}))
        self.assertEqual(result["api_mapping_status"], "needs_review")

    def test_editor_conversion_does_not_guess_widget_names(self):
        with self.assertRaisesRegex(ValueError, "API Format"):
            convert_editor_workflow({"nodes": [{"id": 1, "type": "KSampler", "widgets_values": [42, 20, 7, "euler", "normal", 1]}]})
        graph = convert_editor_workflow({"nodes": [{"id": 1, "type": "CLIPTextEncode", "inputs": [], "widgets_values": ["hello"]}], "links": []})
        self.assertEqual(graph["1"]["inputs"], {"text": "hello"})
        with self.assertRaisesRegex(ValueError, "API Format"):
            convert_editor_workflow({"nodes": [{"id": 1, "type": "Unknown", "inputs": [{"name": "clip", "link": None}], "widgets_values": ["hello"]}], "links": []})
        with self.assertRaisesRegex(ValueError, "API Format"):
            convert_editor_workflow({"nodes": [{"id": 1, "type": "CLIPTextEncode", "mode": 4}], "links": []})

    def test_manual_mapping_persists_and_rolls_back_on_save_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "workflows" / "demo"
            folder.mkdir(parents=True)
            graph = example_graph()
            (folder / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
            manifest = folder / "manifest.json"
            manifest.write_text(json.dumps({"id": "demo", "type": "image.text_to_image"}), encoding="utf-8")
            registry = WorkflowRegistry(root / "runtime/config.json", root / "workflows")
            mapping = analyze_workflow(graph)
            registry.save_mapping("demo", mapping_fields(mapping), "image", graph_hash(graph))
            reloaded = WorkflowRegistry(root / "runtime/config.json", root / "workflows")
            self.assertEqual(reloaded.get("demo").api_mapping["api_mapping_status"], "ready")
            previous = manifest.read_bytes()
            with mock.patch.object(registry, "_save_unlocked", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    registry.save_mapping("demo", [], "image", graph_hash(graph))
            self.assertEqual(manifest.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
