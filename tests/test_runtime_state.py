import json
import tempfile
import unittest
from pathlib import Path

from app.core.secret_store import protect_text, unprotect_text
from app.core.runtime_state import RuntimeState


class RuntimeStateTests(unittest.TestCase):
    def test_start_session_publishes_the_requested_loopback_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            state = RuntimeState(runtime_dir)

            state.start_session(19001)

            saved = json.loads(
                (runtime_dir / "session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state.local_api, "http://127.0.0.1:19001")
            self.assertEqual(saved["local_api"], "http://127.0.0.1:19001")

    def test_access_key_persists_and_offline_clears_public_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            state = RuntimeState(runtime_dir)
            state.start_session()
            state.set_api_key("sk-local-test_1234567890")
            state.set_online("https://example.trycloudflare.com")
            state.set_offline()

            saved = json.loads((runtime_dir / "session.json").read_text(encoding="utf-8"))
            self.assertNotIn("api_key", saved)
            self.assertNotIn("sk-local-test_1234567890", json.dumps(saved))
            self.assertTrue(saved["api_key_protected"].startswith("dpapi:"))
            self.assertEqual(state.api_key, "sk-local-test_1234567890")
            self.assertTrue(state.admin_key.startswith("sk-admin-"))
            self.assertEqual(saved["status"], "offline")
            self.assertEqual(saved["base_url"], "")

    def test_stale_api_instance_cannot_overwrite_new_access_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            api_state = RuntimeState(runtime_dir)
            api_state.start_session()
            api_state.set_api_key("sk-local-old_1234567890")
            gui_state = RuntimeState(runtime_dir)

            gui_state.set_api_key("sk-local-new_1234567890")
            api_state.set_offline()

            saved = json.loads((runtime_dir / "session.json").read_text(encoding="utf-8"))
            self.assertNotIn("api_key", saved)
            self.assertEqual(RuntimeState(runtime_dir).api_key, "sk-local-new_1234567890")
            self.assertEqual(saved["status"], "offline")

    def test_stale_snapshot_save_cannot_overwrite_new_access_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            stale_state = RuntimeState(runtime_dir)
            stale_state.start_session()
            stale_state.set_api_key("sk-local-old_1234567890")
            current_state = RuntimeState(runtime_dir)

            current_state.set_api_key("sk-local-new_1234567890")
            stale_state.save()

            self.assertEqual(RuntimeState(runtime_dir).api_key, "sk-local-new_1234567890")

    def test_legacy_plaintext_key_is_migrated_on_next_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            runtime_dir.joinpath("session.json").write_text(
                json.dumps({"api_key": "sk-local-legacy", "status": "offline"}),
                encoding="utf-8",
            )

            state = RuntimeState(runtime_dir)
            self.assertEqual(state.api_key, "sk-local-legacy")
            state.set_offline()

            saved = json.loads((runtime_dir / "session.json").read_text(encoding="utf-8"))
            self.assertNotIn("api_key", saved)
            self.assertEqual(unprotect_text(saved["api_key_protected"]), "sk-local-legacy")

    def test_windows_secret_round_trip_does_not_store_plaintext(self):
        protected = protect_text("sensitive-session-token")

        self.assertNotIn("sensitive-session-token", protected)
        self.assertEqual(unprotect_text(protected), "sensitive-session-token")


if __name__ == "__main__":
    unittest.main()
