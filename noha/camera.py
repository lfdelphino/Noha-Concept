"""Person detection with the Pi camera, entirely on the Pi: no picture is stored or leaves the robot.

YOLOX-Nano (Megvii, Apache-2.0: data/yolox_nano.onnx) on ONNX Runtime finds people in a small frame a few times
per second. `CameraSensor` has the motion sensor's interface (`value`, `close`), so the visit logic is the same."""
import base64
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time

import numpy as np

log = logging.getLogger(__name__)
SIZE = 416  # the model's square input
FRAME = (416, 312)  # 4:3, scaled by the camera's ISP: the whole field of view, no resizing in Python
FPS = 5  # frames analysed per second; a slow frame rate also allows the long exposures a dim room needs
PERSON = 0  # COCO class index
CAPTURE_SECONDS = 5  # no frame for this long: the ribbon cable or the sensor failed
STALL_SECONDS = 15


@lru_cache
def grid(size=SIZE):
    """(cell x and y, stride) of every YOLOX prediction, in the order of the model's output."""
    cells, strides = [], []
    for stride in (8, 16, 32):
        side = size // stride
        rows, columns = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
        cells.append(np.stack((columns, rows), 2).reshape(-1, 2))
        strides.append(np.full((side * side, 1), stride))
    return np.concatenate(cells), np.concatenate(strides)


def overlap(a, b):
    """Intersection over union of two (left, top, right, bottom) boxes."""
    width, height = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    shared = width * height
    return shared / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - shared)


def people(output, width, height, threshold, size=SIZE):
    """People in one YOLOX output (predictions x 85): [(score, left, top, right, bottom)], most confident first.
    Edges are fractions of the width x height picture; overlapping boxes of one person are merged."""
    score = output[:, 4] * output[:, 5 + PERSON]
    keep = score >= threshold
    if not keep.any():
        return []
    cells, strides = grid(size)
    found, score = output[keep], score[keep]
    centre = (found[:, :2] + cells[keep]) * strides[keep]
    half = np.exp(np.minimum(found[:, 2:4], 10)) * strides[keep] / 2
    boxes = (np.concatenate((centre - half, centre + half), 1) / (width, height, width, height)).clip(0, 1)
    result = []
    for index in np.argsort(-score):
        box = tuple(float(edge) for edge in boxes[index])
        if all(overlap(box, other[1:]) < 0.5 for other in result):
            result.append((float(score[index]), *box))
    return result


