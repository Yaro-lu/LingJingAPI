import copy
import json
from pathlib import Path
import unittest

from app.core.h3_dimensions import h3_dimensions
from app.core.workflow_adaptation import prepare_mapped_graph


ROOT = Path(__file__).resolve().parents[1]


def h3_fixture():
    folder = ROOT / "workflows-archive/2026-09-28-h3-replaced/video_minimax_h3_i2v"
    return (json.loads((folder / "workflow.json").read_text(encoding="utf-8")),
            json.loads((folder / "manifest.json").read_text(encoding="utf-8")))


class H3DimensionsTests(unittest.TestCase):
    def test_actual_selector_defaults_and_no_reference_keep_original_graph(self):
        graph, mapping = h3_fixture()
        settings = h3_dimensions(graph)
        self.assertEqual((settings["width"], settings["height"], settings["step"]), (480, 864, 32))
        self.assertEqual(settings["megapixels"], 0.4)

    def test_first_frame_aspect_reaches_actual_h3_node_without_changing_saved_graph(self):
        for size, expected in [((800, 800), (640, 640)), ((1600, 900), (864, 480)), ((900, 1600), (480, 864))]:
            with self.subTest(size=size):
                graph, mapping = h3_fixture()
                original = copy.deepcopy(graph)
                result = prepare_mapped_graph(graph, mapping, {"image": "data"},
                                              lambda *args: "uploaded.png", image_sizes={"image": size})
                node = result["105:104"]["inputs"]
                self.assertEqual((node["width"], node["height"]), expected)
                self.assertEqual(graph, original)
                self.assertEqual(result["115"], original["115"])
                self.assertEqual(result["114"]["inputs"]["image"], "uploaded.png")

    def test_explicit_size_takes_precedence_over_first_frame(self):
        graph, mapping = h3_fixture()
        result = prepare_mapped_graph(graph, mapping, {"image": "data", "width": 1024, "height": 576},
                                      lambda *args: "uploaded.png", image_sizes={"image": (800, 800)})
        self.assertEqual(result["105:104"]["inputs"]["width"], 1024)
        self.assertEqual(result["105:104"]["inputs"]["height"], 576)

    def test_bad_dimensions_are_rejected_before_upload(self):
        graph, mapping = h3_fixture()
        for body in ({"width": 800}, {"width": True, "height": 640}, {"width": 801, "height": 640},
                     {"width": 0, "height": 640}, {"width": 16384, "height": 640}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                prepare_mapped_graph(graph, mapping, {"image": "data", **body},
                                     lambda *args: self.fail("invalid request uploaded an image"))

    def test_unrecognized_links_are_not_overridden(self):
        graph, _ = h3_fixture()
        graph["105:104"]["inputs"]["height"] = ["115", 0]
        self.assertIsNone(h3_dimensions(graph))
