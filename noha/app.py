"""Voice service: motion or the dashboard start a visit; the heartbeat file tells the dashboard what is happening."""
import asyncio
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import random
import shutil
import time

from .audio import AudioIO, AudioUnavailable, echo_cancel_dropped, missing, profile_missing
from .config import presence_enabled
from .controller import internet_available
from .greeting import OFFLINE_GREETING, Preroll, SpeechDetector, espeak, greet_and_listen, play, prepare, setup_text
from .leds import Ring, pattern_for
from .metrics import record, record_visit, spent_today
from .models import model_info
from .motion import open_sensor
from .network import hotspot_ssid
from .realtime import APIProblem, api_message, close_problem, conversation, over_budget, safe_code
from .state import MotionGate, MotionLog

log = logging.getLogger(__name__)
_status = {}
MARKERS = ("update-pending", "config-pending")  # admin/updater: finish the visit, start no new one


def runtime_dir():
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "noha"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def heartbeat(**fields):
    _status.update(fields)
    _status.update(updated=time.time(), pid=os.getpid(), version=version("noha-robot"))
    path = runtime_dir() / "status.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(_status), encoding="utf-8")
    temp.replace(path)


def update_status(state, detail=""):
    # detail is our own Portuguese explanation for the dashboard; never a raw server message.
    heartbeat(state=state, detail=detail)
    log.info("voice state=%s%s", state, f" detail={detail}" if detail else "")


async def restart_audio_stack():
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "pipewire.service", "pipewire-pulse.service", "wireplumber.service",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(30):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


