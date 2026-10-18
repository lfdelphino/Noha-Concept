import asyncio
import json
import logging
import os
from pathlib import Path
import time

from .audio import AudioIO
from .motion import open_sensor
from .realtime import APIProblem, conversation
from .state import MotionGate
from .metrics import record
from .models import model_info
from importlib.metadata import version

log = logging.getLogger(__name__)
_status = {}


def runtime_dir():
    root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "noha"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def heartbeat(**fields):
    _status.update(fields)
    _status.update(updated=time.time(), pid=os.getpid(), version=version("noha-robot"))
    path = runtime_dir() / "status.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(_status), encoding="utf-8")
    temp.replace(path)


def update_status(state):
    heartbeat(state=state)
    log.info("voice state=%s", state)


async def run(config, simulate=False):
    if model_info(config.voice.model).transport == "live":
        from .live import conversation as adapter
    else:
        adapter = conversation
    heartbeat(model=config.voice.model, gpio=config.motion.gpio, audio_ready=False, busy=False)
    sensor = None
    active = None
    trigger = runtime_dir() / "trigger"
    trigger.unlink(missing_ok=True)
    gate = MotionGate(time.monotonic(), config.motion.warmup_seconds,
                      config.motion.rearm_seconds, config.motion.greeting_cooldown_seconds)
    next_sensor_retry = 0
    next_heartbeat = next_audio_check = 0
    audio_ready = False
    if simulate:
        trigger.touch()
    update_status("aquecendo_sensor")

    async def session_attempts():
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            update_status("chave_ausente: configure /etc/noha/secrets.env")
            return
        greet = True
        for attempt, delay in enumerate((1, 2, 4, 8, 16)):
            audio = AudioIO(config.audio)
            try:
                instructions = Path(config.voice.instructions).read_text(encoding="utf-8")
                audio.start()
                update_status("conectando")
                def connected(state):
                    nonlocal greet
                    if greet:
                        record(config.voice.model, conversations=1)
                    update_status(state)
                    greet = False
                await adapter(config.voice, key, instructions, audio, greet=greet, state_callback=connected)
                update_status("aguardando_movimento")
                return
            except APIProblem as exc:
                record(config.voice.model, errors=1)
                # APIProblem contains only a sanitized code, never a raw server message.
                log.error("OpenAI code=%s", exc)
                update_status("erro_api: confira chave, saldo e acesso ao modelo")
                return
            except Exception as exc:
                record(config.voice.model, errors=1)
                log.warning("Sessão interrompida type=%s tentativa=%s", type(exc).__name__, attempt + 1)
                update_status("recuperando_audio_ou_conexao")
                # HTTP 401/403/429 from the WebSocket handshake are not network loss.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (400, 401, 403, 404, 429):
                    update_status(f"erro_api_http_{status}")
                    return
            finally:
                audio.close()
            if attempt < 4:
                record(config.voice.model, reconnects=1)
                await asyncio.sleep(delay)
        update_status("indisponivel: execute noha doctor")

    async def session():
        started = time.monotonic()
        try:
            await session_attempts()
        finally:
            record(config.voice.model, duration_seconds=time.monotonic() - started)

    try:
        while True:
            now = time.monotonic()
            if sensor is None and now >= next_sensor_retry:
                try:
                    sensor = open_sensor(config.motion.gpio)
                    update_status("aquecendo_sensor" if now - gate.boot < gate.warmup else "aguardando_movimento")
                except Exception as exc:
                    log.error("GPIO indisponível type=%s; confira BCM%s com noha doctor", type(exc).__name__, config.motion.gpio)
                    next_sensor_retry = now + 30
                    update_status("gpio_indisponivel")
            if active and active.done():
                try:
                    active.result()
                except Exception as exc:
                    log.error("Conversa encerrada type=%s", type(exc).__name__)
                active = None
            manual = trigger.exists()
            if manual:
                trigger.unlink(missing_ok=True)
            detected = bool(sensor and sensor.value)
            updating = any((runtime_dir() / name).exists() for name in ("update-pending", "config-pending"))
            motion_trigger = gate.update(now, detected, active is not None or updating)
            if active is None and not updating and (manual or motion_trigger):
                if manual:
                    gate.armed = False
                    gate.last_greeting = now
                heartbeat(busy=True)
                active = asyncio.create_task(session())
            if now >= next_audio_check:
                from .audio import devices
                try:
                    names = {d["name"] for d in await asyncio.to_thread(devices)}
                    audio_ready = all(n and n in names for n in (config.audio.microphone, config.audio.speaker,
                                                                config.audio.clean_source, config.audio.reference_sink))
                except Exception:
                    audio_ready = False
                next_audio_check = now + 10
            if now >= next_heartbeat:
                ready = bool(sensor and audio_ready and os.environ.get("OPENAI_API_KEY", "").strip()
                             and Path(config.voice.instructions).is_file())
                heartbeat(busy=active is not None, ready=ready, updating=updating,
                          audio_ready=audio_ready, gpio_ready=sensor is not None)
                next_heartbeat = now + 2
            await asyncio.sleep(0.1)
    finally:
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if sensor:
            sensor.close()
        update_status("parado")

