import asyncio
import json
import logging
import os
from pathlib import Path
import time

from .audio import AudioIO, AudioUnavailable, echo_cancel_dropped, missing, profile_missing
from .leds import Ring, pattern_for
from .motion import open_sensor
from .realtime import APIProblem, api_message, close_problem, conversation, over_budget
from .state import MotionGate, MotionLog
from .metrics import record, spent_today
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


async def restart_audio_stack():
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "pipewire.service", "pipewire-pulse.service", "wireplumber.service",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), 30)
    except TimeoutError:
        process.kill()
        await process.wait()


def update_status(state, detail=""):
    # detail is our own Portuguese explanation for the dashboard; never a raw server message.
    heartbeat(state=state, detail=detail)
    log.info("voice state=%s%s", state, f" detail={detail}" if detail else "")


async def run(config, simulate=False):
    if model_info(config.voice.model).transport == "live":
        from .live import conversation as adapter
    else:
        adapter = conversation
    heartbeat(model=config.voice.model, gpio=config.motion.gpio, audio_ready=False, busy=False)
    sensor = None
    active = None
    current_audio = None
    ring = Ring(config.leds)
    trigger = runtime_dir() / "trigger"
    trigger.unlink(missing_ok=True)
    gate = MotionGate(time.monotonic(), config.motion.warmup_seconds,
                      config.motion.rearm_seconds, config.motion.greeting_cooldown_seconds)
    motion = MotionLog()
    next_sensor_retry = 0
    next_heartbeat = next_audio_check = next_aec_restart = 0
    audio_ready = False
    audio_missing = []
    aec_backoff = 120
    if simulate:
        trigger.touch()
    update_status("aquecendo_sensor")

    async def session_attempts():
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            update_status("chave_ausente", "Informe a chave OpenAI em Configurações")
            return
        nonlocal current_audio
        greet = True
        failure = None
        for attempt, delay in enumerate((1, 2, 4, 8, 16)):
            audio = current_audio = AudioIO(config.audio)
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
            except AudioUnavailable as exc:
                log.error("Áudio ausente: %s", exc)
                update_status("audio_indisponivel", f"Não encontrado: {exc}. Confira cabos, tela e a seleção em Configurações")
                return
            except Exception as exc:
                record(config.voice.model, errors=1)
                # HTTP 401/403/429 from the WebSocket handshake and error close frames are not network loss.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                problem = exc if isinstance(exc, APIProblem) else close_problem(exc)
                if problem or status in (400, 401, 403, 404, 429):
                    code = str(problem) if problem else f"http_{status}"
                    # APIProblem contains only a sanitized code, never a raw server message.
                    log.error("OpenAI code=%s", code)
                    update_status("erro_api", api_message(code))
                    return
                failure = type(exc).__name__
                log.warning("Sessão interrompida type=%s tentativa=%s", failure, attempt + 1)
                update_status("recuperando_audio_ou_conexao")
            finally:
                audio.close()
                current_audio = None
            if attempt < 4:
                record(config.voice.model, reconnects=1)
                await asyncio.sleep(delay)
        update_status("sem_conexao", f"A conversa caiu 5 vezes seguidas ({failure}); confira a internet do robô")

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
            if motion.update(time.time(), detected):
                next_heartbeat = 0  # show motion edges on the dashboard right away
            updating = any((runtime_dir() / name).exists() for name in ("update-pending", "config-pending"))
            motion_trigger = gate.update(now, detected, active is not None or updating)
            if active is None and not updating and (manual or motion_trigger):
                if manual:
                    gate.armed = False
                    gate.last_greeting = now
                if over_budget(config.voice):
                    log.warning("Conversa não iniciada: limite diário de custo atingido")
                    update_status("orcamento_esgotado", f"Limite diário de US$ {config.voice.daily_budget_usd:.2f} atingido; "
                                  "Noha volta à meia-noite ou aumente o limite em Configurações")
                else:
                    heartbeat(busy=True)
                    active = asyncio.create_task(session())
            if now >= next_audio_check:
                from .audio import devices
                try:
                    names = {d["name"] for d in await asyncio.to_thread(devices)}
                    audio_missing = missing(config.audio, names)
                    audio_ready = not audio_missing
                    if audio_ready:
                        aec_backoff = 120
                        if _status.get("state") == "audio_indisponivel":
                            update_status("aguardando_movimento")
                    elif active is None and now >= next_aec_restart:
                        reason = ("AEC PipeWire ausente com microfone e alto-falante presentes"
                                  if echo_cancel_dropped(config.audio, names) else
                                  "placa de áudio sem perfil (tela HDMI desligada no boot?)"
                                  if profile_missing(config.audio, names) else None)
                        if reason:
                            log.warning("%s; reiniciando PipeWire", reason)
                            # A display may stay off for hours: back off instead of restarting every 2 min.
                            next_aec_restart, aec_backoff = now + aec_backoff, min(aec_backoff * 2, 1800)
                            await restart_audio_stack()
                except Exception:
                    audio_ready, audio_missing = False, []
                heartbeat(spent_today=round(await asyncio.to_thread(spent_today), 4),
                          daily_budget=config.voice.daily_budget_usd)
                next_audio_check = now + 10
            ring.show(pattern_for(_status.get("state", ""), warming=now - gate.boot < gate.warmup,
                                  speaking=bool(current_audio and current_audio.busy()),
                                  updating=updating))
            if now >= next_heartbeat:
                ready = bool(sensor and audio_ready and os.environ.get("OPENAI_API_KEY", "").strip()
                             and Path(config.voice.instructions).is_file())
                gate_state, gate_seconds = gate.readiness(now, active is not None)
                heartbeat(busy=active is not None, ready=ready, updating=updating,
                          audio_ready=audio_ready, audio_missing=audio_missing, gpio_ready=sensor is not None,
                          motion=detected, last_motion=motion.last, motion_history=motion.snapshot(),
                          gate=gate_state, gate_seconds=round(gate_seconds))
                next_heartbeat = now + 2
            await asyncio.sleep(0.1)
    finally:
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if sensor:
            sensor.close()
        ring.close()
        update_status("parado")