class Robot:
    """The main loop and one visit at a time. Hardware and network access are injectable for tests."""

    def __init__(self, config, simulate=False, *, adapter=None, audio_factory=AudioIO, sensor_factory=open_sensor,
                 online=None, synthesize=prepare, speak_offline=espeak, clock=time.monotonic):
        self.config = config
        if adapter is None:
            if model_info(config.voice.model).transport == "live":
                from .live import conversation as adapter
            else:
                adapter = conversation
        self.adapter, self.audio_factory, self.sensor_factory = adapter, audio_factory, sensor_factory
        self.online = online or self.internet
        self.synthesize, self.speak_offline, self.clock = synthesize, speak_offline, clock
        self.simulate = simulate
        motion = config.motion
        self.gate = MotionGate(clock(), motion.warmup_seconds, motion.rearm_seconds, motion.greeting_cooldown_seconds,
                               motion.confirm_seconds, motion.confirm_pulses, motion.confirm_window_seconds)
        self.motion = MotionLog()
        self.ring = Ring(config.leds)
        self.sensor = self.active = self.audio = self.preparing = None
        self.clips = []  # (text, pcm) greetings played locally; until synthesized the voice model greets
        self.setup_clip = None  # Wi-Fi instructions in Noha's voice
        self.offline = {}  # espeak-ng renderings, while Noha's voice has not been synthesized
        self.offline_voice = shutil.which("espeak-ng") is not None
        try:
            self.setup = setup_text(config.network, hotspot_ssid(config.network))
        except OSError:
            self.setup = setup_text(config.network, "Noha-Setup")
        self.prepared = False
        self.enabled = presence_enabled()
        self.audio_ready, self.audio_missing, self.aec_backoff = False, [], 120
        self.next_sensor_retry = self.next_clips_try = self.next_presence_check = 0
        self.next_heartbeat = self.next_audio_check = self.next_aec_restart = 0

    @staticmethod
    def key():
        return os.environ.get("OPENAI_API_KEY", "").strip()

    async def internet(self):
        try:
            return await internet_available(self.config.network)
        except Exception:
            return False

    # Main loop ---------------------------------------------------------------------------------

    async def run(self):
        trigger = runtime_dir() / "trigger"
        trigger.unlink(missing_ok=True)
        if self.simulate:
            trigger.touch()
        heartbeat(model=self.config.voice.model, gpio=self.config.motion.gpio, audio_ready=False, busy=False)
        update_status("aquecendo_sensor")
        try:
            while True:
                await self.tick()
                await asyncio.sleep(0.1)
        finally:
            await self.stop()

    async def tick(self):
        now = self.clock()
        self.poll_sensor(now)
        self.reap()
        self.poll_clips(now)
        self.poll_presence(now)
        trigger = runtime_dir() / "trigger"
        manual = trigger.exists()  # `noha trigger` or the dashboard's test button
        if manual:
            trigger.unlink(missing_ok=True)
        detected = bool(self.sensor and self.sensor.value)
        if self.motion.update(time.time(), detected):
            self.next_heartbeat = 0  # show motion edges on the dashboard right away
        updating = any((runtime_dir() / name).exists() for name in MARKERS)
        warming = now - self.gate.boot < self.gate.warmup
        if not warming and _status.get("state") == "aquecendo_sensor":
            update_status("aguardando_movimento" if self.enabled else "sensor_desligado")
        # Switched off, the sensor still reports motion (to tune it) but never greets; manual triggers still work.
        greet = self.gate.update(now, detected, self.active is not None or updating or not self.enabled)
        if self.active is None and not updating and (manual or greet):
            self.start_visit(now, manual)
        if now >= self.next_audio_check:
            await self.check_audio(now)
        self.ring.show(pattern_for(_status.get("state", ""), warming=warming,
                                   speaking=bool(self.audio and self.audio.busy()), updating=updating))
        if now >= self.next_heartbeat:
            self.publish(now, detected, updating)

    def poll_sensor(self, now):
        if self.sensor is not None or now < self.next_sensor_retry:
            return
        try:
            self.sensor = self.sensor_factory(self.config.motion.gpio)
            warming = now - self.gate.boot < self.gate.warmup
            update_status("aquecendo_sensor" if warming else "aguardando_movimento")
        except Exception as exc:
            log.error("GPIO indisponível type=%s; confira BCM%s com noha doctor", type(exc).__name__, self.config.motion.gpio)
            self.next_sensor_retry = now + 30
            update_status("gpio_indisponivel", f"Confira a ligação do sensor no GPIO {self.config.motion.gpio}")

    def reap(self):
        if self.active and self.active.done():
            try:
                self.active.result()
            except Exception as exc:
                log.error("Visita encerrada type=%s", type(exc).__name__)
            self.active = None

    def poll_clips(self, now):
        if not self.prepared and self.preparing is None and now >= self.next_clips_try and (key := self.key()):
            texts = [*(self.config.greeting.texts if self.config.greeting.local else []), self.setup]
            self.preparing = asyncio.create_task(self.synthesize(self.config, key, texts))
        if self.preparing and self.preparing.done():
            try:
                *self.clips, (_, self.setup_clip) = self.preparing.result()
                self.prepared = True
                self.offline.clear()  # Noha's own voice replaces espeak-ng
                log.info("Saudações locais prontas count=%d", len(self.clips))
            except Exception as exc:
                log.warning("Saudações locais indisponíveis type=%s code=%s; o modelo cumprimenta até nova tentativa",
                            type(exc).__name__, safe_code(str(exc)))
            self.preparing = None
            self.next_clips_try = now + 600
            self.next_heartbeat = 0

    def poll_presence(self, now):
        if now < self.next_presence_check:
            return
        self.enabled = presence_enabled()
        self.next_presence_check = now + 1
        current = _status.get("state")
        if not self.enabled and self.active is None and current == "aguardando_movimento":
            update_status("sensor_desligado", "Saudações por movimento desligadas no painel")
            self.next_heartbeat = 0
        elif self.enabled and current == "sensor_desligado":
            update_status("aguardando_movimento")
            self.next_heartbeat = 0

    def start_visit(self, now, manual):
        if manual:
            self.gate.armed = False
            self.gate.last_greeting = now
        if over_budget(self.config.voice):
            log.warning("Visita não iniciada: limite diário de custo atingido")
            update_status("orcamento_esgotado", f"Limite diário de US$ {self.config.voice.daily_budget_usd:.2f} atingido; "
                          "Noha volta à meia-noite ou aumente o limite nos ajustes")
            return
        heartbeat(busy=True)
        self.active = asyncio.create_task(self.visit())
        self.next_heartbeat = 0

    async def check_audio(self, now):
        from .audio import devices
        try:
            names = {d["name"] for d in await asyncio.to_thread(devices)}
            self.audio_missing = missing(self.config.audio, names)
            self.audio_ready = not self.audio_missing
            if self.audio_ready:
                self.aec_backoff = 120
                if _status.get("state") == "audio_indisponivel":
                    update_status("aguardando_movimento")
            elif self.active is None and now >= self.next_aec_restart:
                reason = ("AEC PipeWire ausente com microfone e alto-falante presentes"
                          if echo_cancel_dropped(self.config.audio, names) else
                          "placa de áudio sem perfil (tela HDMI desligada no boot?)"
                          if profile_missing(self.config.audio, names) else None)
                if reason:
                    log.warning("%s; reiniciando PipeWire", reason)
                    # A display may stay off for hours: back off instead of restarting every 2 min.
                    self.next_aec_restart, self.aec_backoff = now + self.aec_backoff, min(self.aec_backoff * 2, 1800)
                    await restart_audio_stack()
        except Exception:
            self.audio_ready, self.audio_missing = False, []
        heartbeat(spent_today=round(await asyncio.to_thread(spent_today), 4),
                  daily_budget=self.config.voice.daily_budget_usd)
        self.next_audio_check = now + 10

    def publish(self, now, detected, updating):
        ready = bool(self.sensor and self.audio_ready and self.key() and Path(self.config.voice.instructions).is_file())
        gate, seconds = self.gate.readiness(now, self.active is not None)
        if not self.enabled and self.active is None:
            gate, seconds = "desligado", 0
        heartbeat(busy=self.active is not None, speaking=bool(self.audio and self.audio.busy()), ready=ready,
                  updating=updating, audio_ready=self.audio_ready,
                  audio_missing=self.audio_missing, gpio_ready=self.sensor is not None, motion=detected,
                  last_motion=self.motion.last, motion_history=self.motion.snapshot(), gate=gate,
                  gate_seconds=round(seconds), sensor_enabled=self.enabled, greeting_local=self.config.greeting.local,
                  local_greetings=len(self.clips), setup_ready=self.setup_clip is not None,
                  offline_voice=self.offline_voice, key_configured=bool(self.key()), config_digest=self.config.digest)
        self.next_heartbeat = now + 2

    async def stop(self):
        for task in (self.active, self.preparing):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.sensor:
            self.sensor.close()
        self.ring.close()
        update_status("parado")

    # One visit ---------------------------------------------------------------------------------

    async def visit(self):
        """Greet, listen, then converse, explain the Wi-Fi setup, or give up. Records how it ended."""
        started, spent = time.time(), spent_today()
        stats, outcome = {"replies": 0}, None
        # Checked while Noha greets: offline, a visitor who talks hears how to set up the Wi-Fi.
        probe = asyncio.create_task(self.online())
        try:
            outcome = await self.greet(probe, stats)
        finally:
            probe.cancel()
            outcome = outcome or ("conversou" if stats["replies"] else "falha")
            record_visit(started, time.time(), outcome, self.config.voice.model, spent_today() - spent, stats["replies"])

    async def greet(self, probe, stats):
        key = self.key()
        said = pcm = None
        if self.clips:
            said, pcm = random.choice(self.clips)
        elif await probe:
            if not key:
                update_status("chave_ausente", "Informe a chave OpenAI nos ajustes")
                return "sem_chave"
            return await self.session(key, stats=stats)  # nothing recorded yet: the voice model greets
        elif pcm := await self.offline_speech("greeting"):
            said = OFFLINE_GREETING  # no internet and no recording yet: tell visitors to talk anyway
        audio = self.audio = self.audio_factory(self.config.audio)
        detector = SpeechDetector(self.config.greeting.speech_margin_db)
        heard = None
        try:
            audio.start()
            if pcm:
                update_status("cumprimentando")
                record(self.config.voice.model, greetings=1)
            heard = await greet_and_listen(audio, pcm, self.config.voice.unanswered_seconds, detector)
        except AudioUnavailable as exc:
            self.audio_unavailable(exc)
            return "falha"
        except Exception as exc:
            log.error("Saudação local interrompida type=%s", type(exc).__name__)
            update_status("aguardando_movimento")
            return "falha"
        finally:
            if heard is None:
                self.close(audio)
        # Levels only: tune the voice sensitivity from these if speech is missed or noise connects.
        levels = f"piso_dbfs={detector.floor or -100:.0f} pico_dbfs={detector.peak:.0f}"
        if heard is None:
            log.info("Saudação sem resposta %s; nenhuma chamada à API", levels)
            update_status("aguardando_movimento")
            return "sem_resposta"
        if not await probe:
            await self.explain_wifi(audio, levels)
            return "wifi"
        if not key:
            self.close(audio)
            update_status("chave_ausente", "Informe a chave OpenAI nos ajustes")
            return "sem_chave"
        log.info("Fala após a saudação %s; conectando", levels)
        return await self.session(key, greeting=said, audio=audio, preroll=Preroll(audio, heard), stats=stats)

    async def offline_speech(self, which):
        """espeak-ng rendering of the offline greeting or the Wi-Fi steps; None if unavailable."""
        if which == "setup" and self.setup_clip:
            return self.setup_clip
        if which not in self.offline and self.offline_voice:
            try:
                self.offline[which] = await asyncio.to_thread(
                    self.speak_offline, OFFLINE_GREETING if which == "greeting" else self.setup)
            except FileNotFoundError:
                self.offline_voice = False
                log.warning("espeak-ng ausente: sem fala offline até a voz da Noha ser sintetizada")
            except Exception as exc:
                log.error("Fala offline falhou type=%s", type(exc).__name__)
        return self.offline.get(which)

    async def explain_wifi(self, audio, levels):
        log.info("Fala sem internet %s; explicando a configuração do Wi-Fi", levels)
        update_status("orientando_wifi", "Sem internet: Noha explica como configurar o Wi-Fi pelo celular")
        try:
            if clip := await self.offline_speech("setup"):
                await play(audio, clip)
        except Exception as exc:
            log.error("Instruções de Wi-Fi interrompidas type=%s", type(exc).__name__)
        finally:
            self.close(audio)
        update_status("aguardando_movimento")

    def close(self, audio):
        audio.close()
        if self.audio is audio:
            self.audio = None

    def audio_unavailable(self, exc):
        log.error("Áudio ausente: %s", exc)
        update_status("audio_indisponivel", f"Não encontrado: {exc}. Confira cabos, tela e a seleção nos ajustes")

    async def session(self, key, greeting=None, audio=None, preroll=None, stats=None):
        """Realtime conversation. greeting: text already played locally, with the visitor's answer in preroll."""
        stats = stats if stats is not None else {"replies": 0}
        started = time.monotonic()
        try:
            completed = await self.attempts(key, greeting, audio, preroll, stats)
        finally:
            record(self.config.voice.model, duration_seconds=time.monotonic() - started)
        return "conversou" if stats["replies"] else "sem_resposta" if completed else "falha"

    async def attempts(self, key, greeting, audio, preroll, stats):
        """True when the conversation ended normally, False when it could not happen."""
        greet = greeting is None
        counted = False
        failure = None
        for attempt, delay in enumerate((1, 2, 4, 8, 16)):
            audio = self.audio = audio or self.audio_factory(self.config.audio)
            try:
                instructions = Path(self.config.voice.instructions).read_text(encoding="utf-8")
                if audio.playback is None:
                    audio.start()
                update_status("conectando")

                def connected(state):
                    nonlocal greet, counted
                    if not counted:
                        record(self.config.voice.model, conversations=1)
                        counted = True
                    update_status(state)
                    greet = False
                await self.adapter(self.config.voice, key, instructions, audio, greet=greet, state_callback=connected,
                                   greeting=greeting, preroll=preroll, stats=stats)
                update_status("aguardando_movimento")
                return True
            except AudioUnavailable as exc:
                self.audio_unavailable(exc)
                return False
            except Exception as exc:
                record(self.config.voice.model, errors=1)
                # HTTP 401/403/429 from the WebSocket handshake and error close frames are not network loss.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                problem = exc if isinstance(exc, APIProblem) else close_problem(exc)
                if problem or status in (400, 401, 403, 404, 429):
                    code = str(problem) if problem else f"http_{status}"
                    # APIProblem contains only a sanitized code, never a raw server message.
                    log.error("OpenAI code=%s", code)
                    update_status("erro_api", api_message(code))
                    return False
                failure = type(exc).__name__
                log.warning("Sessão interrompida type=%s tentativa=%s", failure, attempt + 1)
                update_status("recuperando_audio_ou_conexao")
            finally:
                if preroll:
                    preroll.close()
                    preroll = None
                self.close(audio)
                audio = None
            if attempt < 4:
                record(self.config.voice.model, reconnects=1)
                await asyncio.sleep(delay)
        update_status("sem_conexao", f"A conversa caiu 5 vezes seguidas ({failure}); confira a internet do robô")
        return False


async def run(config, simulate=False):
    await Robot(config, simulate).run()
