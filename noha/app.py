import asyncio
import json
import logging
import os
from pathlib import Path
import random
import time

from .audio import AudioIO, AudioUnavailable, echo_cancel_dropped, missing, profile_missing
from .config import presence_enabled
from .controller import internet_available
from .greeting import Preroll, SpeechDetector, espeak, greet_and_listen, play, prepare, setup_text
from .leds import Ring, pattern_for
from .motion import open_sensor
from .network import hotspot_ssid
from .realtime import APIProblem, api_message, close_problem, conversation, over_budget, safe_code
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
                      config.motion.rearm_seconds, config.motion.greeting_cooldown_seconds,
                      config.motion.confirm_seconds, config.motion.confirm_pulses, config.motion.confirm_window_seconds)
    clips = []  # (text, pcm) greetings played locally; until synthesized the voice model greets
    try:
        setup = setup_text(config.network, hotspot_ssid(config.network))
    except OSError:
        setup = setup_text(config.network, "Noha-Setup")
    setup_clip = None  # Wi-Fi instructions in Noha's voice once synthesized; espeak-ng before that
    prepared = False
    preparing = None
    next_clips_try = next_presence_check = 0
    enabled = presence_enabled()
    motion = MotionLog()
    next_sensor_retry = 0
    next_heartbeat = next_audio_check = next_aec_restart = 0
    audio_ready = False
    audio_missing = []
    aec_backoff = 120
    if simulate:
        trigger.touch()
    update_status("aquecendo_sensor")

    async def session_attempts(key, greeting=None, audio=None, preroll=None):
        """greeting: text already played locally, with the visitor's answer in preroll and audio still open."""
        nonlocal current_audio
        greet = greeting is None
        counted = False
        failure = None
        for attempt, delay in enumerate((1, 2, 4, 8, 16)):
            audio = current_audio = audio or AudioIO(config.audio)
            try:
                instructions = Path(config.voice.instructions).read_text(encoding="utf-8")
                if audio.playback is None:
                    audio.start()
                update_status("conectando")
                def connected(state):
                    nonlocal greet, counted
                    if not counted:
                        record(config.voice.model, conversations=1)
                        counted = True
                    update_status(state)
                    greet = False
                await adapter(config.voice, key, instructions, audio, greet=greet, state_callback=connected,
                              greeting=greeting, preroll=preroll)
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
                if preroll:
                    preroll.close()
                    preroll = None
                audio.close()
                audio = current_audio = None
            if attempt < 4:
                record(config.voice.model, reconnects=1)
                await asyncio.sleep(delay)
        update_status("sem_conexao", f"A conversa caiu 5 vezes seguidas ({failure}); confira a internet do robô")

    async def online():
        try:
            return await internet_available(config.network)
        except Exception:
            return False

    async def visit():
        # Checked while Noha greets: offline, a visitor who talks hears how to set up the Wi-Fi.
        probe = asyncio.create_task(online())
        try:
            await local_visit(probe)
        finally:
            probe.cancel()

    async def local_visit(probe):
        nonlocal current_audio
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        text = pcm = None
        if clips:
            text, pcm = random.choice(clips)
        elif await probe:
            if not key:
                update_status("chave_ausente", "Informe a chave OpenAI em Configurações")
                return
            await session(key)  # nothing synthesized yet: the voice model greets
            return
        # Free until the visitor answers: a passer-by never opens a paid session.
        audio = current_audio = AudioIO(config.audio)
        detector = SpeechDetector(config.greeting.speech_margin_db)
        heard = None
        try:
            audio.start()
            if pcm:
                update_status("cumprimentando")
                record(config.voice.model, greetings=1)
            heard = await greet_and_listen(audio, pcm, config.voice.unanswered_seconds, detector)
        except AudioUnavailable as exc:
            log.error("Áudio ausente: %s", exc)
            update_status("audio_indisponivel", f"Não encontrado: {exc}. Confira cabos, tela e a seleção em Configurações")
            return
        except Exception as exc:
            log.error("Saudação local interrompida type=%s", type(exc).__name__)
            update_status("aguardando_movimento")
            return
        finally:
            if heard is None:
                audio.close()
                current_audio = None
        # Levels only: tune [greeting] speech_margin_db from these if speech is missed or noise connects.
        levels = f"piso_dbfs={detector.floor or -100:.0f} pico_dbfs={detector.peak:.0f}"
        if heard is None:
            log.info("Saudação sem resposta %s; nenhuma chamada à API", levels)
            update_status("aguardando_movimento")
            return
        if not await probe:
            await setup_help(audio, levels)
            return
        if not key:
            audio.close()
            current_audio = None
            update_status("chave_ausente", "Informe a chave OpenAI em Configurações")
            return
        log.info("Fala após a saudação %s; conectando", levels)
        await session(key, greeting=text, audio=audio, preroll=Preroll(audio, heard))

    async def setup_help(audio, levels):
        nonlocal current_audio, setup_clip
        log.info("Fala sem internet %s; explicando a configuração do Wi-Fi", levels)
        update_status("orientando_wifi", "Sem internet: Noha explica como configurar o Wi-Fi pelo celular")
        try:
            if setup_clip is None:
                setup_clip = await asyncio.to_thread(espeak, setup)
            await play(audio, setup_clip)
        except FileNotFoundError:
            log.warning("espeak-ng ausente: sem instruções faladas até a voz da Noha ser sintetizada; reexecute o instalador")
        except Exception as exc:
            log.error("Instruções de Wi-Fi interrompidas type=%s", type(exc).__name__)
        finally:
            audio.close()
            current_audio = None
        update_status("aguardando_movimento")

    async def session(*args, **kwargs):
        # Conversation time only: a local greeting nobody answers is free and not counted.
        started = time.monotonic()
        try:
            await session_attempts(*args, **kwargs)
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
            if not prepared and preparing is None and now >= next_clips_try \
                    and (key := os.environ.get("OPENAI_API_KEY", "").strip()):
                texts = [*(config.greeting.texts if config.greeting.local else []), setup]
                preparing = asyncio.create_task(prepare(config, key, texts))
            if preparing and preparing.done():
                try:
                    *clips, (_, setup_clip) = preparing.result()
                    prepared = True
                    log.info("Saudações locais prontas count=%d", len(clips))
                except Exception as exc:
                    log.warning("Saudações locais indisponíveis type=%s code=%s; o modelo cumprimenta até nova tentativa",
                                type(exc).__name__, safe_code(str(exc)))
                preparing = None
                next_clips_try = now + 600
            if now >= next_presence_check:
                enabled = presence_enabled()
                next_presence_check = now + 1
                current = _status.get("state")
                if not enabled and active is None and current in ("aguardando_movimento", "aquecendo_sensor"):
                    update_status("sensor_desligado", "Saudações por movimento desligadas no painel")
                    next_heartbeat = 0
                elif enabled and current == "sensor_desligado":
                    update_status("aguardando_movimento")
                    next_heartbeat = 0
            manual = trigger.exists()
            if manual:
                trigger.unlink(missing_ok=True)
            detected = bool(sensor and sensor.value)
            if motion.update(time.time(), detected):
                next_heartbeat = 0  # show motion edges on the dashboard right away
            updating = any((runtime_dir() / name).exists() for name in ("update-pending", "config-pending"))
            # Switched off, the sensor still reports motion (to tune it) but never greets; manual triggers still work.
            motion_trigger = gate.update(now, detected, active is not None or updating or not enabled)
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
                    active = asyncio.create_task(visit())
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
                if not enabled and active is None:
                    gate_state, gate_seconds = "desligado", 0
                heartbeat(busy=active is not None, ready=ready, updating=updating,
                          audio_ready=audio_ready, audio_missing=audio_missing, gpio_ready=sensor is not None,
                          motion=detected, last_motion=motion.last, motion_history=motion.snapshot(),
                          gate=gate_state, gate_seconds=round(gate_seconds), sensor_enabled=enabled,
                          local_greetings=len(clips))
                next_heartbeat = now + 2
            await asyncio.sleep(0.1)
    finally:
        for task in (active, preparing):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if sensor:
            sensor.close()
        ring.close()
        update_status("parado")

