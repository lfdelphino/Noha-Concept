import asyncio
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from noha.config import Config, Network
from noha.controller import Controller
from noha.network import split_nm, validate_credentials
from noha.realtime import Playback, safe_code
from noha.state import ConversationState, MotionGate, MotionLog


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

    def test_dashboard_sensor_readiness_and_timeline(self):
        gate = MotionGate(0)
        self.assertEqual(gate.readiness(20), ("aquecendo", 40))
        self.assertEqual(gate.readiness(60), ("pronto", 0))
        self.assertTrue(gate.update(60, True))
        self.assertEqual(gate.readiness(61, busy=True), ("conversando", 0))
        gate.update(70, False)
        self.assertEqual(gate.readiness(90), ("rearmando", 90))  # cooldown outlasts the quiet period
        log = MotionLog(window=600)
        self.assertTrue(log.update(1000, True))
        self.assertFalse(log.update(1001, True))
        self.assertTrue(log.update(1005, False))
        self.assertEqual((log.snapshot(), log.last), ([[1000, 1005]], 1001))
        log.update(1700, True)
        self.assertEqual(log.snapshot(), [[1700, None]])  # older intervals leave the 10 minute window

    def test_idle_counts_from_noha_last_word_not_from_noise(self):
        state = ConversationState(10)
        self.assertFalse(state.idle(39, 30, False))
        state.speaking = True  # VAD noise alone must not keep the session open
        self.assertTrue(state.idle(40, 30, False))
        self.assertFalse(state.idle(50, 30, True))  # Noha talking restarts the idle clock
        state.generating = True
        self.assertFalse(state.idle(60, 30, False))
        state.generating = False
        self.assertFalse(state.idle(89, 30, False))
        self.assertTrue(state.idle(90, 30, False))


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
        self.assertEqual(cfg.voice.model, "gpt-realtime-2.1-mini")
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

    def test_error_close_frame_becomes_api_problem(self):
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close
        from noha.realtime import api_message, close_problem
        quota = ConnectionClosedError(Close(1013, "insufficient_quota.credit_balance_exhausted"), None)
        self.assertEqual(str(close_problem(quota)), "insufficient_quota.credit_balance_exhausted")
        self.assertIn("sem créditos", api_message(str(close_problem(quota))))
        self.assertIsNone(close_problem(ConnectionClosedError(Close(1011, "keepalive ping timeout"), None)))
        self.assertIsNone(close_problem(ConnectionClosedError(None, None)))
        self.assertIsNone(close_problem(OSError()))
        self.assertIn("código weird_thing", api_message("weird_thing"))


if __name__ == "__main__":
    unittest.main()


class GpioChipTests(unittest.TestCase):
    def test_rp1_compatibility_symlink_is_not_a_second_chip(self):
        import os
        import sys
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from noha.motion import rp1_chip
        labels = {0: "pinctrl-rp1", 10: "gpio-brcmstb@107d508500"}
        lgpio = SimpleNamespace(gpiochip_open=lambda n: n, gpiochip_close=lambda h: None,
                                gpio_get_chip_info=lambda h: [0, 54, f"gpiochip{h}", labels[h]])
        with TemporaryDirectory() as folder, patch.dict(sys.modules, {"lgpio": lgpio}):
            dev = Path(folder)
            (dev / "gpiochip0").touch()
            (dev / "gpiochip10").touch()
            try:
                os.symlink(dev / "gpiochip0", dev / "gpiochip4")
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertEqual(rp1_chip(dev), 0)


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

    def test_missing_names_the_device_before_its_echo_nodes(self):
        from noha.audio import missing
        from noha.config import Audio
        hdmi = Audio(microphone=self.MIC, speaker=self.HDMI)
        self.assertEqual(missing(hdmi, {self.MIC, "noha_echo_source"}), ["Alto-falante"])
        self.assertEqual(missing(hdmi, {self.MIC, self.HDMI}),
                         ["Microfone com cancelamento de eco", "Alto-falante com cancelamento de eco"])
        self.assertEqual(missing(hdmi, {self.MIC, self.HDMI, "noha_echo_source", "noha_echo_sink"}), [])
        self.assertEqual(missing(Audio(microphone=self.MIC, speaker=self.XVF_OUT), {self.XVF_OUT}), ["Microfone"])

    def test_hdmi_card_without_profile_is_detected(self):
        from noha.audio import card_of, parse_nodes, profile_missing
        from noha.config import Audio
        dump = json.dumps([{"info": {"props": {"media.class": "Audio/Device", "device.name": "alsa_card.platform-107c701400.hdmi"}}},
                           {"info": {"props": {"media.class": "Audio/Source", "node.name": self.MIC}}}])
        names = {n["name"] for n in parse_nodes(dump)}
        self.assertEqual(card_of(self.XVF_OUT), "alsa_card.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993701262801141-00")
        self.assertTrue(profile_missing(Audio(microphone=self.MIC, speaker=self.HDMI), names))
        self.assertFalse(profile_missing(Audio(microphone=self.MIC, speaker=self.HDMI), names | {self.HDMI}))
        self.assertFalse(profile_missing(Audio(microphone=self.MIC, speaker=self.XVF_OUT), names))  # unplugged USB

    def test_forced_modes(self):
        from noha.config import Audio
        self.assertFalse(Audio(microphone=self.MIC, speaker=self.XVF_OUT, echo_cancel="pipewire").hardware_aec())
        self.assertTrue(Audio(microphone=self.MIC, speaker=self.HDMI, echo_cancel="xvf3800").hardware_aec())


