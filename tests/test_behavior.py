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


class FakeUSB:
    def __init__(self):
        self.writes = []

    def ctrl_transfer(self, request_type, request, value, index, data, timeout):
        self.writes.append((request_type, value, index, bytes(data)))


class LedTests(unittest.TestCase):
    def test_states_map_to_patterns(self):
        from noha.leds import pattern_for
        self.assertEqual(pattern_for("aquecendo_sensor", warming=True), "warming")
        self.assertEqual(pattern_for("aquecendo_sensor"), "off")
        self.assertEqual(pattern_for("conversando"), "listening")
        self.assertEqual(pattern_for("conversando", speaking=True), "speaking")
        self.assertEqual(pattern_for("erro_api_http_401"), "error")
        self.assertEqual(pattern_for("conversando", updating=True), "updating")

    def test_writes_colors_before_effect_and_only_on_change(self):
        from noha.config import Leds
        from noha.leds import Ring
        device = FakeUSB()
        ring = Ring(Leds(brightness=255), opener=lambda: device)
        ring.show("listening")
        ring.show("listening")
        self.assertEqual(device.writes, [
            (0x40, 14, 20, b"\x01"),
            (0x40, 17, 20, (0x001830).to_bytes(4, "little") + (0x00B4FF).to_bytes(4, "little")),
            (0x40, 12, 20, b"\x04"),
        ])

    def test_usb_failure_is_silent_and_retried_later(self):
        from noha.config import Leds
        from noha.leds import Ring
        now = [0]
        def missing():
            raise LookupError
        ring = Ring(Leds(), opener=missing, clock=lambda: now[0])
        ring.show("error")
        device = FakeUSB()
        ring.opener = lambda: device
        ring.show("error")
        self.assertEqual(device.writes, [])
        now[0] = 31
        ring.show("error")
        self.assertEqual(device.writes[-1], (0x40, 12, 20, b"\x03"))


class EchoCancelTests(unittest.TestCase):
    MIC = "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993701262801141-00.analog-stereo"
    XVF_OUT = "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993701262801141-00.analog-stereo"
    HDMI = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"

    def test_auto_uses_chip_only_when_speaker_is_the_array(self):
        from noha.config import Audio
        chip = Audio(microphone=self.MIC, speaker=self.XVF_OUT)
        self.assertEqual(chip.endpoints(), (self.MIC, self.XVF_OUT))
        self.assertEqual(chip.required(), (self.MIC, self.XVF_OUT, self.MIC, self.XVF_OUT))
        hdmi = Audio(microphone=self.MIC, speaker=self.HDMI)
        self.assertEqual(hdmi.endpoints(), ("noha_echo_source", "noha_echo_sink"))

    def test_dropped_pipewire_aec_detected_only_when_devices_are_present(self):
        from noha.audio import echo_cancel_dropped
        from noha.config import Audio
        hdmi = Audio(microphone=self.MIC, speaker=self.HDMI)
        self.assertTrue(echo_cancel_dropped(hdmi, {self.MIC, self.HDMI}))
        self.assertFalse(echo_cancel_dropped(hdmi, {self.MIC}))  # unplugged: a restart would not help
        self.assertFalse(echo_cancel_dropped(hdmi, {self.MIC, self.HDMI, "noha_echo_source", "noha_echo_sink"}))
        self.assertFalse(echo_cancel_dropped(Audio(microphone=self.MIC, speaker=self.XVF_OUT), {self.MIC, self.XVF_OUT}))

    def test_forced_modes(self):
        from noha.config import Audio
        self.assertFalse(Audio(microphone=self.MIC, speaker=self.XVF_OUT, echo_cancel="pipewire").hardware_aec())
        self.assertTrue(Audio(microphone=self.MIC, speaker=self.HDMI, echo_cancel="xvf3800").hardware_aec())
