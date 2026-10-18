import base64
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from noha import admin, metrics
from noha.config import Network, Voice
from noha.dashboard import Dashboard, is_recovery, make_app
from noha.live import LivePlayback, start_event
from noha.models import model_info
from noha.passwords import password_record, verify_password
from noha.realtime import record_usage


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_waits_for_conversation_then_applies_and_can_recover(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            personality = root / "personality.txt"
            personality.write_text("Original", encoding="utf-8")
            config = root / "noha.toml"
            original = '[voice]\ninstructions = ' + json.dumps(str(personality)) + '\nmodel = "gpt-realtime-1.5"\nvoice = "marin"\n[motion]\ngpio = 3\n'
            config.write_text(original, encoding="utf-8")
            (root / "secrets.env").write_text("OPENAI_API_KEY=original-key\n")
            stack.enter_context(patch.object(admin, "ETC", root))
            stack.enter_context(patch.object(admin, "STATE", root))
            stack.enter_context(patch.object(admin, "voice_runtime", return_value=runtime))
            stack.enter_context(patch.object(admin, "update_lock", side_effect=lambda: nullcontext()))
            stack.enter_context(patch.object(admin.os, "chown", create=True))
            stack.enter_context(patch.object(admin, "validate_gpio", new=AsyncMock()))
            service = stack.enter_context(patch.object(admin, "user_systemctl", new=AsyncMock()))
            voice = stack.enter_context(patch.object(admin, "voice_status", return_value={"stale": False, "busy": True}))
            helper = admin.Admin()
            await helper.save_settings({"model": "gpt-live-1", "voice": "marin", "gpio": 4, "instructions": "Nova personalidade", "openai_key": "new-openai-key"})
            await helper.apply_pending()
            service.assert_not_called()
            self.assertEqual(config.read_text(), original)
            self.assertTrue((runtime / "config-pending").exists())
            voice.return_value = {"stale": False, "busy": False, "updated": 10**15, "model": "gpt-live-1", "gpio": 4}
            await helper.apply_pending()
            self.assertEqual(personality.read_text(encoding="utf-8"), "Nova personalidade")
            self.assertIn('model = "gpt-live-1"', config.read_text())
            self.assertFalse((runtime / "config-pending").exists())
            self.assertEqual(admin.read_json(root / "config-result.json")["state"], "applied")
            self.assertTrue(list((root / "backups").rglob("secrets.env")))
            # A power interruption after file replacement leaves a durable transaction.
            admin.atomic_write(root / "config-transaction.json", {str(personality): {"text": "Original", "mode": 0o644}})
            await helper.recover_config()
            self.assertEqual(personality.read_text(), "Original")
            self.assertEqual(admin.read_json(root / "config-result.json")["state"], "error")

    async def test_login_csrf_password_revision_and_throttle(self):
        revision = ["first"]
        async def helper(self, app, operation, method="GET", data=None):
            if operation == "login":
                return (200, {"revision": revision[0]}) if data.get("password") == "admin" else (401, {"error": "Senha inválida"})
            if operation == "auth":
                return 200, {"revision": revision[0]}
            return 200, {"ngrok_url": "https://example.ngrok-free.app"}
        app = make_app(Network())
        app.cleanup_ctx.clear()
        with patch.object(Dashboard, "call", helper):
            async with TestClient(TestServer(app)) as client:
                denied = await client.get("/api/status")
                self.assertEqual(denied.status, 401)
                origin = {"Origin": str(client.make_url("/")).rstrip("/")}
                bad_origin = await client.post("/api/login", json={"password": "admin"}, headers={"Origin": "https://evil.test"})
                self.assertEqual(bad_origin.status, 403)
                login = await client.post("/api/login", json={"password": "admin"}, headers=origin)
                self.assertEqual(login.status, 200)
                csrf = (await login.json())["csrf"]
                denied = await client.post("/api/settings", json={}, headers=origin)
                self.assertEqual(denied.status, 403)
                allowed = await client.post("/api/settings", json={}, headers={**origin, "X-Noha-Token": csrf})
                self.assertEqual(allowed.status, 200)
                revision[0] = "changed-through-cli"
                self.assertEqual((await client.get("/api/status")).status, 401)
                for _ in range(5):
                    self.assertEqual((await client.post("/api/login", json={"password": "wrong"}, headers=origin)).status, 401)
                self.assertEqual((await client.post("/api/login", json={"password": "admin"}, headers=origin)).status, 429)

    async def test_hotspot_dispatch_uses_socket_destination_not_host(self):
        def request(local, host):
            return SimpleNamespace(headers={"Host": host}, transport=SimpleNamespace(get_extra_info=lambda _: (local, 80)))
        self.assertTrue(is_recovery(request("192.168.50.1", "noha.local"), "192.168.50.1"))
        self.assertTrue(is_recovery(request("::ffff:192.168.50.1", "public.ngrok-free.app"), "192.168.50.1"))
        self.assertFalse(is_recovery(request("192.168.1.5", "192.168.50.1"), "192.168.50.1"))
        self.assertFalse(is_recovery(request("127.0.0.1", "192.168.50.1"), "192.168.50.1"))


class DataTests(unittest.TestCase):
    def test_password_hashes_and_secret_free_settings(self):
        password = password_record("admin")
        self.assertTrue(verify_password("admin", password))
        self.assertFalse(verify_password("other", password))
        self.assertNotEqual(password["salt"], password_record("admin")["salt"])
        with TemporaryDirectory() as folder:
            root = Path(folder)
            personality = root / "personality.txt"
            personality.write_text("Olá", encoding="utf-8")
            (root / "noha.toml").write_text('[voice]\ninstructions = ' + json.dumps(str(personality)) + '\n', encoding="utf-8")
            (root / "secrets.env").write_text("OPENAI_API_KEY=sk-private-value\n")
            (root / "ngrok.json").write_text(json.dumps({"authtoken": "private-ngrok-token", "url": "https://example.ngrok-free.app"}))
            (root / "config-pending.json").write_text(json.dumps({"model": "gpt-live-1", "openai_key": "new-secret"}))
            with patch.object(admin, "ETC", root), patch.object(admin, "STATE", root):
                settings = admin.get_settings()
            payload = json.dumps(settings)
            for secret in ("sk-private-value", "private-ngrok-token", "new-secret"):
                self.assertNotIn(secret, payload)
            self.assertTrue(settings["openai_key_configured"])
            self.assertTrue(settings["ngrok_token_configured"])

    def test_metrics_persist_and_distinguish_unreported_counters(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"NOHA_METRICS_DB": str(Path(folder) / "usage.db")}):
            metrics.record("gpt-live-1", conversations=1, duration_seconds=30)
            metrics.record("gpt-realtime-1.5", conversations=1, input_tokens=20, output_tokens=5, cached_tokens=0)
            result = metrics.summary(7)
            self.assertEqual(result["totals"]["conversations"], 2)
            live = next(row for row in result["models"] if row["model"] == "gpt-live-1")
            self.assertIsNone(live["input_tokens"])
            self.assertEqual(result["totals"]["cached_tokens"], 0)

    def test_model_adapters_use_their_own_protocol_and_usage(self):
        self.assertEqual(model_info("gpt-live-1").transport, "live")
        with self.assertRaises(ValueError):
            model_info("gpt-4o-realtime-preview")
        event = start_event(Voice(model="gpt-live-1"), "Instruções locais", True)
        self.assertEqual(event["type"], "session.start")
        self.assertEqual(event["session"]["audio"]["format"]["rate"], 24000)
        self.assertEqual(event["session"]["input"][0]["role"], "developer")
        self.assertFalse(event["session"]["store"])
        audio = SimpleNamespace(push=lambda data: chunks.append(data), flush=lambda: chunks.clear())
        chunks = []
        playback = LivePlayback(audio)
        playback.event({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x00\x00").decode()})
        self.assertEqual(chunks, [b"\x00\x00"])
        playback.last_user -= 5
        playback.event({"type": "session.input_transcript.delta", "delta": "private transcript"})
        self.assertEqual(chunks, [])
        self.assertFalse(hasattr(playback, "transcript"))
        with patch("noha.realtime.record") as record:
            record_usage("gpt-realtime-1.5", {"input_tokens": 4, "output_tokens": 6})
            record.assert_called_once_with("gpt-realtime-1.5", input_tokens=4, output_tokens=6)


if __name__ == "__main__":
    unittest.main()
