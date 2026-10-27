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
        gate = MotionGate(0, pulses=1)  # greet on the first detection
        self.assertFalse(gate.update(59, True))
        self.assertTrue(gate.update(60, True))
        self.assertFalse(gate.update(100, True, busy=True))
        gate.update(110, False)
        self.assertFalse(gate.update(150, True))  # cooldown and motion-free interval required
        gate.update(151, False)
        gate.update(182, False)
        self.assertTrue(gate.update(183, True))

    def test_dashboard_sensor_readiness_and_timeline(self):
        gate = MotionGate(0, pulses=1)
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

    def test_passer_by_is_ignored_and_a_visitor_is_confirmed(self):
        gate = MotionGate(0, confirm=6, pulses=2, window=20)
        self.assertFalse(gate.update(100, True))  # someone walks past: one short detection
        self.assertEqual(gate.readiness(101), ("confirmando", 5))
        gate.update(103, False)
        self.assertFalse(gate.update(124, True))  # outside the window: a new passer-by, not a second pulse
        gate.update(126, False)
        self.assertTrue(gate.update(130, True))  # second detection within 20 s: someone is here
        gate = MotionGate(0, confirm=6, pulses=2)
        self.assertFalse(gate.update(100, True))
        self.assertFalse(gate.update(105, True))
        self.assertTrue(gate.update(106, True))  # continuous motion for confirm seconds
        gate = MotionGate(0, confirm=6, pulses=2)
        gate.update(55, True)  # motion during warmup is not a detection by itself,
        self.assertFalse(gate.update(60, True))
        self.assertTrue(gate.update(61, True))  # but its duration counts once warm

    def test_switched_off_sensor_never_greets(self):
        gate = MotionGate(0, pulses=1)
        self.assertFalse(gate.update(100, True, busy=True))  # the app passes busy while switched off
        gate.update(101, False)
        self.assertTrue(gate.update(102, True))

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

    async def test_answered_local_greeting_sends_context_and_speech_but_no_greeting_reply(self):
        from types import SimpleNamespace
        from noha.config import Audio, Voice
        from noha.realtime import conversation
        audio = SimpleNamespace(config=Audio(microphone=self.MIC, speaker=EchoCancelTests.HDMI), input=asyncio.Queue(),
                                start_capture=lambda: None, check=lambda: None, flush=lambda: None,
                                busy=lambda tail_ms=0: False, played_ms=lambda: 0, begin_item=lambda: None)
        socket = FakeSocket([{"type": "session.updated"},
                             {"type": "error", "error": {"code": "invalid_value", "event_id": "noha_greeting"}}])
        with patch("websockets.asyncio.client.connect", lambda *a, **k: socket), \
             patch("noha.realtime.spent_today", return_value=0), patch("noha.realtime.record"), \
             patch("noha.audio.devices", return_value=[{"name": n} for n in audio.config.required()]):
            with self.assertRaises(ConnectionError):  # the context error was tolerated
                await conversation(Voice(), "key", "Instruções", audio, greet=False, greeting="Olá, sou Noha.",
                                   preroll=SimpleNamespace(take=lambda: b"visitor"))
        sent = socket.sent
        context = sent[1]
        self.assertEqual((context["type"], context["item"]["role"]), ("conversation.item.create", "system"))
        self.assertIn("Olá, sou Noha.", context["item"]["content"][0]["text"])
        self.assertFalse(any(e["type"] == "response.create" for e in sent))
        appended = [e for e in sent if e["type"] == "input_audio_buffer.append"]
        self.assertEqual(base64.b64decode(appended[0]["audio"]), b"visitor")

    def test_unanswered_sessions_close_sooner(self):
        from noha.config import Voice
        from noha.live import start_event
        from noha.realtime import idle_limit
        self.assertEqual(idle_limit(Voice(), 0), 10)
        self.assertEqual(idle_limit(Voice(), 1), 30)
        self.assertEqual(idle_limit(Voice(idle_seconds=5), 0), 5)
        event = start_event(Voice(model="gpt-live-1"), "Instruções", False, "Olá, sou Noha.")
        self.assertIn("Olá, sou Noha.", event["session"]["input"][0]["content"][0]["text"])
        self.assertEqual(start_event(Voice(model="gpt-live-1"), "Instruções", False)["session"]["input"], [])


