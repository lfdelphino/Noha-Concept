"""Local greeting: motion plays a pre-synthesized clip and only a voice reply opens a paid session."""
import asyncio
from array import array
from collections import deque
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import wave

from .audio import BYTES_PER_SECOND
from .metrics import record
from .realtime import ECHO_TAIL_MS

log = logging.getLogger(__name__)
TTS_PER_MINUTE = 0.015  # gpt-4o-mini-tts estimate, USD per minute of generated audio
CALIBRATE_SECONDS = 0.3  # room noise measured before Noha speaks
PREROLL_SECONDS = 1.5  # speech onset kept from before the local detection
SPEECH_MS = 300  # local check only; the session's own VAD and min_speech_ms still filter noise
GAP_MS = 250
FLOOR_DBFS = -55  # a silent room must not make faint sounds count as speech


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "noha" / "greetings"


def clip_name(greeting, voice, text):
    key = json.dumps([greeting.tts_model, voice, greeting.style, text], ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:20] + ".pcm"


async def synthesize(session, key, greeting, voice, text):
    """24 kHz mono PCM16, the playback pipeline's own format."""
    body = {"model": greeting.tts_model, "voice": voice, "input": text, "response_format": "pcm"}
    if greeting.style:
        body["instructions"] = greeting.style
    async with session.post("https://api.openai.com/v1/audio/speech", json=body,
                            headers={"Authorization": f"Bearer {key}"}) as response:
        if response.status != 200:
            raise RuntimeError(f"http_{response.status}")  # never the server's message
        return await response.read()


async def prepare(config, key, texts, folder=None, session=None):
    """[(text, pcm)] for every text, synthesizing only what the cache lacks (once per voice and text)."""
    folder = folder or cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {clip_name(config.greeting, config.voice.voice, text): text for text in texts}
    missing = [name for name in wanted if not (folder / name).is_file()]
    if missing:
        from aiohttp import ClientSession, ClientTimeout
        owner = session is None
        session = session or ClientSession(timeout=ClientTimeout(total=60))
        try:
            for name in missing:
                pcm = await synthesize(session, key, config.greeting, config.voice.voice, wanted[name])
                if not pcm or len(pcm) % 2:
                    raise RuntimeError("audio_invalido")
                temp = folder / (name + ".tmp")
                temp.write_bytes(pcm)
                temp.replace(folder / name)
                record(config.greeting.tts_model, cost_usd=len(pcm) / BYTES_PER_SECOND / 60 * TTS_PER_MINUTE)
                log.info("Saudação sintetizada seconds=%.1f", len(pcm) / BYTES_PER_SECOND)
        finally:
            if owner:
                await session.close()
    for stale in folder.glob("*.pcm"):
        if stale.name not in wanted:
            stale.unlink(missing_ok=True)  # previous voice, style or texts
    return [(text, (folder / clip_name(config.greeting, config.voice.voice, text)).read_bytes()) for text in texts]


def setup_text(network, ssid):
    """Wi-Fi setup steps spoken when a visitor talks while Noha is offline (see README, Wi-Fi pelo celular)."""
    name = ("Noha Setup " + " ".join(ssid.removeprefix("Noha-Setup-"))) if ssid.startswith("Noha-Setup-")         else ssid.replace("-", " ").replace("_", " ")
    address = " ponto ".join(network.gateway.split("."))
    return (f"Estou sem internet e preciso de ajuda para me conectar. No celular, entre na rede Wi-Fi aberta {name}. "
            f"A página de configuração costuma abrir sozinha. Se não abrir, acesse no navegador o endereço {address} "
            "e confirme que deseja continuar na rede sem internet. Depois escolha a rede Wi-Fi do local e digite a senha. "
            "Se a rede Noha Setup ainda não aparecer, aguarde um minuto e tente de novo.")


