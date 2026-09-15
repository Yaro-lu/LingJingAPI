import asyncio
import json
import unittest
from unittest import mock

import httpx

from app import server
from app.core.workflow_adaptation import MAPPING_KEYS, analyze_workflow, make_mapping
import test_image_compatibility as compatibility
from test_image_compatibility import FakeComfyUIClient, asgi_request
from test_workflow_adaptation import example_graph


class WorkflowAdaptationAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = compatibility.ImageCompatibilityTests.asyncSetUp
    asyncTearDown = compatibility.ImageCompatibilityTests.asyncTearDown

    def install_mapping(self, fields=None):
        workflow = server.registry.workflows[0]
        graph = example_graph()
        (workflow.folder / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
        mapping = analyze_workflow(graph) if fields is None else make_mapping(graph, fields, "image")
        workflow.api_mapping = {k: mapping[k] for k in MAPPING_KEYS}
        workflow.input_schema = mapping["input_schema"]
        return workflow, graph

    async def test_summary_is_names_only_and_does_not_load_payload(self):
        with mock.patch.object(server, "_workflow_payload", side_effect=AssertionError("summary must be cheap")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://client.example") as client:
                response = await client.get("/v1/workflows?summary=true", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["workflows"]), len(server.registry.workflows))
        self.assertTrue(all(set(w) == {"id", "name"} for w in response.json()["workflows"]))

    async def test_available_summary_hides_unavailable_workflows_without_sending_schemas(self):
        selected = server.registry.workflows[0]
        with mock.patch.object(server, "_workflow_payload", side_effect=lambda w: {"available": w.id == selected.id}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://client.example") as client:
                response = await client.get("/v1/workflows?summary=true&available_only=true", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workflows"], [{"id": selected.id, "name": selected.name}])
        with mock.patch.object(server, "_workflow_payload", return_value={"available": False}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://client.example") as client:
                response = await client.get("/v1/workflows?summary=true&available_only=true", headers=self.auth)
        self.assertEqual(response.json()["workflows"], [])

    async def test_detail_returns_only_selected_public_parameters(self):
        workflow, _ = self.install_mapping()
        with mock.patch.object(server, "_installed_comfy_node_types", return_value=set()):
            status, _, raw = await asgi_request(self.app, "GET", f"/v1/workflows/{workflow.id}/schema", headers=self.auth)
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["id"], workflow.id)
        self.assertEqual(payload["api_mapping_status"], "ready")
        self.assertNotIn("api_bindings", payload)
        self.assertNotIn("workflow_json", payload)
        self.assertIn("cfg", [f["name"] for f in payload["input_schema"]["inputs"]])

    async def test_mapped_submission_sets_only_requested_fields(self):
        workflow, graph = self.install_mapping()
        status, _, _ = await asgi_request(self.app, "POST", f"/v1/workflows/run/{workflow.id}", headers=self.auth,
                                         json_body={"prompt": "new prompt", "cfg": 0})
        self.assertEqual(status, 200)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["1"]["inputs"]["text"], "new prompt")
        self.assertEqual(queued["2"]["inputs"]["text"], graph["2"]["inputs"]["text"])
        self.assertEqual(queued["3"]["inputs"]["cfg"], 0)
        self.assertEqual(queued["3"]["inputs"]["seed"], 0)

    async def test_zero_parameters_do_not_enter_legacy_injection(self):
        workflow, graph = self.install_mapping([])
        status, _, _ = await asgi_request(self.app, "POST", f"/{workflow.id}", headers=self.auth, json_body={})
        self.assertEqual(status, 202)
        self.assertEqual(FakeComfyUIClient.queued_workflows[-1], graph)

    async def test_image_compatibility_does_not_add_unrequested_mapped_defaults(self):
        workflow, graph = self.install_mapping()
        status, _, _ = await asgi_request(self.app, "POST", "/api/v3/images/generations",
                                         headers={**self.auth, "prefer": "respond-async"},
                                         json_body={"model": workflow.id, "prompt": "new"})
        self.assertEqual(status, 202)
        queued = FakeComfyUIClient.queued_workflows[-1]
        self.assertEqual(queued["2"]["inputs"]["text"], graph["2"]["inputs"]["text"])
        self.assertEqual(queued["3"]["inputs"]["seed"], 0)

    async def test_stale_or_manual_mapping_rejected_before_queue_and_releases_slot(self):
        workflow, graph = self.install_mapping()
        graph["1"]["inputs"]["text"] = "changed"
        (workflow.folder / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
        status, _, raw = await asgi_request(self.app, "POST", f"/v1/workflows/run/{workflow.id}", headers=self.auth, json_body={})
        self.assertEqual(status, 422)
        self.assertIn("映射失效", json.loads(raw)["detail"])
        self.assertEqual(FakeComfyUIClient.queued_workflows, [])
        self.assertEqual(server.current_task, {})

    async def test_detail_requires_key_and_unknown_workflow_is_404(self):
        status, _, _ = await asgi_request(self.app, "GET", "/v1/workflows/unknown/schema")
        self.assertEqual(status, 401)
        status, _, _ = await asgi_request(self.app, "GET", "/v1/workflows/unknown/schema", headers=self.auth)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
