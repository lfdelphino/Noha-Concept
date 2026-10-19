import asyncio
import base64
from collections import deque
import json
import logging
import re
import time
from urllib.parse import urlencode

from .state import ConversationState
from .metrics import record, spent_today
from .models import response_cost

log = logging.getLogger(__name__)


class APIProblem(RuntimeError):
    pass


def safe_code(value):
    value = str(value or "unknown")
    return value if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value) else "unknown"


def close_problem(exc):
    """The server may refuse a session by closing with an error code as the reason,
    e.g. 1013 insufficient_quota.credit_balance_exhausted. Retrying cannot fix that."""
    frame = getattr(exc, "rcvd", None)
    code = safe_code(getattr(frame, "reason", None) or None)
    return None if code == "unknown" else APIProblem(code)


API_MESSAGES = {
    "insufficient_quota": "Conta OpenAI sem créditos; adicione saldo em platform.openai.com",
    "credit_balance_exhausted": "Conta OpenAI sem créditos; adicione saldo em platform.openai.com",
    "invalid_api_key": "Chave OpenAI inválida; atualize-a em Configurações",
    "rate_limit_exceeded": "Limite de uso da OpenAI atingido; tente de novo em alguns minutos",
    "model_not_found": "Modelo indisponível para esta conta; escolha outro em Configurações",
    "http_401": "Chave OpenAI inválida; atualize-a em Configurações",
    "http_403": "Esta chave não tem acesso ao modelo; confira o projeto em platform.openai.com",
    "http_404": "Modelo indisponível para esta conta; escolha outro em Configurações",
    "http_429": "Conta OpenAI sem créditos ou limite atingido; confira platform.openai.com",
}


def api_message(code):
    return API_MESSAGES.get(code.partition(".")[0], f"OpenAI recusou a sessão (código {code})")


def record_usage(model, usage):
    values = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            values[key] = value
    cached = (usage.get("input_token_details") or {}).get("cached_tokens")
    if type(cached) is int and cached >= 0:
        values["cached_tokens"] = cached
    if values:
        record(model, **values, cost_usd=response_cost(model, usage))


GREETING = "Um visitante acabou de ser detectado pelo sensor de movimento. Siga agora a seção INÍCIO DA VISITA."
ECHO_TAIL_MS = 400  # room reverb after Noha's last sample; still Noha, not the visitor


def over_budget(config):
    return config.daily_budget_usd > 0 and spent_today() >= config.daily_budget_usd


def session_limit(config, started, now):
    """None while the visit may go on; otherwise why it must end (logged, never spoken)."""
    if now - started >= config.max_session_seconds:
        return "max_session_seconds"
    if over_budget(config):
        return "daily_budget_usd"
    return None


class Playback:
    """Reject late deltas from interrupted responses, even after generation finished."""
    def __init__(self, audio):
        self.audio = audio
        self.response = self.item = None
        self.content_index = 0
        self.blocked = deque(maxlen=64)

    def delta(self, event):
        response = event.get("response_id")
        if response in self.blocked:
            return
        item = event["item_id"]
        if item != self.item:
            self.audio.begin_item()
        self.item, self.response = item, response
        self.content_index = event.get("content_index", 0)
        self.audio.push(base64.b64decode(event["delta"]))

    def interrupt(self, generating_id=None):
        for rid in (self.response, generating_id):
            if rid and rid not in self.blocked:
                self.blocked.append(rid)
        heard = self.audio.played_ms()
        should_truncate = self.item is not None and self.audio.busy()
        self.audio.flush()
        event = None
        if should_truncate:
            event = {"type": "conversation.item.truncate", "item_id": self.item,
                     "content_index": self.content_index, "audio_end_ms": int(heard)}
        self.item = self.response = None
        return event