def tone(ms, amplitude):
    import math
    from array import array
    return array("h", (int(amplitude * math.sin(i / 3)) for i in range(24 * ms))).tobytes()


class LocalGreetingTests(unittest.IsolatedAsyncioTestCase):
    def test_speech_detector_needs_sustained_level_above_the_room(self):
        from noha.greeting import SpeechDetector
        detector = SpeechDetector(margin_db=12)
        for _ in range(15):
            detector.calibrate(tone(20, 100))  # quiet room, about -50 dBFS
        self.assertFalse(any(detector.feed(tone(20, 100)) for _ in range(100)))  # steady noise
        self.assertFalse(detector.feed(tone(100, 8000)))  # a door: loud but short
        self.assertFalse(any(detector.feed(tone(20, 100)) for _ in range(20)))
        results = [detector.feed(tone(20, 3000)) for _ in range(20)]
        self.assertFalse(results[13])
        self.assertTrue(results[14])  # 300 ms of voice

    def test_detector_follows_a_noisy_room(self):
        from noha.greeting import SpeechDetector
        detector = SpeechDetector(margin_db=12)
        detector.calibrate(tone(20, 100))
        # A TV starts: louder than the calibration but steady, so the floor rises with it.
        early = [detector.feed(tone(20, 1500)) for _ in range(500)]
        self.assertTrue(any(early[:50]))
        detector.voiced = 0
        self.assertFalse(any(detector.feed(tone(20, 1500)) for _ in range(100)))

    async def run_greeting(self, chunks, seconds=3):
        from types import SimpleNamespace
        from noha.greeting import SpeechDetector, greet_and_listen
        pushed = []
        audio = SimpleNamespace(input=asyncio.Queue(), start_capture=lambda: None, check=lambda: None,
                                push=pushed.append, busy=lambda tail_ms=0: False)
        for chunk in chunks:
            audio.input.put_nowait(chunk)
        now = [0.0]
        def clock():
            now[0] += 0.02
            return now[0]
        heard = await greet_and_listen(audio, b"clip", seconds, SpeechDetector(), clock=clock)
        return pushed, heard

    async def test_greeting_plays_locally_and_returns_the_visitor_speech(self):
        quiet, voice = tone(20, 100), tone(20, 3000)
        pushed, heard = await self.run_greeting([quiet] * 30 + [voice] * 15)
        self.assertEqual(pushed, [b"clip"])  # after measuring the room
        self.assertEqual(heard[-1], voice)
        self.assertIn(quiet, heard)  # the onset before the detection is kept for the session

    async def test_unanswered_greeting_ends_without_api(self):
        pushed, heard = await self.run_greeting([tone(20, 100)] * 300, seconds=2)
        self.assertEqual(pushed, [b"clip"])
        self.assertIsNone(heard)

    async def test_preroll_pumps_the_queue_until_the_session_takes_it(self):
        from types import SimpleNamespace
        from noha.greeting import Preroll
        audio = SimpleNamespace(input=asyncio.Queue())
        preroll = Preroll(audio, [b"ab"])
        audio.input.put_nowait(b"cd")
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(preroll.take(), b"abcd")
        self.assertTrue(audio.input.empty())

    async def test_clips_are_synthesized_once_per_voice_and_text(self):
        from tempfile import TemporaryDirectory
        from noha.greeting import prepare
        requests = []
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def read(self): return b"\0\0" * 24000
        class Session:
            def post(self, url, json, headers):
                requests.append(json)
                return Response()
        config = Config()
        config.greeting.texts = ["Olá", "Oi"]
        with TemporaryDirectory() as folder, patch("noha.greeting.record") as record:
            folder = Path(folder)
            (folder / "old.pcm").write_bytes(b"x")
            clips = await prepare(config, "key", config.greeting.texts, folder, Session())
            self.assertEqual([text for text, _ in clips], ["Olá", "Oi"])
            self.assertEqual({r["voice"] for r in requests}, {"alloy"})
            self.assertEqual(requests[0]["response_format"], "pcm")
            self.assertFalse((folder / "old.pcm").exists())
            self.assertAlmostEqual(record.call_args.kwargs["cost_usd"], 0.015 / 60)
            await prepare(config, "key", config.greeting.texts, folder, Session())
            self.assertEqual(len(requests), 2)  # cached
            config.voice.voice = "cedar"  # another voice: new clips, old ones removed
            await prepare(config, "key", config.greeting.texts, folder, Session())
            self.assertEqual(len(requests), 4)
            self.assertEqual(len(list(folder.glob("*.pcm"))), 2)

    async def test_failed_synthesis_reports_only_the_status(self):
        from tempfile import TemporaryDirectory
        from noha.greeting import prepare
        class Response:
            status = 401
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
        class Session:
            def post(self, *a, **k): return Response()
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "^http_401$"):
                await prepare(Config(), "key", ["Olá"], Path(folder), Session())


class OfflineSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_instructions_name_this_pi_hotspot_and_portal(self):
        from tempfile import TemporaryDirectory
        from noha.greeting import setup_text
        from noha.network import hotspot_ssid
        with TemporaryDirectory() as folder:
            machine = Path(folder) / "machine-id"
            machine.write_text("abc\n")
            ssid = hotspot_ssid(Network(), machine)
        self.assertRegex(ssid, r"^Noha-Setup-[0-9A-F]{4}$")
        text = setup_text(Network(), "Noha-Setup-A1B2")
        self.assertIn("rede Wi-Fi aberta Noha Setup A 1 B 2.", text)  # spelled out for speech
        self.assertIn("192 ponto 168 ponto 50 ponto 1", text)
        self.assertIn("Minha Rede", setup_text(Network(), "Minha-Rede"))

    def test_espeak_fallback_becomes_24khz_pcm(self):
        import wave
        from noha.greeting import espeak
        def fake_run(args, **kwargs):
            self.assertEqual(args[:3], ["espeak-ng", "-v", "pt-br"])
            with wave.open(args[args.index("-w") + 1], "wb") as clip:
                clip.setnchannels(1)
                clip.setsampwidth(2)
                clip.setframerate(22050)
                clip.writeframes(b"\x10\x00" * 22050)
        with patch("noha.greeting.subprocess.run", side_effect=fake_run):
            pcm = espeak("Olá")
        self.assertEqual(len(pcm), 2 * 24000)  # one second at the pipeline's rate
        self.assertEqual(pcm[:2], b"\x10\x00")

    async def test_play_drains_the_microphone_until_the_clip_ends(self):
        from types import SimpleNamespace
        from noha.greeting import play
        remaining = [3]
        def busy():
            remaining[0] -= 1
            return remaining[0] > 0
        audio = SimpleNamespace(input=asyncio.Queue(), pushed=[], check=lambda: None, busy=busy)
        audio.push = audio.pushed.append
        for _ in range(50):
            audio.input.put_nowait(b"\0\0")
        await play(audio, b"instructions")
        self.assertEqual(audio.pushed, [b"instructions"])
        self.assertTrue(audio.input.empty())

    async def test_without_a_greeting_clip_noha_only_listens(self):
        from types import SimpleNamespace
        from noha.greeting import SpeechDetector, greet_and_listen
        audio = SimpleNamespace(input=asyncio.Queue(), start_capture=lambda: None, check=lambda: None,
                                push=lambda data: self.fail("nothing to play"), busy=lambda tail_ms=0: False)
        for chunk in [tone(20, 100)] * 20 + [tone(20, 3000)] * 15:
            audio.input.put_nowait(chunk)
        now = [0.0]
        def clock():
            now[0] += 0.02
            return now[0]
        self.assertIsNotNone(await greet_and_listen(audio, None, 3, SpeechDetector(), clock=clock))


