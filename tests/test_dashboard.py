import base64
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from noha import admin, metrics
from noha.config import Config, Network, Voice
from noha.dashboard import SESSIONS, Dashboard, is_recovery, make_app
from noha.live import LivePlayback, start_event
from noha.models import model_info
from noha.passwords import password_record, verify_password
from noha.realtime import record_usage


class AdminSettingsTests(unittest.IsolatedAsyncioTestCase):
    def robot(self, stack, root, toml):
        runtime = root / "runtime"
        runtime.mkdir()
        personality = root / "personality.txt"
        personality.write_text("Original", encoding="utf-8")
        config = root / "noha.toml"
        config.write_text('[voice]\ninstructions = ' + json.dumps(str(personality)) + "\n" + toml, encoding="utf-8")
        (root / "secrets.env").write_text("OPENAI_API_KEY=original-key\n")
        stack.enter_context(patch.object(admin, "ETC", root))
        stack.enter_context(patch.object(admin, "STATE", root))
        stack.enter_context(patch.object(admin, "voice_runtime", return_value=runtime))
        stack.enter_context(patch.object(admin, "update_lock", side_effect=lambda: nullcontext()))
        stack.enter_context(patch.object(admin.os, "chown", create=True))
        stack.enter_context(patch.object(admin, "validate_gpio", new=AsyncMock()))
        return runtime, personality, config

    async def test_changes_wait_for_the_visit_then_apply_confirmed_by_digest(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            runtime, personality, config = self.robot(stack, root, 'model = "gpt-realtime-1.5" # chosen\nvoice = "marin"\n[motion]\ngpio = 3\n')
            service = stack.enter_context(patch.object(admin, "user_systemctl", new=AsyncMock()))
            busy = {"stale": False, "busy": True}
            voice = stack.enter_context(patch.object(admin, "voice_status", return_value=busy))
            helper = admin.Admin()
            await helper.save_settings({"changes": {"voice.model": "gpt-live-1", "personality": "Nova personalidade"}})
            # A second save while waiting merges with the first; secrets never return to the browser.
            saved = await helper.save_settings({"changes": {"motion.gpio": 4, "openai_key": "new-openai-key", "greeting.texts": ["Oi!"]}})
            self.assertEqual(saved["pending"]["openai_key"], True)
            self.assertEqual(set(saved["pending"]), {"voice.model", "personality", "motion.gpio", "openai_key", "greeting.texts"})
            await helper.apply_pending()
            service.assert_not_called()  # a visit is in progress
            self.assertTrue((runtime / "config-pending").exists())
            voice.side_effect = lambda: {"stale": False, "busy": False, "updated": 10**15,
                                         "config_digest": Config.load(config).digest}
            await helper.apply_pending()
            text = config.read_text()
            self.assertIn('model = "gpt-live-1" # chosen', text)  # comments survive
            self.assertIn("[greeting]", text)
            self.assertEqual(Config.load(config).greeting.texts, ["Oi!"])
            self.assertEqual(personality.read_text(encoding="utf-8"), "Nova personalidade")
            self.assertIn("OPENAI_API_KEY=new-openai-key", (root / "secrets.env").read_text())
            self.assertFalse((runtime / "config-pending").exists())
            self.assertEqual(admin.read_json(root / "config-result.json")["state"], "applied")
            self.assertTrue(list((root / "backups").rglob("secrets.env")))
            # A power interruption after file replacement leaves a durable transaction.
            admin.atomic_write(root / "config-transaction.json", {str(personality): {"text": "Original", "mode": 0o644}})
            await helper.recover_config()
            self.assertEqual(personality.read_text(), "Original")
            self.assertEqual(admin.read_json(root / "config-result.json")["state"], "error")

    async def test_invalid_changes_are_rejected_with_the_field_name(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            self.robot(stack, root, '[motion]\ngpio = 3\n')
            helper = admin.Admin()
            for changes, message in (({"motion.confirm_pulses": 0}, "Detecções para confirmar"),
                                     ({"voice.voice": "inexistente"}, "Voz incompatível"),
                                     ({"nada": 1}, "Campo desconhecido"), ({}, "Nenhuma alteração")):
                with self.assertRaisesRegex(ValueError, message):
                    await helper.save_settings({"changes": changes})
            self.assertFalse((root / "config-pending.json").exists())

    async def test_discard_removes_pending_changes(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            runtime, _, _ = self.robot(stack, root, '[motion]\ngpio = 3\n')
            helper = admin.Admin()
            await helper.save_settings({"changes": {"voice.daily_budget_usd": 5}})
            (runtime / "config-pending").touch()
            await helper.discard_pending()
            self.assertFalse((root / "config-pending.json").exists())
            self.assertFalse((runtime / "config-pending").exists())
            self.assertEqual(admin.read_json(root / "config-result.json")["state"], "discarded")

    async def test_audio_change_switches_echo_cancel_and_recovers(self):
        mic = "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_1-00.analog-stereo"
        hdmi = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"
        chip = "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_1-00.analog-stereo"
        with TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            _, _, config = self.robot(stack, root, '[motion]\ngpio = 3\n[audio]\nmicrophone = ' + json.dumps(mic)
                                      + '\nspeaker = ' + json.dumps(hdmi) + '\n')
            echo = root / "home/.config/pipewire/pipewire.conf.d/80-noha-echo.conf"
            echo.parent.mkdir(parents=True)
            echo.write_text("old echo config")
            stack.enter_context(patch.object(admin, "account", return_value=SimpleNamespace(pw_dir=str(root / "home"), pw_uid=0, pw_gid=0)))
            stack.enter_context(patch.object(admin, "audio_devices", new=AsyncMock(return_value=[
                {"name": mic, "class": "Audio/Source"}, {"name": hdmi, "class": "Audio/Sink"}, {"name": chip, "class": "Audio/Sink"}])))
            service = stack.enter_context(patch.object(admin, "user_systemctl", new=AsyncMock()))
            stack.enter_context(patch.object(admin, "voice_status", side_effect=lambda: {
                "stale": False, "busy": False, "updated": 10**15, "audio_ready": True, "config_digest": Config.load(config).digest}))
            helper = admin.Admin()
            with self.assertRaisesRegex(ValueError, "indisponível"):
                await helper.save_settings({"changes": {"audio.speaker": "alsa_output.usb-unplugged.analog-stereo"}})
            with self.assertRaisesRegex(ValueError, "indisponível"):  # a microphone is not a speaker
                await helper.save_settings({"changes": {"audio.speaker": mic}})
            await helper.save_settings({"changes": {"audio.speaker": chip, "audio.echo_cancel": "auto"}})
            await helper.apply_pending()
            self.assertIn(json.dumps(chip), config.read_text())
            self.assertFalse(echo.exists())  # the chip cancels its own output; no second AEC
            service.assert_any_call("restart", *admin.AUDIO_STACK)
            # Interrupted before confirmation: recovery restores the PipeWire file and config.
            admin.atomic_write(root / "config-transaction.json", {str(echo): {"text": "old echo config", "mode": 0o644},
                                                                  str(config): {"text": "restored", "mode": 0o644}})
            await helper.recover_config()
            self.assertEqual(echo.read_text(), "old echo config")
            self.assertEqual(config.read_text(), "restored")


class AdminControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_greeting_touches_the_trigger_only_when_free(self):
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            with patch.object(admin, "voice_runtime", return_value=runtime), \
                    patch.object(admin, "voice_status", return_value={"stale": False, "busy": True}):
                with self.assertRaisesRegex(ValueError, "atendendo"):
                    await admin.Admin().trigger({})
            with patch.object(admin, "voice_runtime", return_value=runtime), \
                    patch.object(admin, "voice_status", return_value={"stale": False, "busy": False}):
                await admin.Admin().trigger({})
            self.assertTrue((runtime / "trigger").exists())

    async def test_end_button_only_when_a_visit_is_running(self):
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            with patch.object(admin, "voice_runtime", return_value=runtime),                     patch.object(admin, "voice_status", return_value={"stale": False, "busy": False}):
                with self.assertRaisesRegex(ValueError, "não está atendendo"):
                    await admin.Admin().stop({})
            with patch.object(admin, "voice_runtime", return_value=runtime),                     patch.object(admin, "voice_status", return_value={"stale": False, "busy": True}):
                await admin.Admin().stop({})
            self.assertTrue((runtime / "stop").exists())

    async def test_volume_uses_the_configured_speaker_node(self):
        helper = admin.Admin()
        calls = []

        async def user_command(*args, **kwargs):
            calls.append(args)
            return "Volume: 0.35 [MUTED]" if args[:2] == ("wpctl", "get-volume") else ""
        with patch.object(admin, "user_command", side_effect=user_command), \
                patch.object(helper, "speaker_node", new=AsyncMock(return_value="57")):
            self.assertEqual(await helper.volume(), {"level": 35, "muted": True})
            await helper.set_volume({"level": 60})
            with self.assertRaises(ValueError):
                await helper.set_volume({"level": 101})
        self.assertIn(("wpctl", "set-volume", "57", "0.60"), calls)
        self.assertIn(("wpctl", "set-mute", "57", "0"), calls)

    async def test_dispatch_maps_errors_to_status_codes(self):
        helper = admin.Admin()
        app = web.Application()
        app.router.add_route("*", "/{operation}", helper.dispatch)
        record = password_record("segredo")
        with TemporaryDirectory() as folder, patch.object(admin, "ETC", Path(folder)):
            admin.atomic_write(Path(folder) / "dashboard-password.json", record)
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.post("/login", json={"password": "errada"})).status, 401)
                ok = await client.post("/login", json={"password": "segredo"})
                self.assertEqual((ok.status, (await ok.json())["default"]), (200, False))
                weak = await client.post("/password", json={"current": "segredo", "password": "admin"})
                self.assertEqual(weak.status, 400)
                self.assertEqual((await client.post("/password", json={"current": "x", "password": "nova-senha"})).status, 403)
                self.assertEqual((await client.get("/nada")).status, 404)


def fake_admin(state):
    async def call(self, app, operation, method="GET", data=None):
        state.setdefault("calls", []).append((method, operation))
        if operation == "login":
            if data.get("password") == "admin" and state.get("default"):
                return 200, {"revision": state["revision"], "default": True}
            return (200, {"revision": state["revision"]}) if data.get("password") == "segredo" else (401, {"error": "Senha inválida"})
        if operation == "auth":
            return 200, {"revision": state["revision"], "default": state.get("default", False)}
        if operation == "kiosk":
            return (200, {"revision": state["revision"]}) if data.get("token") == "local-token" else (401, {"error": "Token inválido"})
        if operation == "settings" and method == "GET":
            return 200, {"ngrok_url": "https://example.ngrok-free.app"}
        if operation == "password":
            return 200, {"accepted": True}
        return 200, {"operation": operation}
    return call


class DashboardServerTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, state):
        app = make_app(Network())
        app.cleanup_ctx.clear()
        self.patcher = patch.object(Dashboard, "call", fake_admin(state))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return app, client, {"Origin": str(client.make_url("/")).rstrip("/")}

    async def test_login_csrf_password_revision_and_throttle(self):
        state = {"revision": "first"}
        app, client, origin = await self.client(state)
        self.assertEqual((await client.get("/api/status")).status, 401)
        self.assertEqual((await client.post("/api/login", json={"password": "segredo"}, headers={"Origin": "https://evil.test"})).status, 403)
        login = await client.post("/api/login", json={"password": "segredo"}, headers=origin)
        self.assertEqual(login.status, 200)
        csrf = (await login.json())["csrf"]
        self.assertEqual((await client.post("/api/settings", json={}, headers=origin)).status, 403)
        self.assertEqual((await client.post("/api/settings", json={}, headers={**origin, "X-Noha-Token": csrf})).status, 200)
        session = await (await client.get("/api/session")).json()
        self.assertEqual((session["scope"], session["must_change"]), ("admin", False))
        state["revision"] = "changed-through-cli"
        self.assertEqual((await client.get("/api/status")).status, 401)
        for _ in range(5):
            self.assertEqual((await client.post("/api/login", json={"password": "wrong"}, headers=origin)).status, 401)
        self.assertEqual((await client.post("/api/login", json={"password": "segredo"}, headers=origin)).status, 429)

    async def test_default_password_works_only_locally_and_must_be_changed(self):
        state = {"revision": "r", "default": True}
        app, client, origin = await self.client(state)
        self.assertTrue((await (await client.get("/api/hello")).json())["default_hint"])
        tunnel = {"X-Forwarded-For": "203.0.113.9"}
        self.assertFalse((await (await client.get("/api/hello", headers=tunnel)).json())["default_hint"])
        denied = await client.post("/api/login", json={"password": "admin"}, headers={**origin, **tunnel})
        self.assertEqual(denied.status, 403)
        login = await client.post("/api/login", json={"password": "admin"}, headers=origin)
        body = await login.json()
        self.assertTrue(body["must_change"])
        blocked = await client.get("/api/status")
        self.assertEqual((blocked.status, (await blocked.json())["code"]), (403, "password_change_required"))
        changed = await client.post("/api/password", json={"current": "admin", "password": "nova-senha"},
                                    headers={**origin, "X-Noha-Token": body["csrf"]})
        self.assertEqual(changed.status, 200)
        self.assertEqual(app[SESSIONS], {})  # every session ends with the password change

    async def test_pi_display_gets_a_read_only_board(self):
        state = {"revision": "r"}
        app, client, origin = await self.client(state)
        self.assertEqual((await client.get("/kiosk?token=wrong", allow_redirects=False)).status, 403)
        # ngrok also connects from loopback, but forwards the visitor's address and host.
        self.assertEqual((await client.get("/kiosk?token=local-token", allow_redirects=False,
                                           headers={"X-Forwarded-For": "203.0.113.9"})).status, 404)
        self.assertEqual((await client.get("/kiosk?token=local-token", allow_redirects=False,
                                           headers={"Host": "example.ngrok-free.app"})).status, 404)
        login = await client.get("/kiosk?token=local-token", allow_redirects=False)
        self.assertEqual((login.status, login.headers["Location"]), (302, "/board"))
        self.assertEqual((await client.get("/api/status")).status, 200)
        self.assertEqual((await (await client.get("/api/session")).json())["scope"], "board")
        session = await (await client.get("/api/session")).json()
        for method, path in (("GET", "/api/settings"), ("GET", "/api/logs"), ("POST", "/api/trigger"), ("POST", "/api/presence")):
            response = await client.request(method, path, json={}, headers={**origin, "X-Noha-Token": session["csrf"]})
            self.assertEqual(response.status, 404, path)
        app[SESSIONS].clear()  # portal restart: the display's cookie logs it in again
        self.assertEqual((await client.get("/api/status")).status, 200)
        app[SESSIONS].clear()
        self.assertEqual((await client.get("/api/status", headers={"X-Forwarded-For": "203.0.113.9"})).status, 401)

    async def test_pages_and_assets_are_served_with_validators_and_a_strict_policy(self):
        app, client, _ = await self.client({"revision": "r"})
        page = await client.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn('src="/assets/app.js"', await page.text())
        self.assertIn("script-src 'self';", page.headers["Content-Security-Policy"])
        board = await client.get("/board")
        self.assertIn("/assets/board.js", await board.text())
        for path in ("/assets/app.js", "/assets/lib.js", "/assets/robot.js", "/assets/pages/home.js", "/assets/pages/settings.js",
                     "/assets/app.css", "/logo.png", "/dashboard.css"):
            response = await client.get(path)
            self.assertEqual(response.status, 200, path)
            again = await client.get(path, headers={"If-None-Match": response.headers["ETag"]})
            self.assertEqual(again.status, 304, path)
        for path in ("/assets/../admin.py", "/assets/%2e%2e/admin.py", "/assets/nope.js", "/noha/admin.py"):
            response = await client.get(path)  # never a file outside noha/web
            self.assertIn(response.status, {401, 404}, path)
            self.assertNotIn("import", await response.text())
        self.assertEqual((await client.get("/", headers={"Host": "evil.test"})).status, 403)

    async def test_hotspot_dispatch_uses_socket_destination_not_host(self):
        def request(local, host):
            return SimpleNamespace(headers={"Host": host}, transport=SimpleNamespace(get_extra_info=lambda _: (local, 80)))
        self.assertTrue(is_recovery(request("192.168.50.1", "noha.local"), "192.168.50.1"))
        self.assertTrue(is_recovery(request("::ffff:192.168.50.1", "public.ngrok-free.app"), "192.168.50.1"))
        self.assertFalse(is_recovery(request("192.168.1.5", "192.168.50.1"), "192.168.50.1"))
        self.assertFalse(is_recovery(request("127.0.0.1", "192.168.50.1"), "192.168.50.1"))


class DataTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_hashes_and_secret_free_settings(self):
        password = password_record("admin")
        self.assertTrue(verify_password("admin", password))
        self.assertFalse(verify_password("other", password))
        self.assertNotEqual(password["salt"], password_record("admin")["salt"])
        self.assertTrue(admin.default_password(password))
        self.assertFalse(admin.default_password(password_record("segredo")))
        with TemporaryDirectory() as folder:
            root = Path(folder)
            personality = root / "personality.txt"
            personality.write_text("Olá", encoding="utf-8")
            (root / "noha.toml").write_text('[voice]\ninstructions = ' + json.dumps(str(personality)) + '\n', encoding="utf-8")
            (root / "secrets.env").write_text("OPENAI_API_KEY=sk-private-value\n")
            (root / "ngrok.json").write_text(json.dumps({"authtoken": "private-ngrok-token", "url": "https://example.ngrok-free.app"}))
            (root / "config-pending.json").write_text(json.dumps({"voice.model": "gpt-live-1", "openai_key": "new-secret"}))
            with patch.object(admin, "ETC", root), patch.object(admin, "STATE", root), \
                    patch.object(admin, "audio_devices", new=AsyncMock(side_effect=OSError)):
                settings = await admin.Admin().get_settings()
            payload = json.dumps(settings)
            for secret in ("sk-private-value", "private-ngrok-token", "new-secret"):
                self.assertNotIn(secret, payload)
            self.assertTrue(settings["openai_key_configured"])
            self.assertTrue(settings["ngrok_token_configured"])
            self.assertEqual(settings["pending"], {"voice.model": "gpt-live-1", "openai_key": True})
            self.assertEqual(settings["values"]["personality"], "Olá")
            self.assertEqual(settings["devices"], [])

    def test_metrics_persist_and_distinguish_unreported_counters(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"NOHA_METRICS_DB": str(Path(folder) / "usage.db")}):
            metrics.record("gpt-live-1", conversations=1, duration_seconds=30)
            metrics.record("gpt-realtime-1.5", conversations=1, input_tokens=20, output_tokens=5, cached_tokens=0)
            result = metrics.summary(7)
            self.assertEqual(result["totals"]["conversations"], 2)
            live = next(row for row in result["models"] if row["model"] == "gpt-live-1")
            self.assertIsNone(live["input_tokens"])
            self.assertEqual(result["totals"]["cached_tokens"], 0)

    def test_visits_feed_outcomes_series_and_recent_list(self):
        import time
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"NOHA_METRICS_DB": str(Path(folder) / "usage.db")}):
            self.assertEqual(metrics.summary(1)["totals"]["visits"], 0)  # no database yet
            now = time.time()
            metrics.record_visit(now - 120, now - 60, "conversou", "gpt-realtime-2.1-mini", 0.03, 4)
            metrics.record_visit(now - 50, now - 40, "sem_resposta", "gpt-realtime-2.1-mini")
            metrics.record("gpt-realtime-2.1-mini", cost_usd=0.03)
            with self.assertRaises(ValueError):
                metrics.record_visit(now, now, "inventado", "x")
            today = metrics.summary(1)
            self.assertEqual(today["totals"]["visits"], 2)
            self.assertEqual(today["outcomes"]["conversou"], 1)
            self.assertEqual(len(today["series"]), 24)
            self.assertEqual(sum(bar["visits"] for bar in today["series"]), 2)
            self.assertAlmostEqual(sum(bar["cost_usd"] for bar in today["series"]), 0.03)
            self.assertEqual([v["outcome"] for v in today["visits"]], ["sem_resposta", "conversou"])
            self.assertEqual(len(metrics.summary(30)["series"]), 30)

    def test_cost_estimate_and_spend_today(self):
        from noha.models import response_cost
        usage = {"input_tokens": 5000, "output_tokens": 100,
                 "input_token_details": {"text_tokens": 1500, "audio_tokens": 3500, "cached_tokens": 4000,
                                         "cached_tokens_details": {"text_tokens": 1500, "audio_tokens": 2500}},
                 "output_token_details": {"text_tokens": 20, "audio_tokens": 80}}
        self.assertAlmostEqual(response_cost("gpt-realtime-1.5", usage),
                               (1500 * 0.4 + 2500 * 0.4 + 1000 * 32 + 20 * 16 + 80 * 64) / 1e6)
        self.assertAlmostEqual(response_cost("gpt-realtime-2.1-mini", {"input_tokens": 1000, "output_tokens": 0}), 10 / 1000)
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"NOHA_METRICS_DB": str(Path(folder) / "usage.db")}):
            self.assertEqual(metrics.spent_today(), 0)
            metrics.record("gpt-realtime-1.5", cost_usd=0.75)
            metrics.record("gpt-live-1", cost_usd=0.5)
            self.assertAlmostEqual(metrics.spent_today(), 1.25)
            self.assertAlmostEqual(metrics.summary(1)["totals"]["cost_usd"], 1.25)

    def test_model_adapters_use_their_own_protocol_and_usage(self):
        self.assertEqual(model_info("gpt-live-1").transport, "live")
        with self.assertRaises(ValueError):
            model_info("gpt-4o-realtime-preview")
        event = start_event(Voice(model="gpt-live-1", voice="marin"), "Instruções locais", True)
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
            record.assert_called_once_with("gpt-realtime-1.5", input_tokens=4, output_tokens=6, cost_usd=(4 * 32 + 6 * 64) / 1e6)


if __name__ == "__main__":
    unittest.main()