async def conversation(config, key, instructions, audio, greet=True, state_callback=lambda _: None):
    from websockets.asyncio.client import connect
    url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": config.model})
    started = time.monotonic()
    state = ConversationState(started)
    playback = Playback(audio)
    generating_id = None
    ready = asyncio.Event()
    greeting_sent = False
    barge_in = audio.config.barge_in()
    speech = {}  # item_id -> [audio_start_ms, audio_end_ms]
    counts = {"replies": 0, "ignored": 0}
    ending = None
    async with connect(url, additional_headers={"Authorization": f"Bearer {key}"},
                       open_timeout=20, close_timeout=3, ping_interval=20, ping_timeout=20,
                       max_size=4 * 1024 * 1024, max_queue=32) as ws:
        async def send(event):
            await ws.send(json.dumps(event))

        # Every reply re-bills the whole kept conversation as input; cap it after the instructions.
        await send({"type": "session.update", "session": {
            "type": "realtime", "instructions": instructions, "output_modalities": ["audio"],
            "tools": [], "max_output_tokens": config.max_output_tokens,
            "truncation": {"type": "retention_ratio", "retention_ratio": 0.7,
                           "token_limits": {"post_instructions": config.context_tokens}},
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "noise_reduction": None if config.noise_reduction == "off" else {"type": config.noise_reduction},
                          # Replies are created here, after the turn proves to be speech, not noise.
                          "turn_detection": {"type": "semantic_vad", "eagerness": config.eagerness,
                                             "create_response": False, "interrupt_response": barge_in}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": config.voice}}}})

        async def receive():
            nonlocal generating_id, greeting_sent
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.updated":
                    ready.set()
                    state_callback("conversando")
                    if greet and not greeting_sent:
                        greeting_sent = True
                        state.generating = True
                        # Response instructions replace the session's: keep the personality and facts.
                        await send({"type": "response.create", "response": {"instructions": f"{instructions}\n\n{GREETING}"}})
                elif kind == "input_audio_buffer.speech_started":
                    state.speaking = True
                    speech[event.get("item_id")] = [event.get("audio_start_ms", 0), None]
                    if barge_in:
                        truncate = playback.interrupt(generating_id)
                        if truncate:
                            await send(truncate)
                elif kind == "input_audio_buffer.speech_stopped":
                    state.speaking = False
                    speech.setdefault(event.get("item_id"), [0, None])[1] = event.get("audio_end_ms")
                elif kind == "input_audio_buffer.committed":
                    start, end = speech.pop(event.get("item_id"), (0, None))
                    if ending or (end is not None and end - start < config.min_speech_ms):
                        # A cough, a door, a TV blip: answering it costs the whole context again.
                        counts["ignored"] += 1
                        await send({"type": "conversation.item.delete", "item_id": event["item_id"]})
                    else:
                        counts["replies"] += 1
                        state.last_user = time.monotonic()
                        state.generating = True
                        await send({"type": "response.create"})
                elif kind == "response.created":
                    generating_id = event["response"]["id"]
                    state.generating = True
                elif kind == "response.output_audio.delta":
                    playback.delta(event)
                elif kind == "response.done":
                    response = event.get("response", {})
                    if response.get("id") == generating_id:
                        state.generating = False
                        generating_id = None
                    usage = response.get("usage") or {}
                    record_usage(config.model, usage)
                    # Never log server messages or transcripts: only numeric usage counters.
                    log.info("response status=%s input_tokens=%s output_tokens=%s cached_tokens=%s",
                             safe_code(response.get("status")), int(usage.get("input_tokens", 0)),
                             int(usage.get("output_tokens", 0)),
                             int((usage.get("input_token_details") or {}).get("cached_tokens", 0)))
                    if response.get("status") == "failed":
                        details = response.get("status_details") or {}
                        raise APIProblem(safe_code((details.get("error") or {}).get("code")))
                elif kind == "error":
                    code = safe_code(event.get("error", {}).get("code"))
                    if code in ("conversation_already_has_active_response", "item_delete_invalid_item_id"):
                        log.info("OpenAI code=%s ignorado", code)  # turn raced a reply; harmless
                        continue
                    raise APIProblem(code)
            raise ConnectionError("Realtime desconectado")

        async def capture():
            await asyncio.wait_for(ready.wait(), 30)
            # Do not fill the mic queue while DNS/TLS/session negotiation is pending.
            audio.start_capture()
            while True:
                data = await audio.input.get()
                if ending or (not barge_in and (state.generating or audio.busy(ECHO_TAIL_MS))):
                    continue  # Noha's own voice must never become a visitor turn
                await send({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode("ascii")})

        async def watchdog():
            nonlocal ending
            await asyncio.wait_for(ready.wait(), 30)
            last_check = ending_at = 0
            while True:
                audio.check()
                now = time.monotonic()
                if now - last_check >= 5:
                    from .audio import devices
                    names = {d["name"] for d in await asyncio.to_thread(devices)}
                    if any(n not in names for n in audio.config.required()):
                        raise RuntimeError("USB ou AEC desconectado")
                    if not ending:
                        ending = session_limit(config, started, now)
                        ending_at = now
                    last_check = now
                talking = state.generating or audio.busy()
                if state.idle(now, config.idle_seconds, talking):
                    return
                # A limit lets Noha finish its sentence, but never waits on a stuck reply.
                if ending and (not talking or now - ending_at > 20):
                    log.warning("Conversa encerrada pelo limite %s", ending)
                    return
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(f()) for f in (receive, capture, watchdog)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            audio.flush()
            log.info("session duration_seconds=%.1f replies=%d ignored=%d", time.monotonic() - started,
                     counts["replies"], counts["ignored"])