class FakeSocket:
    def __init__(self, events):
        self.events, self.sent = [json.dumps(e) for e in events], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.events.pop(0)


class RealtimeSessionTests(unittest.IsolatedAsyncioTestCase):
    MIC = EchoCancelTests.MIC

    async def run_session(self, speaker, events):
        from types import SimpleNamespace
        from noha.config import Audio, Voice
        from noha.realtime import conversation
        audio = SimpleNamespace(config=Audio(microphone=self.MIC, speaker=speaker), input=asyncio.Queue(),
                                start_capture=lambda: None, check=lambda: None, flush=lambda: None,
                                busy=lambda tail_ms=0: False, played_ms=lambda: 0, begin_item=lambda: None)
        socket = FakeSocket([{"type": "session.updated"}, *events])
        with patch("websockets.asyncio.client.connect", lambda *a, **k: socket), \
             patch("noha.realtime.spent_today", return_value=0), patch("noha.realtime.record"), \
             patch("noha.audio.devices", return_value=[{"name": n} for n in audio.config.required()]):
            with self.assertRaises(ConnectionError):  # the fake server hangs up after its script
                await conversation(Voice(), "key", "Instruções", audio)
        return socket.sent

    async def test_short_detections_are_deleted_and_speech_gets_a_reply(self):
        sent = await self.run_session(EchoCancelTests.HDMI, [
            {"type": "input_audio_buffer.speech_started", "item_id": "noise", "audio_start_ms": 1000},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "noise", "audio_end_ms": 1300},
            {"type": "input_audio_buffer.committed", "item_id": "noise"},
            {"type": "input_audio_buffer.speech_started", "item_id": "visitor", "audio_start_ms": 2000},
            {"type": "input_audio_buffer.speech_stopped", "item_id": "visitor", "audio_end_ms": 3500},
            {"type": "input_audio_buffer.committed", "item_id": "visitor"},
            {"type": "error", "error": {"code": "conversation_already_has_active_response"}},
        ])
        session = sent[0]["session"]
        self.assertFalse(session["audio"]["input"]["turn_detection"]["create_response"])
        self.assertFalse(session["audio"]["input"]["turn_detection"]["interrupt_response"])  # HDMI: no barge-in
        self.assertEqual(session["truncation"]["token_limits"]["post_instructions"], 4000)
        self.assertEqual(session["audio"]["input"]["noise_reduction"], {"type": "far_field"})
        greeting = sent[1]["response"]["instructions"]
        self.assertTrue(greeting.startswith("Instruções") and "INÍCIO DA VISITA" in greeting)
        self.assertEqual(sent[2:], [{"type": "conversation.item.delete", "item_id": "noise"}, {"type": "response.create"}])

    async def test_xvf3800_speaker_keeps_barge_in(self):
        sent = await self.run_session(EchoCancelTests.XVF_OUT, [])
        self.assertTrue(sent[0]["session"]["audio"]["input"]["turn_detection"]["interrupt_response"])