class PresenceTests(unittest.TestCase):
    def test_switch_defaults_on_and_config_is_validated(self):
        from tempfile import TemporaryDirectory
        from noha.config import presence_enabled
        with TemporaryDirectory() as folder:
            path = Path(folder) / "presence.json"
            self.assertTrue(presence_enabled(path))
            path.write_text('{"enabled": false}')
            self.assertFalse(presence_enabled(path))
            path.write_text("not json")
            self.assertTrue(presence_enabled(path))
            config = Path(folder) / "noha.toml"
            config.write_text("[motion]\nconfirm_pulses = 0\n")
            with self.assertRaises(ValueError):
                Config.load(config)
            config.write_text("[greeting]\ntexts = []\nlocal = false\n")
            self.assertFalse(Config.load(config).greeting.local)


class EndingTests(unittest.IsolatedAsyncioTestCase):
    """Visits must end: by the model's tool, by idleness even when the stream never stops."""
    MIC = EchoCancelTests.MIC

    def fake_audio(self):
        from types import SimpleNamespace
        from noha.config import Audio
        return SimpleNamespace(config=Audio(microphone=self.MIC, speaker=EchoCancelTests.HDMI), input=asyncio.Queue(),
                               start_capture=lambda: None, check=lambda: None, flush=lambda: None, push=lambda data: None,
                               busy=lambda tail_ms=0: False, played_ms=lambda: 0, begin_item=lambda: None)

    def test_only_audible_chunks_count_as_speaking(self):
        from array import array
        from noha.audio import audible
        self.assertFalse(audible(array("h", [0, 3, -5, 120] * 240).tobytes()))  # GPT-Live between turns
        self.assertTrue(audible(array("h", [0, 2500, -2600, 40] * 240).tobytes()))
        self.assertFalse(audible(b""))

    async def test_end_tool_closes_the_realtime_visit(self):
        from noha.config import Voice
        from noha.realtime import END_TOOL, conversation

        class Hanging(FakeSocket):
            async def __anext__(self):
                if not self.events:
                    await asyncio.Event().wait()  # the server would keep the session open
                return await super().__anext__()
        audio = self.fake_audio()
        socket = Hanging([{"type": "session.updated"}, {"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "encerrar_conversa", "call_id": "c1", "arguments": "{}"}}])
        with patch("websockets.asyncio.client.connect", lambda *a, **k: socket), \
             patch("noha.realtime.spent_today", return_value=0), patch("noha.realtime.record"), \
             patch("noha.audio.devices", return_value=[{"name": n} for n in audio.config.required()]):
            async with asyncio.timeout(5):
                await conversation(Voice(), "key", "Instruções", audio, greet=False)  # returns: a normal end
        self.assertIn(END_TOOL, socket.sent[0]["session"]["tools"])

    async def test_gpt_live_visit_idles_out_while_its_stream_keeps_arriving(self):
        from noha.config import Voice
        from noha.live import conversation

        class LiveSocket(FakeSocket):
            def __init__(self):
                super().__init__([])
                self.queue = asyncio.Queue()
                self.queue.put_nowait(json.dumps({"type": "session.started"}))

            async def send(self, raw):
                await super().send(raw)
                if json.loads(raw)["type"] == "session.close":
                    self.queue.put_nowait(json.dumps({"type": "session.closed", "reason": "close_requested"}))

            async def __anext__(self):
                try:
                    return self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.02)  # near-silent output keeps streaming
                    return json.dumps({"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(960)).decode()})
        socket = LiveSocket()
        audio = self.fake_audio()
        voice = Voice(model="gpt-live-1", voice="marin", idle_seconds=0.3, unanswered_seconds=0.3)
        with patch("websockets.asyncio.client.connect", lambda *a, **k: socket), \
             patch("noha.realtime.spent_today", return_value=0), patch("noha.live.record"), \
             patch("noha.audio.devices", return_value=[{"name": n} for n in audio.config.required()]):
            async with asyncio.timeout(5):
                await conversation(voice, "key", "Instruções", audio, greet=False)
        self.assertIn({"type": "session.close"}, socket.sent)
