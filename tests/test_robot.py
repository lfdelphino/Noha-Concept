"""Visit flow of the voice service, with fake audio, sensor, network and OpenAI adapter."""
import asyncio
from array import array
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from noha import app, metrics
from noha.config import Config


def tone(ms, amplitude):
    return array("h", (int(amplitude * math.sin(i / 3)) for i in range(24 * ms))).tobytes()


QUIET, VOICE = tone(20, 100), tone(20, 3000)


class FakeAudio:
    """Microphone chunks arrive in real time, 20 ms apart, as from the capture pipeline."""
    def __init__(self, config, chunks):
        self.config, self.input, self.playback, self.chunks = config, asyncio.Queue(), None, list(chunks)
        self.pushed, self.closed, self.feeder = [], False, None

    def start(self):
        self.playback = object()

    def start_capture(self):
        async def feed():
            for chunk in self.chunks:
                await asyncio.sleep(0.02)
                self.input.put_nowait(chunk)
        self.feeder = self.feeder or asyncio.create_task(feed())

    def check(self):
        pass

    def push(self, data):
        self.pushed.append(data)

    def busy(self, tail_ms=0):
        return False

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if self.feeder:
            self.feeder.cancel()


class RobotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        root = Path(self.folder.name)
        personality = root / "personality.txt"
        personality.write_text("Você é Noha.", encoding="utf-8")
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "run"), "NOHA_METRICS_DB": str(root / "usage.db"),
                                           "OPENAI_API_KEY": "sk-test-key"})
        self.env.start()
        self.config = Config()
        self.config.voice.instructions = str(personality)
        self.config.voice.unanswered_seconds = 1.5
        self.calls, self.audios, self.chunks, self.connected = [], [], [], True

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    async def adapter(self, voice, key, instructions, audio, greet, state_callback, greeting, preroll, stats):
        self.calls.append({"greet": greet, "greeting": greeting, "preroll": preroll.take() if preroll else None})
        state_callback("conversando")
        stats["replies"] += 1

    def robot(self, clips=(), setup_clip=None):
        def audio(config):
            self.audios.append(FakeAudio(config, self.chunks))
            return self.audios[-1]

        async def online():
            return self.connected

        async def offline_synthesis(config, key, texts):
            raise RuntimeError("http_503")  # tests never call OpenAI
        robot = app.Robot(self.config, adapter=self.adapter, audio_factory=audio, online=online, synthesize=offline_synthesis,
                          sensor_factory=lambda gpio: SimpleNamespace(value=False, close=lambda: None),
                          speak_offline=lambda text: b"espeak:" + text.encode())
        robot.clips, robot.setup_clip = list(clips), setup_clip
        return robot

    def outcomes(self):
        return [visit["outcome"] for visit in metrics.summary(1)["visits"]]

    async def test_answered_local_greeting_opens_a_session_with_context_and_speech(self):
        self.chunks = [QUIET] * 20 + [VOICE] * 20
        robot = self.robot(clips=[("Olá, sou Noha!", b"clip")])
        await robot.visit()
        self.assertEqual(self.audios[0].pushed, [b"clip"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.calls[0]["greet"])  # already greeted locally
        self.assertEqual(self.calls[0]["greeting"], "Olá, sou Noha!")
        self.assertIn(VOICE, [self.calls[0]["preroll"][i:i + len(VOICE)] for i in range(0, len(self.calls[0]["preroll"]), len(VOICE))])
        self.assertEqual(self.outcomes(), ["conversou"])
        self.assertTrue(self.audios[0].closed)

    async def test_unanswered_greeting_never_calls_the_api(self):
        self.chunks = [QUIET] * 200
        self.config.voice.unanswered_seconds = 0.3
        await self.robot(clips=[("Olá!", b"clip")]).visit()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcomes(), ["sem_resposta"])
        self.assertTrue(self.audios[0].closed)

    async def test_offline_visitor_who_talks_hears_the_wifi_steps(self):
        self.connected = False
        self.chunks = [QUIET] * 20 + [VOICE] * 20
        await self.robot(clips=[("Olá!", b"clip")], setup_clip=b"wifi steps").visit()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.audios[0].pushed, [b"clip", b"wifi steps"])
        self.assertEqual(self.outcomes(), ["wifi"])

    async def test_offline_without_recordings_greets_in_the_offline_voice(self):
        self.connected = False
        self.chunks = [QUIET] * 20 + [VOICE] * 20
        robot = self.robot()
        robot.offline_voice = True
        await robot.visit()
        greeting, steps = self.audios[0].pushed
        self.assertTrue(greeting.startswith("espeak:Olá, eu sou Noha".encode()))
        self.assertIn("Noha Setup".encode(), steps)
        self.assertEqual(self.outcomes(), ["wifi"])

    async def test_online_without_recordings_lets_the_model_greet(self):
        await self.robot().visit()
        self.assertEqual(self.calls, [{"greet": True, "greeting": None, "preroll": None}])
        self.assertEqual(self.outcomes(), ["conversou"])

    async def test_missing_key_is_reported_without_a_session(self):
        del os.environ["OPENAI_API_KEY"]
        await self.robot().visit()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outcomes(), ["sem_chave"])
        self.assertEqual(app._status["state"], "chave_ausente")

    async def test_recordings_replace_the_offline_voice_once_synthesized(self):
        async def synthesize(config, key, texts):
            return [(text, text.encode()) for text in texts]
        robot = self.robot()
        robot.synthesize = synthesize
        robot.offline["greeting"] = b"espeak"
        robot.poll_clips(0)
        await robot.preparing
        robot.poll_clips(1)
        self.assertEqual(len(robot.clips), len(self.config.greeting.texts))
        self.assertIn(b"Noha Setup", robot.setup_clip)
        self.assertEqual(robot.offline, {})
        self.assertTrue(robot.prepared)

    async def test_stop_request_ends_the_visit_and_waits_for_visitors(self):
        robot = self.robot()
        started = asyncio.Event()

        async def visit():
            started.set()
            await asyncio.Event().wait()  # a conversation that would never end by itself
        robot.visit = visit
        (app.runtime_dir() / "trigger").touch()
        await robot.tick()
        await started.wait()
        (app.runtime_dir() / "stop").touch()
        await robot.tick()
        await asyncio.gather(robot.active, return_exceptions=True)
        await robot.tick()
        self.assertIsNone(robot.active)
        self.assertFalse((app.runtime_dir() / "stop").exists())
        self.assertEqual((app._status["state"], app._status["detail"]), ("aguardando_movimento", "Conversa encerrada pelo painel"))
        robot.ring.close()

    async def test_dashboard_trigger_starts_a_visit_unless_over_budget(self):
        robot = self.robot()
        visits = []

        async def visit():
            visits.append(True)
        robot.visit = visit
        (app.runtime_dir() / "trigger").touch()
        await robot.tick()
        await robot.active
        self.assertEqual(visits, [True])
        self.assertFalse((app.runtime_dir() / "trigger").exists())
        robot.active = None
        (app.runtime_dir() / "trigger").touch()
        with patch.object(app, "over_budget", return_value=True):
            await robot.tick()
        self.assertIsNone(robot.active)
        self.assertEqual(app._status["state"], "orcamento_esgotado")
        robot.ring.close()


if __name__ == "__main__":
    unittest.main()
