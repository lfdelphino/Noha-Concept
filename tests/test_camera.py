"""Camera presence: detection decoding, steadying, snapshots and the robot's handling of the camera."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from noha import admin, app, diagnostics, settings
from noha.config import Camera, Config

try:
    import numpy as np
    from noha import camera
except ImportError:  # numpy comes from apt on the Pi; CI installs it
    np = camera = None
try:
    import PIL  # noqa: F401
except ImportError:
    PIL = None


def prediction(cell, stride, size=(4.0, 6.0), objectness=0.9, person=0.9):
    """One raw YOLOX row: offset 0.5 in the cell, width and height `size` strides (log-encoded)."""
    row = np.zeros(85, np.float32)
    row[:4] = (0.5, 0.5, np.log(size[0]), np.log(size[1]))
    row[4], row[5] = objectness, person
    return row


@unittest.skipIf(np is None, "numpy ausente")
class DetectionTests(unittest.TestCase):
    def output(self, *rows):
        cells, _ = camera.grid()
        output = np.zeros((len(cells), 85), np.float32)
        for index, row in rows:
            output[index] = row
        return output

    def test_a_person_is_decoded_into_picture_fractions(self):
        # Stride 16 cell (x=10, y=8): index 52*52 + 8*26 + 10; centre (10.5*16, 8.5*16) = (168, 136).
        index = 52 * 52 + 8 * 26 + 10
        found = camera.people(self.output((index, prediction(None, 16))), 416, 312, 0.5)
        self.assertEqual(len(found), 1)
        score, left, top, right, bottom = found[0]
        self.assertAlmostEqual(score, 0.81, places=2)
        self.assertAlmostEqual(left, (168 - 32) / 416, places=3)  # 4 strides wide
        self.assertAlmostEqual(top, (136 - 48) / 312, places=3)   # 6 strides tall
        self.assertAlmostEqual(right, (168 + 32) / 416, places=3)
        self.assertAlmostEqual(bottom, (136 + 48) / 312, places=3)

    def test_other_objects_weak_scores_and_duplicates_are_dropped(self):
        index = 52 * 52 + 8 * 26 + 10
        dog = prediction(None, 16)
        dog[5], dog[5 + 16] = 0, 0.95  # class 16 is "dog"
        self.assertEqual(camera.people(self.output((index, dog)), 416, 312, 0.5), [])
        self.assertEqual(camera.people(self.output((index, prediction(None, 16, person=0.4))), 416, 312, 0.5), [])
        # The same person predicted by two neighbouring cells is one person; a second one far away is another.
        found = camera.people(self.output((index, prediction(None, 16)), (index + 1, prediction(None, 16, person=0.8)),
                                          (52 * 52 + 8 * 26 + 2, prediction(None, 16))), 416, 312, 0.5)
        self.assertEqual(len(found), 2)
        self.assertGreater(found[0][0], 0.8)

    def test_boxes_stay_inside_the_picture(self):
        found = camera.people(self.output((0, prediction(None, 8, size=(20.0, 20.0)))), 416, 312, 0.5)
        self.assertEqual(found[0][1:], (0.0, 0.0, found[0][3], found[0][4]))
        self.assertTrue(all(0 <= edge <= 1 for edge in found[0][1:]))

    def test_only_the_schema_noise_is_dropped_from_stderr(self):
        import sys
        import tempfile
        sys.stderr.flush()
        saved = os.dup(2)
        with tempfile.TemporaryFile() as journal:
            os.dup2(journal.fileno(), 2)
            try:
                with camera.filtered_stderr():
                    os.write(2, b"Schema error: Trying to register schema with name Scaler\n")
                    os.write(2, b"ERROR RPI camera frontend has timed out\n")
                    os.write(2, b"Schema error: Trying to register schema with name ZipMap\n")
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            journal.seek(0)
            self.assertEqual(journal.read(), b"ERROR RPI camera frontend has timed out\n")

    def test_rotation_turns_clockwise(self):
        frame = np.arange(6).reshape(2, 3)
        self.assertEqual(camera.upright(frame, 90).tolist(), [[3, 0], [4, 1], [5, 2]])
        self.assertEqual(camera.upright(frame, 270).tolist(), [[2, 5], [1, 4], [0, 3]])
        self.assertIs(camera.upright(frame, 0), frame)


@unittest.skipIf(np is None, "numpy ausente")
class PresenceTests(unittest.TestCase):
    def test_one_wrong_frame_never_counts_and_one_missed_frame_is_not_leaving(self):
        presence = camera.Presence(frames=2, hold=1.5)
        self.assertFalse(presence.update(0.0, True))   # a single frame: maybe a coat on a chair
        self.assertFalse(presence.update(0.2, False))
        self.assertFalse(presence.update(0.4, True))
        self.assertTrue(presence.update(0.6, True))
        self.assertTrue(presence.update(0.8, False))   # missed in one frame: still there
        self.assertTrue(presence.update(2.0, False))
        self.assertFalse(presence.update(2.2, False))  # 1.6 s without anyone: gone


@unittest.skipIf(np is None, "numpy ausente")
class CameraSensorTests(unittest.TestCase):
    def sensor(self, runtime, found, settings=None):
        frame = np.zeros((312, 416, 3), np.uint8)
        self.clock = 0.0

        def tick():
            self.clock += 0.2
            return self.clock
        return camera.CameraSensor(settings or Camera(), runtime, capture=lambda: frame,
                                   detect=lambda image, threshold: found, clock=tick, start=False)

    def test_only_people_tall_enough_in_the_picture_are_close(self):
        with TemporaryDirectory() as folder:
            near, far = (0.9, 0.3, 0.1, 0.6, 0.9), (0.95, 0.7, 0.4, 0.75, 0.6)  # 80% and 20% of the height
            sensor = self.sensor(Path(folder), [far])
            for _ in range(3):
                sensor.step()
            self.assertFalse(sensor.value)
            self.assertEqual(sensor.info, {"people": 0, "seen": 1, "height": 20})
            sensor = self.sensor(Path(folder), [far, near])
            sensor.step()
            self.assertFalse(sensor.value)  # needs two frames in a row
            sensor.step()
            self.assertTrue(sensor.value)
            self.assertEqual(sensor.info, {"people": 1, "seen": 2, "height": 80})
            self.assertFalse(sensor.failed)

    @unittest.skipIf(PIL is None, "Pillow ausente")
    def test_snapshot_is_made_only_on_request(self):
        with TemporaryDirectory() as folder:
            runtime = Path(folder)
            sensor = self.sensor(runtime, [(0.9, 0.3, 0.1, 0.6, 0.9), (0.6, 0.7, 0.4, 0.75, 0.6)])
            sensor.step()
            self.assertFalse((runtime / "snapshot.json").exists())
            (runtime / "snapshot").touch()
            sensor.step()
            self.assertFalse((runtime / "snapshot").exists())
            shot = json.loads((runtime / "snapshot.json").read_text())
            self.assertTrue(shot["image"].startswith("data:image/jpeg;base64,"))
            self.assertEqual([(p["height"], p["counts"]) for p in shot["people"]], [(80, True), (20, False)])
            self.assertEqual(shot["min_height"], 30)

    def test_a_camera_that_stops_sending_frames_is_failed(self):
        with TemporaryDirectory() as folder:
            def broken():
                raise TimeoutError
            sensor = camera.CameraSensor(Camera(), Path(folder), capture=broken, detect=lambda *a: [], start=False)
            sensor.run()
            self.assertEqual(sensor.error, "TimeoutError")
            self.assertTrue(sensor.failed)
            self.assertFalse(sensor.value)


class FakeCamera:
    def __init__(self):
        self.value, self.failed, self.closed = False, False, False
        self.info = {"people": 0, "seen": 0, "height": 0}

    def close(self):
        self.closed = True


class RobotCameraTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        root = Path(self.folder.name)
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "run"), "NOHA_METRICS_DB": str(root / "usage.db"),
                                           "OPENAI_API_KEY": "sk-test-key"})
        self.env.start()
        self.config = Config()
        self.config.motion.source = "camera"
        self.config.motion.confirm_seconds = 1
        self.cameras, self.now = [], 0.0

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def robot(self, factory=None):
        def opened(settings, runtime):
            self.cameras.append(FakeCamera())
            return self.cameras[-1]

        async def online():
            return True

        def gpio(pin):
            raise AssertionError("the camera robot must not open a GPIO pin")
        return app.Robot(self.config, adapter=lambda *a, **k: None, audio_factory=None, online=online,
                         synthesize=None, sensor_factory=gpio, camera_factory=factory or opened,
                         speak_offline=lambda text: b"", clock=lambda: self.now)

    def test_a_person_in_view_greets_after_confirm_seconds_without_pulses(self):
        robot = self.robot()
        self.assertEqual(robot.gate.warmup, 5)
        robot.poll_sensor(self.now)
        self.now = 10.0
        self.assertFalse(robot.gate.update(self.now, True))
        self.now = 10.5
        self.assertFalse(robot.gate.update(self.now, True))  # one long detection, not two pulses
        self.now = 11.0
        self.assertTrue(robot.gate.update(self.now, True))

    def test_a_failed_camera_is_closed_reported_and_reopened(self):
        robot = self.robot()
        robot.poll_sensor(0)
        first = self.cameras[0]
        first.failed = True
        robot.poll_sensor(1)
        self.assertTrue(first.closed)
        self.assertIsNone(robot.sensor)
        self.assertEqual(app._status["state"], "camera_indisponivel")
        robot.poll_sensor(20)
        self.assertEqual(len(self.cameras), 1)  # waits before trying again
        robot.poll_sensor(31)
        self.assertEqual(len(self.cameras), 2)
        self.assertIs(robot.sensor, self.cameras[1])

    def test_a_missing_camera_is_reported_and_retried(self):
        def missing(settings, runtime):
            raise RuntimeError("no camera")
        robot = self.robot(missing)
        robot.poll_sensor(0)
        self.assertIsNone(robot.sensor)
        self.assertEqual(app._status["state"], "camera_indisponivel")
        self.assertEqual(robot.next_sensor_retry, 30)

    async def test_heartbeat_names_the_camera_and_what_it_sees(self):
        robot = self.robot()
        robot.poll_sensor(0)
        self.cameras[0].info = {"people": 1, "seen": 2, "height": 64}
        robot.publish(0, True, False)
        status = json.loads((app.runtime_dir() / "status.json").read_text())
        self.assertEqual(status["camera"], {"people": 1, "seen": 2, "height": 64})
        self.assertTrue(status["gpio_ready"])


class CameraSettingsTests(unittest.TestCase):
    def test_source_and_rotation_choices_keep_their_type(self):
        config = Config()
        self.assertEqual(settings.validate({"motion.source": "camera", "camera.rotation": 90}, config),
                         {"motion.source": "camera", "camera.rotation": 90})
        for change in ({"camera.rotation": "90"}, {"camera.rotation": 45}, {"motion.source": "radar"},
                       {"camera.min_height": 120}, {"camera.confidence": 5}):
            with self.assertRaises(ValueError, msg=change):
                settings.validate(change, config)

    def test_camera_section_is_written_and_loaded(self):
        text = settings.apply("[motion]\ngpio = 17\n", {"motion.source": "camera", "camera.rotation": 270,
                                                       "camera.min_height": 40})
        with TemporaryDirectory() as folder:
            path = Path(folder) / "noha.toml"
            path.write_text(text)
            config = Config.load(path)
        self.assertEqual((config.motion.source, config.camera.rotation, config.camera.min_height), ("camera", 270, 40))
        shipped = Config.load(Path(__file__).resolve().parent.parent / "config/noha.toml")
        self.assertEqual((shipped.motion.source, shipped.camera), ("gpio", Camera()))

    def test_camera_only_fields_are_marked(self):
        only = {item["key"]: item["only"] for item in settings.schema()["settings"] if item["group"] == "sensor"}
        self.assertEqual(only["motion.gpio"], "gpio")
        self.assertEqual(only["camera.min_height"], "camera")
        self.assertEqual(only["motion.confirm_seconds"], "")


class CameraDiagnosticsTests(unittest.TestCase):
    def check(self, **voice):
        status = {"voice": {"stale": False, "audio_ready": True, "source": "camera", "gpio_ready": True, **voice},
                  "network": {"mode": "online"}, "key_configured": True, "presence": True}
        return {item["id"]: item for item in diagnostics.checks(status)}["sensor"]

    def test_camera_health_in_words(self):
        self.assertEqual(self.check()["level"], "ok")
        broken = self.check(gpio_ready=False)
        self.assertEqual(broken["level"], "error")
        self.assertIn("cabo", broken["detail"])
        # A visitor standing in front of Noha for long is not a wiring problem, unlike a stuck pin.
        self.assertEqual(self.check(motion=True, updated=10_000, motion_history=[[10_000 - 900, None]])["level"], "ok")


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_requested_from_the_voice_service_and_deleted_once_read(self):
        with TemporaryDirectory() as folder:
            runtime = Path(folder)

            async def answer():
                while not (runtime / "snapshot").exists():
                    await asyncio.sleep(0.01)
                (runtime / "snapshot.json").write_text(json.dumps({"image": "data:image/jpeg;base64,AA", "people": []}))
            helper = admin.Admin()
            with patch.object(admin, "voice_runtime", return_value=runtime), \
                    patch.object(admin, "voice_status", return_value={"stale": False, "source": "camera", "gpio_ready": True}):
                task = asyncio.create_task(answer())
                shot = await helper.snapshot({})
                await task
            self.assertEqual(shot["people"], [])
            self.assertFalse((runtime / "snapshot.json").exists())
            self.assertFalse((runtime / "snapshot").exists())
            with patch.object(admin, "voice_runtime", return_value=runtime), \
                    patch.object(admin, "voice_status", return_value={"stale": False, "source": "gpio"}):
                with self.assertRaisesRegex(ValueError, "sensor de movimento"):
                    await helper.snapshot({})

    async def test_switching_to_the_camera_needs_a_working_camera(self):
        async def command(*args, **kwargs):
            return json.dumps(["Nenhuma câmera encontrada: confira o cabo flat"])
        with patch.object(admin, "command", side_effect=command), \
                patch.object(admin, "account", return_value=SimpleNamespace(pw_name="noha")):
            with self.assertRaisesRegex(ValueError, "Nenhuma câmera"):
                await admin.validate_camera()


if __name__ == "__main__":
    unittest.main()