def resample(pcm, rate, target=24000):
    """Linear interpolation; enough for speech."""
    source = array("h", pcm[:len(pcm) - len(pcm) % 2])
    if rate == target or not source:
        return source.tobytes()
    out = array("h", bytes(2 * (len(source) * target // rate)))
    last = len(source) - 1
    for i in range(len(out)):
        position = i * rate / target
        j = int(position)
        a, b = source[j], source[min(j + 1, last)]
        out[i] = int(a + (b - a) * (position - j))
    return out.tobytes()


def espeak(text, voice="pt-br"):
    """Offline speech for when no synthesized clip exists yet (first boot without internet or key)."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "speech.wav"
        subprocess.run(["espeak-ng", "-v", voice, "-s", "150", "-w", str(path), text],
                       check=True, capture_output=True, timeout=30)
        with wave.open(str(path)) as clip:
            if clip.getsampwidth() != 2 or clip.getnchannels() != 1:
                raise RuntimeError("formato_espeak")
            return resample(clip.readframes(clip.getnframes()), clip.getframerate())


async def play(audio, pcm):
    """Speak a clip to the end, draining the microphone so its queue never overflows meanwhile."""
    audio.push(pcm)
    while audio.busy():
        audio.check()
        while not audio.input.empty():
            audio.input.get_nowait()
        await asyncio.sleep(0.05)


def dbfs(chunk):
    samples = array("h", chunk[:len(chunk) - len(chunk) % 2])
    if not samples:
        return -100.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(max(rms, 1) / 32768)


class SpeechDetector:
    """Free on-device check that someone talks: sustained level above an adaptive noise floor."""
    def __init__(self, margin_db=12, speech_ms=SPEECH_MS, gap_ms=GAP_MS):
        self.margin, self.speech_ms, self.gap_ms = margin_db, speech_ms, gap_ms
        self.floor = None
        self.voiced = self.gap = 0
        self.peak = -100.0

    def calibrate(self, chunk):
        level = dbfs(chunk)
        self.floor = level if self.floor is None else 0.7 * self.floor + 0.3 * level

    def feed(self, chunk):
        level, ms = dbfs(chunk), len(chunk) * 1000 / BYTES_PER_SECOND
        self.peak = max(self.peak, level)
        if self.floor is None:
            self.floor = level
        loud = level >= max(self.floor, FLOOR_DBFS) + self.margin
        if loud:
            self.voiced += ms
            self.gap = 0
        else:
            self.gap += ms
            if self.gap > self.gap_ms:
                self.voiced = 0
        # Follow the room: drop at once to quieter levels, rise slowly with steady noise (a TV switched on);
        # a few hundred milliseconds of speech barely move it.
        self.floor = level if level < self.floor else self.floor + (0.005 if loud else 0.02) * (level - self.floor)
        return self.voiced >= self.speech_ms


async def greet_and_listen(audio, pcm, listen_seconds, detector, clock=time.monotonic):
    """Play the greeting (none: only listen), then listen up to listen_seconds after it. The recent
    microphone chunks when someone talks (the session's pre-roll), else None. Nothing here reaches the API."""
    audio.start_capture()
    recent = deque(maxlen=int(PREROLL_SECONDS * 50))  # 20 ms capture chunks
    speak_at = clock() + CALIBRATE_SECONDS
    played = False
    deadline = None
    while True:
        audio.check()
        try:
            data = await asyncio.wait_for(audio.input.get(), 0.2)  # draining keeps the queue from overflowing
        except TimeoutError:
            data = None
        now = clock()
        if not played:
            if data:
                detector.calibrate(data)
            if now >= speak_at:
                if pcm:
                    audio.push(pcm)
                played = True
            continue
        if audio.busy(ECHO_TAIL_MS):
            continue  # Noha's own voice and its echo
        deadline = deadline or now + listen_seconds
        if data:
            recent.append(data)
            if detector.feed(data):
                return list(recent)
        if now >= deadline:
            return None


class Preroll:
    """Microphone audio from the local detection until the session is ready to receive it.
    Pumping the queue meanwhile keeps a slow connection from overflowing it."""
    def __init__(self, audio, chunks, seconds=10):
        self.audio = audio
        self.chunks = deque(chunks, maxlen=seconds * 50)
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            self.chunks.append(await self.audio.input.get())

    def take(self):
        """Everything buffered, as one PCM block; the caller reads the queue from now on."""
        self.close()
        data = b"".join(self.chunks)
        self.chunks.clear()
        return data

    def close(self):
        self.task.cancel()