def upright(frame, rotation):
    """The frame turned `rotation` degrees clockwise (0, 90, 180 or 270)."""
    return np.rot90(frame, -rotation // 90) if rotation else frame


class Detector:
    """YOLOX-Nano on two CPU threads: the voice pipeline keeps the other cores."""
    def __init__(self, threads=2):
        import onnxruntime
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = threads, 1
        options.log_severity_level = 3
        model = files("noha").joinpath("data/yolox_nano.onnx").read_bytes()
        with filtered_stderr():
            self.session = onnxruntime.InferenceSession(model, options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def __call__(self, frame, threshold):
        """People in an upright BGR frame no larger than SIZE on either side."""
        height, width = frame.shape[:2]
        canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)  # YOLOX pads with grey, picture at the top left
        canvas[:height, :width] = frame
        blob = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        return people(self.session.run(None, {self.input: blob})[0][0], width, height, threshold)


class Presence:
    """Steadies the detections: on after `frames` in a row, off `hold` seconds after the last one.
    One wrong frame never greets, and a visitor missed in one frame has not left."""
    def __init__(self, frames=2, hold=1.5):
        self.frames, self.hold = frames, hold
        self.streak, self.last, self.value = 0, None, False

    def update(self, now, seen):
        if seen:
            self.streak += 1
            self.last = now
            if self.streak >= self.frames:
                self.value = True
        else:
            self.streak = 0
            if self.value and now - self.last >= self.hold:
                self.value = False
        return self.value


@contextmanager
def filtered_stderr(noise=b"Schema error"):
    """Drop one kind of harmless native-library line from stderr (the journal), keeping every other line.

    Debian's python3-onnxruntime loads two copies of the runtime that both register the ONNX operator schemas
    in the shared libonnx: ~570 "Schema error" lines each time a model is loaded, with no effect on results."""
    sys.stderr.flush()
    saved = os.dup(2)
    with tempfile.TemporaryFile() as capture:
        os.dup2(capture.fileno(), 2)
        try:
            yield
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
            capture.seek(0)
            kept = b"".join(line for line in capture if noise not in line)
            if kept:
                os.write(2, kept)


def open_picamera():
    """The first camera, streaming small frames. Blue-green-red byte order, as the model expects."""
    from picamera2 import Picamera2
    camera = Picamera2()
    try:
        camera.configure(camera.create_video_configuration(
            main={"size": FRAME, "format": "RGB888"}, controls={"FrameRate": FPS}, buffer_count=2))
        camera.start()
    except Exception:
        camera.close()
        raise
    return camera


def snapshot(frame, found, min_height):
    """JPEG data URL of what the detector saw: people who count outlined in green, the others in orange."""
    from PIL import Image, ImageDraw
    image = Image.fromarray(np.ascontiguousarray(frame[:, :, ::-1]))
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for _, left, top, right, bottom in reversed(found):
        colour = (40, 220, 90) if bottom - top >= min_height else (255, 150, 0)
        draw.rectangle((left * width, top * height, right * width - 1, bottom * height - 1), outline=colour, width=3)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class CameraSensor:
    """`value` is True while a person stands close enough. A thread reads and analyses the frames."""

    def __init__(self, settings, runtime, capture=None, detect=None, clock=time.monotonic, start=True):
        self.settings, self.runtime, self.clock = settings, runtime, clock
        self.detect = detect or Detector()
        self.camera = None
        if capture is None:
            self.camera = open_picamera()
            capture = lambda: self.camera.capture_array("main", wait=CAPTURE_SECONDS)  # noqa: E731
        self.capture = capture
        self.presence = Presence()
        self.value, self.error, self.last_frame = False, None, clock()
        self.info = {"people": 0, "seen": 0, "height": 0}
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="noha-camera", daemon=True)
        if start:
            self.thread.start()

    @property
    def failed(self):
        """The camera stopped delivering frames: the robot closes this sensor and opens a new one later."""
        return self.error is not None or self.clock() - self.last_frame > STALL_SECONDS

    def step(self):
        frame = upright(self.capture(), self.settings.rotation)
        found = self.detect(frame, self.settings.confidence / 100)
        minimum = self.settings.min_height / 100
        heights = [bottom - top for _, _, top, _, bottom in found]
        near = sum(height >= minimum for height in heights)
        now = self.clock()
        self.last_frame = now
        self.info = {"people": near, "seen": len(found), "height": round(100 * max(heights, default=0))}
        self.value = self.presence.update(now, near > 0)
        request = self.runtime / "snapshot"  # the dashboard asks to see what the camera sees
        if request.exists():
            request.unlink(missing_ok=True)
            result = {"at": time.time(), "image": snapshot(frame, found, minimum), "min_height": self.settings.min_height,
                      "people": [{"height": round(100 * height), "score": round(100 * person[0]), "counts": height >= minimum}
                                 for person, height in zip(found, heights)]}
            temp = self.runtime / "snapshot.tmp"
            temp.write_text(json.dumps(result), encoding="utf-8")
            temp.replace(self.runtime / "snapshot.json")

    def run(self):
        try:
            while not self.stopping.is_set():
                self.step()
        except Exception as exc:
            self.error = type(exc).__name__
            log.error("Câmera interrompida type=%s", self.error)
        finally:
            self.value = False
            if self.camera is not None:
                try:
                    self.camera.close()
                except Exception:
                    pass

    def close(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(3)
        elif self.camera is not None and self.error is None:  # never started: release the camera here
            self.camera.close()
