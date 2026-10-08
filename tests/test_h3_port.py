import json
import re
import tempfile
import unittest
from pathlib import Path

from app.core.h3_dimensions import h3_dimensions
from app.core.model_maintenance import MODEL_REQUIREMENTS
from app.core.workflow_adaptation import graph_hash, infer_output_type, prepare_mapped_graph, video_timing
from app.core.workflow_capability import infer_capability
from app.core.workflow_dependencies import extract_workflow_dependencies
from app.core.workflow_model_sources import workflow_model_items
from app.workflow_registry import read_local_workflow_catalog

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    'lingjing_h3_3060ti_regular_it2v': '性价比H3',
    'lingjing_h3_4070_hq_it2v': '高质量H3',
}


class H3PortTests(unittest.TestCase):
    def fixtures(self):
        for identifier, name in WORKFLOWS.items():
            folder = ROOT / 'workflows' / identifier
            yield name, json.loads((folder/'manifest.json').read_text('utf-8')), json.loads((folder/'workflow.json').read_text('utf-8'))

    def test_graph_type_bindings_and_four_dependency_roles(self):
        for name, manifest, graph in self.fixtures():
            with self.subTest(name=name):
                self.assertEqual(manifest['name'], name)
                self.assertEqual(manifest['api_graph_hash'], graph_hash(graph))
                self.assertEqual(infer_output_type(graph), 'video')
                self.assertEqual(infer_capability(manifest), 'image_to_video')
                detected = {i['name'] for i in extract_workflow_dependencies(graph)['models']}
                specs = MODEL_REQUIREMENTS[manifest['model_group']]['items']
                self.assertEqual(detected, {Path(i['path']).name for i in specs})
                self.assertEqual(len(specs), 4)
                self.assertEqual(sorted(i['path'].split('/')[0] for i in specs), ['diffusion_models', 'text_encoders', 'vae', 'vae'])
                for item in specs:
                    self.assertRegex(item['url'], r'/resolve/[0-9a-f]{40}/')
                    self.assertRegex(item['sha256'], r'^[0-9a-f]{64}$')
                    self.assertGreater(item['size_bytes'], 0)
                with tempfile.TemporaryDirectory() as temporary:
                    resolved = workflow_model_items(manifest, temporary)
                self.assertEqual({i['path'] for i in resolved}, {i['path'] for i in specs})

    def test_actual_h3_node_uses_reference_budget_and_manual_override(self):
        for name, manifest, graph in self.fixtures():
            with self.subTest(name=name):
                dimensions = h3_dimensions(graph)
                node_id = dimensions['node_id']
                body = {'prompt': '小猫向前走', 'image': 'reference'}
                result = prepare_mapped_graph(graph, manifest, body, lambda *_: 'uploaded.png', {'image': (800, 800)})
                self.assertEqual(result[node_id]['inputs']['width'], result[node_id]['inputs']['height'])
                self.assertEqual(result[node_id]['inputs']['width'] % 32, 0)
                area = result[node_id]['inputs']['width'] ** 2
                self.assertLess(abs(area / (dimensions['width']*dimensions['height']) - 1), .12)
                result = prepare_mapped_graph(graph, manifest, {**body, 'width': 640, 'height': 384}, lambda *_: 'uploaded.png', {'image': (800, 800)})
                self.assertEqual((result[node_id]['inputs']['width'], result[node_id]['inputs']['height']), (640, 384))
                fallback = prepare_mapped_graph(graph, manifest, body, lambda *_: 'uploaded.png')
                self.assertEqual(fallback[node_id]['inputs']['width'], graph[node_id]['inputs']['width'])
                with self.assertRaises(ValueError):
                    prepare_mapped_graph(graph, manifest, {**body, 'width': 641, 'height': 384}, lambda *_: 'uploaded.png')
                self.assertGreater(video_timing(graph)['duration'], 0)

    def test_catalog_lists_two_h3_workflows_and_excludes_legacy_fixture(self):
        records = read_local_workflow_catalog(ROOT/'workflows')
        self.assertTrue(set(WORKFLOWS).issubset({w['id'] for w in records}))
        self.assertNotIn('video_minimax_h3_i2v', {w['id'] for w in records})
        self.assertTrue((ROOT/'tests/fixtures/h3_legacy/workflow.json').is_file())


if __name__ == '__main__':
    unittest.main()
