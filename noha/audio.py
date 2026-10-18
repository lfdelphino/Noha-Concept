"""GStreamer audio through PipeWire's Pulse compatibility server and AEC nodes."""
import asyncio
from collections import deque
import json
import subprocess

RATE = 24000
BYTES_PER_SECOND = RATE * 2


def devices():
    result = subprocess.run(["pw-dump"], check=True, capture_output=True, text=True, timeout=8)
    nodes = []
    for entry in json.loads(result.stdout):
        props = entry.get("info", {}).get("props", {})
        if props.get("media.class") in {"Audio/Source", "Audio/Sink"}:
            nodes.append({"name": props.get("node.name", ""),
                          "description": props.get("node.description", ""),
                          "class": props["media.class"]})
    return nodes


class AudioIO:
    def __init__(self, config):
        self.config = config
        self.input = asyncio.Queue(maxsize=100)  # two seconds; abort instead of sending stale speech
        self.failure = None
        self.capture = self.playback = None
        self.spans = deque()
        self.last_end = 0

    def start(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst = Gst
        names = {d["name"] for d in devices()}
        required = (self.config.microphone, self.config.speaker,
                    self.config.clean_source, self.config.reference_sink)
        if any(not n or n not in names for n in required):
            raise RuntimeError("Dispositivo USB ou cancelamento de eco ausente; execute noha doctor")
        self.loop = asyncio.get_running_loop()
        # Set device properties separately: device identifiers never become pipeline syntax.
        self.capture = Gst.parse_launch(
            'pulsesrc name=mic buffer-time=80000 latency-time=20000 ! audioconvert ! audioresample ! '
            'audio/x-raw,format=S16LE,rate=24000,channels=1,layout=interleaved ! '
            'appsink name=capture emit-signals=true sync=false max-buffers=10 drop=false')
        self.capture.get_by_name("mic").set_property("device", self.config.clean_source)
        self.capture.get_by_name("capture").connect("new-sample", self._sample)
        self.playback = Gst.parse_launch(
            'appsrc name=output format=time is-live=true block=false '
            'caps="audio/x-raw,format=S16LE,rate=24000,channels=1,layout=interleaved" ! '
            'audioconvert ! audioresample ! '
            'pulsesink name=speaker sync=true async=false buffer-time=80000 latency-time=20000')
        self.playback.get_by_name("speaker").set_property("device", self.config.reference_sink)
        self.output = self.playback.get_by_name("output")
        for pipeline, state in ((self.playback, Gst.State.PLAYING), (self.capture, Gst.State.READY)):
            if pipeline.set_state(state) == Gst.StateChangeReturn.FAILURE:
                self.close()
                raise RuntimeError("Falha ao iniciar áudio")

    def start_capture(self):
        if self.capture.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Falha ao iniciar microfone")

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        self.loop.call_soon_threadsafe(self._enqueue, data)
        return self.Gst.FlowReturn.OK

    def _enqueue(self, data):
        if self.capture is None:
            return
        if self.input.full():
            self.failure = RuntimeError("Áudio de entrada atrasado; reconectando")
        else:
            self.input.put_nowait(data)

    def check(self):
        if self.failure:
            raise self.failure
        for pipeline in (self.capture, self.playback):
            if pipeline:
                message = pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
                if message:
                    raise RuntimeError("Dispositivo de áudio interrompido; verifique USB e PipeWire")

    def running_ns(self):
        if self.playback is None or self.playback.get_clock() is None:
            return 0
        return max(0, self.playback.get_clock().get_time() - self.playback.get_base_time())

    def push(self, data):
        self.check()
        now = self.running_ns()
        if self.last_end - now > 30 * self.Gst.SECOND:
            raise RuntimeError("Fila de reprodução excedeu 30 segundos")
        # Timestamp each chunk against the playback clock. Gaps are not counted as spoken audio.
        start = max(now + 20_000_000, self.last_end)
        duration = len(data) * self.Gst.SECOND // BYTES_PER_SECOND
        buffer = self.Gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        buffer.pts = start
        buffer.duration = duration
        if self.output.emit("push-buffer", buffer) != self.Gst.FlowReturn.OK:
            raise RuntimeError("Falha ao reproduzir áudio")
        self.spans.append((start, duration))
        self.last_end = start + duration

    def begin_item(self):
        self.spans.clear()

    def played_ms(self):
        # Conservative device-latency allowance; never equate downloaded bytes with heard audio.
        now = self.running_ns() - self.config.playback_latency_ms * 1_000_000
        return sum(max(0, min(duration, now - start)) for start, duration in self.spans) // 1_000_000

    def busy(self):
        return self.last_end > 0 and self.running_ns() < self.last_end + self.config.playback_latency_ms * 1_000_000

    def flush(self):
        if self.playback:
            # READY drops appsrc AND sink buffers; resetting only the Python queue is insufficient.
            self.playback.set_state(self.Gst.State.READY)
            self.playback.get_state(self.Gst.SECOND)
            self.playback.set_state(self.Gst.State.PLAYING)
        self.last_end = 0
        self.spans.clear()

    def close(self):
        for name in ("capture", "playback"):
            pipeline = getattr(self, name)
            setattr(self, name, None)
            if pipeline:
                pipeline.set_state(self.Gst.State.NULL)

