import unittest
from unittest import mock

from app.engines.comfyui_client import ComfyUIClient


class ComfyUIClientTimeoutTests(unittest.TestCase):
    def test_failed_history_is_not_success(self):
        client = ComfyUIClient()
        with mock.patch.object(client, "get_queue_status", return_value={}), mock.patch.object(
            client, "get_history", return_value={"p": {"status": {
                "status_str": "error", "messages": [["execution_error", {
                    "node_type": "TextGenerate", "exception_message": "HostBuffer.read_file_slice failed"}]]}}}
        ):
            result = client.get_progress("p")
        self.assertEqual(result["status"], "failed")
        self.assertIn("HostBuffer.read_file_slice failed", result["error"])

    def test_preview_any_text_is_returned(self):
        result = ComfyUIClient().get_output_files({"outputs": {"5": {"text": ["你好"]}}})
        self.assertEqual(result[0]["text"], "你好")

    def test_history_requests_have_a_finite_timeout(self):
        response = mock.Mock()
        response.json.return_value = {}
        with mock.patch(
            "app.engines.comfyui_client.requests.get", return_value=response
        ) as get:
            ComfyUIClient().get_history("prompt-test")

        get.assert_called_once_with(
            "http://127.0.0.1:8188/history/prompt-test", timeout=30
        )

    def test_queue_requests_have_a_finite_timeout(self):
        response = mock.Mock()
        response.json.return_value = {}
        with mock.patch(
            "app.engines.comfyui_client.requests.get", return_value=response
        ) as get:
            ComfyUIClient().get_queue_status()

        get.assert_called_once_with("http://127.0.0.1:8188/queue", timeout=30)

    def test_cancel_removes_only_matching_pending_prompt(self):
        client = ComfyUIClient()
        response = mock.Mock()
        with mock.patch.object(client, "get_queue_status", side_effect=[{
            "queue_pending": [[1, "other-prompt"], [2, "target-prompt"]],
            "queue_running": [],
        }, {"queue_pending": [[1, "other-prompt"]], "queue_running": []}]), mock.patch(
            "app.engines.comfyui_client.requests.post", return_value=response
        ) as post:
            client.cancel_prompt("target-prompt")
        post.assert_called_once_with(
            "http://127.0.0.1:8188/queue", json={"delete": ["target-prompt"]}, timeout=5,
        )

    def test_cancel_interrupts_matching_running_prompt(self):
        client = ComfyUIClient()
        response = mock.Mock()
        with mock.patch.object(client, "get_queue_status", return_value={
            "queue_pending": [], "queue_running": [[1, "target-prompt"]],
        }), mock.patch("app.engines.comfyui_client.requests.post", return_value=response) as post:
            client.cancel_prompt("target-prompt")
        post.assert_called_once_with(
            "http://127.0.0.1:8188/interrupt", json={"prompt_id": "target-prompt"}, timeout=5,
        )

    def test_cancel_interrupts_prompt_that_started_while_deleting(self):
        client = ComfyUIClient()
        response = mock.Mock()
        with mock.patch.object(client, "get_queue_status", side_effect=[
            {"queue_pending": [[1, "target-prompt"]], "queue_running": []},
            {"queue_pending": [], "queue_running": [[1, "target-prompt"]]},
        ]), mock.patch("app.engines.comfyui_client.requests.post", return_value=response) as post:
            client.cancel_prompt("target-prompt")
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            "http://127.0.0.1:8188/queue", "http://127.0.0.1:8188/interrupt",
        ])

    def test_input_image_upload_uses_comfy_contract_and_returns_load_image_name(self):
        response = mock.Mock(ok=True)
        response.json.return_value = {
            "name": "frame.png",
            "subfolder": "lingjing",
            "type": "input",
        }
        with mock.patch(
            "app.engines.comfyui_client.requests.post", return_value=response
        ) as post:
            name = ComfyUIClient().upload_input_image(
                b"png-data", "frame.png", "image/png"
            )

        self.assertEqual(name, "lingjing/frame.png")
        post.assert_called_once_with(
            "http://127.0.0.1:8188/upload/image",
            files={"image": ("frame.png", b"png-data", "image/png")},
            data={"type": "input", "overwrite": "true"},
            timeout=60,
        )

    def test_input_image_upload_rejects_traversal_from_comfy(self):
        response = mock.Mock(ok=True)
        response.json.return_value = {"name": "../frame.png", "subfolder": ""}
        with mock.patch(
            "app.engines.comfyui_client.requests.post", return_value=response
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid filename"):
                ComfyUIClient().upload_input_image(b"x", "frame.png", "image/png")


if __name__ == "__main__":
    unittest.main()
