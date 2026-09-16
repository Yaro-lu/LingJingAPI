import json
import unittest
from pathlib import Path
from app.core.workflow_capability import infer_capability, output_kind

class WorkflowCapabilityTests(unittest.TestCase):
    def test_actual_h3(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "workflows/video_minimax_h3_i2v/manifest.json").read_text(encoding="utf-8"))
        data["capability"] = "text_to_image"
        data["name"] = "unrelated name"
        self.assertEqual(infer_capability(data), "image_to_video")
        self.assertEqual(output_kind(data), "video")

    def test_contract_cases(self):
        cases = [
            ("video", [{"name":"prompt", "type":"text"}], "text_to_video"),
            ("video", [{"name":"image", "type":"image"}], "image_to_video"),
            ("video", [{"name":"start_image", "type":"image"}, {"name":"end_image", "type":"image"}], "first_last_frame"),
            ("video", [{"name":"reference1", "type":"image"}, {"name":"reference2", "type":"image"}], "image_to_video"),
            ("video", [{"name":"clip", "type":"video"}], "video_to_video"),
            ("image", [{"name":"prompt", "type":"text"}], "text_to_image"),
            ("image", [{"name":"reference", "type":"image"}], "text_image_to_image"),
            ("text", [{"name":"prompt", "type":"text"}], "text_model"),
        ]
        for out, fields, expected in cases:
            with self.subTest(expected=expected, fields=fields):
                data = {"type":"image.text_to_image", "capability":"text_to_image", "input_schema":{"inputs": fields, "response":{"type":out}}}
                self.assertEqual(infer_capability(data), expected)
                self.assertEqual(output_kind(data), out)

    def test_missing_inputs_are_not_treated_as_text_only(self):
        self.assertEqual(infer_capability({"output_type":"video"}), "")
        self.assertEqual(infer_capability({"output_type":"video", "inputs":["image"]}), "")

if __name__ == "__main__":
    unittest.main()
