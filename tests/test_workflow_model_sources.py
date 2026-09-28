import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.core.model_mappings import register_mapping, read_mappings, extra_search_paths, resolve_model_file, find_related_models
from app.core.model_maintenance import model_file_ready, check_model_groups
from app.core.workflow_dependencies import workflow_dependency_report, clear_model_index_cache
from app.core.workflow_model_sources import workflow_model_items
from app.core.model_source_discovery import save_verified_source
from app.gui import main_gateway


class WorkflowModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.addCleanup(clear_model_index_cache)

    def test_saved_source_is_reused_for_unknown_group_without_network(self):
        source = {'url': 'https://huggingface.co/a/b/resolve/'+'a'*40+'/model.safetensors', 'size_bytes': 42, 'sha256': 'b'*64}
        save_verified_source(self.base/'runtime/model_sources.json', 'vae/model.safetensors', source)
        items = workflow_model_items({'dependencies': {'models': [{'name': 'model.safetensors', 'node': 'VAELoader', 'input': '0'}]}}, self.base)
        self.assertEqual(items[0]['sha256'], source['sha256'])
        self.assertEqual(items[0]['path'], 'vae/model.safetensors')

    def test_conflicting_catalog_versions_remain_candidates(self):
        groups = {'a': {'items': [{'path': 'vae/model.safetensors', 'url': 'https://huggingface.co/a/a/resolve/main/model.safetensors'}]},
                  'b': {'items': [{'path': 'vae/model.safetensors', 'url': 'https://huggingface.co/b/b/resolve/main/model.safetensors'}]}}
        with mock.patch('app.core.workflow_model_sources.MODEL_REQUIREMENTS', groups):
            items = workflow_model_items({'dependencies': {'models': [{'name': 'model.safetensors', 'input': 'vae_name'}]}}, self.base)
        self.assertEqual(items[0]['url'], '')
        self.assertEqual(len(items[0]['candidate_urls']), 2)

    def test_editor_graph_without_registered_group_extracts_model_and_author_address(self):
        graph = {'nodes': [{'type': 'VAELoader', 'widgets_values': ['model.safetensors'],
                           'properties': {'models': [{'name': 'model.safetensors', 'directory': 'vae',
                                                     'url': 'https://huggingface.co/author/repo/resolve/main/model.safetensors'}]}}]}
        result = workflow_model_items({'dependencies': {'models': []}}, self.base, graph)
        self.assertEqual(result, [{'path': 'vae/model.safetensors', 'url': 'https://huggingface.co/author/repo/resolve/main/model.safetensors'}])

    def test_template_subgraph_metadata_resolves_custom_workflow(self):
        template_dir = self.base / 'runtime/python/Lib/site-packages/comfyui_workflow_templates_json/templates'
        template_dir.mkdir(parents=True)
        (template_dir / 'example.json').write_text(json.dumps({"definitions": {"subgraphs": [{"nodes": [{"properties": {"models": [{"name": "test.safetensors", "url": "https://huggingface.co/author/repo/resolve/main/test.safetensors", "directory": "vae"}]}}]}]}}))
        workflow = {"dependencies": {"models": [{"name": "test.safetensors", "input": "vae_name", "node": "VAELoader"}]}}
        items = workflow_model_items(workflow, self.base)
        self.assertEqual(items, [{"path": "vae/test.safetensors", "url": "https://huggingface.co/author/repo/resolve/main/test.safetensors"}])

    def test_explicit_source_is_preserved_and_unknown_source_keeps_local_mapping(self):
        workflow = {"dependencies": {"models": [{"name": "foo.safetensors", "input": "unet_name", "url": "https://example.com/foo.safetensors"}, {"name": "bar.gguf", "input": "clip_name"}]}}
        items = workflow_model_items(workflow, self.base)
        self.assertEqual(items[0]["url"], "https://example.com/foo.safetensors")
        self.assertEqual(items[0]["path"], "diffusion_models/foo.safetensors")
        self.assertEqual(items[1], {"path": "text_encoders/bar.gguf", "url": ""})

    def test_unsafe_destination_is_not_used(self):
        workflow = {"dependencies": {"models": [{"name": "x.safetensors", "directory": "../../outside", "url": "file:///secret"}]}}
        self.assertEqual(workflow_model_items(workflow, self.base), [])

    def test_mapping_preserves_external_file_and_feeds_checks_and_comfy_paths(self):
        models = self.base / 'models'
        source = self.base / '其他位置' / 'test.safetensors'
        source.parent.mkdir()
        source.write_bytes(b'testmodel')
        register_mapping(models, 'vae/test.safetensors', source, expected_size=9)
        self.assertFalse((models / 'vae/test.safetensors').exists())
        self.assertEqual(source.read_bytes(), b'testmodel')
        self.assertEqual(resolve_model_file(models / 'vae/test.safetensors'), source)
        self.assertTrue(model_file_ready(models / 'vae/test.safetensors', 9))
        self.assertEqual(extra_search_paths(models), [('vae', source.parent.as_posix())])
        deps = {"models": ["test.safetensors"]}
        self.assertEqual(workflow_dependency_report(deps, models, cache_seconds=0)["missing_models"], [])
        requirements = {"test": {"items": [{"path": "vae/test.safetensors", "size_bytes": 9}]}}
        self.assertEqual(check_model_groups(models, requirements)["test"], "完整")
        with mock.patch.object(main_gateway, 'BASE_DIR', self.base), mock.patch.object(main_gateway, '_models_dir', return_value=models):
            main_gateway._ensure_extra_model_paths()
        yaml_text = (self.base / 'runtime/ComfyUI/extra_model_paths.yaml').read_text(encoding='utf-8')
        self.assertIn('lingjing_local_0:', yaml_text)
        self.assertIn(json.dumps(source.parent.as_posix(), ensure_ascii=False), yaml_text)
        source.unlink()
        self.assertFalse(model_file_ready(models / 'vae/test.safetensors'))
        self.assertEqual(workflow_dependency_report(deps, models, cache_seconds=0)["missing_models"], ["test.safetensors"])
        source.mkdir()
        self.assertEqual(workflow_dependency_report(deps, models, cache_seconds=0)["missing_models"], ["test.safetensors"])

    def test_mapping_rejects_wrong_file_size_traversal_and_existing_target(self):
        models = self.base / 'models'
        source = self.base / 'test.safetensors'
        source.write_bytes(b'123')
        for relative, size in [('vae/../test.safetensors', None), ('vae/other.safetensors', None), ('vae/test.safetensors', 4)]:
            with self.assertRaises(ValueError):
                register_mapping(models, relative, source, size)
        (models / 'vae').mkdir(parents=True)
        target = models / 'vae/test.safetensors'
        target.write_bytes(b'existing')
        with self.assertRaises(ValueError):
            register_mapping(models, 'vae/test.safetensors', source)
        self.assertEqual(target.read_bytes(), b'existing')
        self.assertEqual(read_mappings(models), {})

    def test_related_scan_checks_current_and_parent_categories_without_recursing(self):
        selected = self.base / 'shared/loras/start.safetensors'
        selected.parent.mkdir(parents=True)
        selected.write_bytes(b'lora')
        nearby = selected.parent / 'vae/current.safetensors'
        sibling = selected.parent.parent / 'vae/sibling.safetensors'
        outside = selected.parent.parent / 'archive/deep/vae/hidden.safetensors'
        for path in (nearby, sibling, outside):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'valid')
        items = [{'path': 'vae/' + name + '.safetensors', 'size_bytes': 5} for name in ('current', 'sibling', 'hidden')]
        found = find_related_models(selected, items, self.base / 'models')
        self.assertEqual([match['source'] for match in found], [nearby, sibling])
        self.assertEqual(read_mappings(self.base / 'models'), {}, 'scan must not map before confirmation')
        register_mapping(self.base / 'models', items[0]['path'], nearby)
        self.assertEqual(len(find_related_models(selected, items, self.base / 'models')), 1)

    def test_rejecting_related_models_suppresses_scan_for_the_same_session(self):
        app = object.__new__(main_gateway.GatewayApp)
        app._model_download_owner = lambda control: control
        app._apply_local_model_mapping = mock.Mock()
        control = {'item': {'path': 'vae/test.safetensors'}, 'state': 'idle'}
        session = {'scan_disabled': False, 'controls': [control]}
        control['selection_session'] = session
        found = [{'item': control['item'], 'source': self.base / 'test.safetensors'}]
        with mock.patch('app.core.model_mappings.find_related_models', return_value=found) as scan, mock.patch.object(main_gateway.messagebox, 'askyesno', return_value=False) as confirm:
            self.assertEqual(app._offer_related_model_mappings(control, self.base / 'a.safetensors'), 0)
            self.assertEqual(app._offer_related_model_mappings(control, self.base / 'b.safetensors'), 0)
            scan.assert_called_once()
            confirm.assert_called_once()
        app._apply_local_model_mapping.assert_not_called()
        self.assertTrue(session['scan_disabled'])
        session['scan_disabled'] = False
        with mock.patch('app.core.model_mappings.find_related_models', return_value=found), mock.patch.object(main_gateway.messagebox, 'askyesno', return_value=True):
            self.assertEqual(app._offer_related_model_mappings(control, self.base / 'a.safetensors'), 1)
        app._apply_local_model_mapping.assert_called_once_with(control, found[0]['source'])


if __name__ == '__main__':
    unittest.main()
