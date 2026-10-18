import asyncio
import base64
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from noha.config import Config, Network
from noha.controller import Controller
from noha.network import split_nm, validate_credentials
from noha.realtime import Playback, safe_code
from noha.state import ConversationState, MotionGate


class MotionTests(unittest.TestCase):
    def test_warmup_single_greeting_and_rearm(self):
        gate = MotionGate(0)
        self.assertFalse(gate.update(59, True))
        self.assertTrue(gate.update(60, True))
        self.assertFalse(gate.update(100, True, busy=True))
        gate.update(110, False)
        self.assertFalse(gate.update(150, True))  # cooldown and motion-free interval required
        gate.update(151, False)
        gate.update(182, False)
        self.assertTrue(gate.update(183, True))

    def test_idle_waits_for_speech_generation_and_playback(self):
        state = ConversationState(10)
        self.assertFalse(state.idle(69, 60, False))
        self.assertFalse(state.idle(80, 60, True))
        state.speaking = True
        self.assertFalse(state.idle(80, 60, False))
        state.speaking = False
        state.generating = True
        self.assertFalse(state.idle(80, 60, False))
        state.generating = False
        self.assertTrue(state.idle(80, 60, False))


class FakeAudio:
    def __init__(self):
        self.chunks = []
        self.flushed = False
    def begin_item(self): pass
    def push(self, data): self.chunks.append(data)
    def played_ms(self): return 250
    def busy(self): return bool(self.chunks)
    def flush(self): self.flushed = True; self.chunks.clear()


class PlaybackTests(unittest.TestCase):
    def test_interruption_truncates_heard_audio_and_rejects_late_delta(self):
        audio = FakeAudio()
        playback = Playback(audio)
        event = {"response_id": "r1", "item_id": "i1", "delta": base64.b64encode(b"\0\0").decode()}
        playback.delta(event)
        truncated = playback.interrupt("r1")
        self.assertEqual(truncated["audio_end_ms"], 250)
        self.assertEqual(truncated["item_id"], "i1")
        self.assertTrue(audio.flushed)
        playback.delta(event)
        self.assertEqual(audio.chunks, [])
        playback.delta({**event, "response_id": "r2", "item_id": "i2"})
        self.assertEqual(len(audio.chunks), 1)


class FakeNetwork:
    ssid = "Noha-Setup-TEST"
    def __init__(self):
        for name in ("hotspot_up", "hotspot_down", "activate", "remove"):
            setattr(self, name, AsyncMock())
        self.scan = AsyncMock(return_value=[{"ssid": "Home", "signal": 90, "security": "wpa-psk"}])
        self.add_wifi = AsyncMock(return_value="candidate")
        self.associated = AsyncMock(return_value=True)
        self.saved = AsyncMock(return_value=["saved"])
        self.hotspot_healthy = AsyncMock(return_value=True)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def setup_controller(self):
        adapter = FakeNetwork()
        self.now = 0
        probe = AsyncMock(return_value=False)
        controller = Controller(Network(), adapter, probe, lambda: self.now)
        return controller, adapter, probe

    async def test_outage_grace_period_then_hotspot(self):
        controller, adapter, probe = self.setup_controller()
        self.now = 89
        await controller.tick()
        adapter.hotspot_up.assert_not_called()
        self.now = 90
        await controller.tick()
        adapter.hotspot_up.assert_awaited_once()
        self.assertEqual(controller.mode, "configuracao")
        probe.return_value = True
        await controller.tick()
        self.assertEqual(controller.mode, "online")
        adapter.hotspot_down.assert_awaited_once()

    async def test_wrong_password_restores_portal_removes_only_candidate(self):
        controller, adapter, _ = self.setup_controller()
        adapter.activate.side_effect = RuntimeError("secret must not be logged")
        adapter.associated.return_value = False
        credentials = {"ssid": "Home", "password": "secret123", "security": "wpa-psk", "hidden": False}
        with patch("noha.controller.asyncio.sleep", new=AsyncMock()):
            await controller.submit(credentials)
        adapter.remove.assert_awaited_once_with("candidate")
        adapter.hotspot_up.assert_awaited_once()
        self.assertEqual(controller.mode, "configuracao")
        self.assertEqual(credentials, {})

    async def test_success_keeps_profile_and_closes_hotspot(self):
        controller, adapter, probe = self.setup_controller()
        probe.return_value = True
        with patch("noha.controller.asyncio.sleep", new=AsyncMock()):
            await controller.submit({"ssid": "Home", "password": "secret123"})
        self.assertEqual(controller.mode, "online")
        adapter.hotspot_down.assert_awaited_once()
        adapter.hotspot_up.assert_not_called()
        adapter.remove.assert_not_called()

    async def test_connected_without_internet_preserves_credentials(self):
        controller, adapter, _ = self.setup_controller()
        with patch("noha.controller.asyncio.sleep", new=AsyncMock()):
            await controller.submit({"ssid": "Home", "password": "secret123"})
        adapter.remove.assert_not_called()
        self.assertEqual(controller.mode, "configuracao")
        self.assertIn("sem internet", controller.message)

    async def test_saved_retry_waits_for_portal_quiet(self):
        controller, adapter, _ = self.setup_controller()
        controller.mode = "configuracao"
        controller.last_activity = 290
        self.now = 310
        await controller.tick()
        adapter.hotspot_down.assert_not_called()
        self.now = 411
        await controller.tick()
        adapter.activate.assert_awaited_once_with("saved")
        self.assertEqual(controller.mode, "configuracao")


class ConfigTests(unittest.TestCase):
    def test_example_and_prompt_are_usable(self):
        cfg = Config.load("config/noha.toml")
        self.assertEqual(cfg.motion.gpio, 3)
        self.assertEqual(cfg.voice.model, "gpt-realtime-1.5")
        self.assertNotIn("OPENAI_API_KEY", Path("config/noha.toml").read_text())
        self.assertIn("Delphino", Path("config/personality.txt").read_text(encoding="utf-8"))

    def test_network_names_and_validation(self):
        self.assertEqual(split_nm(r"Office\:1:80:WPA2"), ["Office:1", "80", "WPA2"])
        self.assertEqual(validate_credentials({"ssid": "Rede", "security": "open"})["password"], "")
        with self.assertRaises(ValueError):
            validate_credentials({"ssid": "Rede", "password": "123"})
        with self.assertRaises(ValueError):
            validate_credentials({"ssid": "Rede", "security": "enterprise"})

    def test_server_error_text_cannot_leak_into_logs(self):
        self.assertEqual(safe_code("invalid_api_key"), "invalid_api_key")
        self.assertEqual(safe_code("Your key is sk-secret123"), "unknown")


if __name__ == "__main__":
    unittest.main()
