"""GPT-Live primary WebSocket adapter, independent of Realtime turn events."""
import asyncio
import base64
import json
import logging
import time
from uuid import uuid4

from .metrics import record
from .models import model_info
from .realtime import ECHO_TAIL_MS, GREETING, APIProblem, greeted, idle_limit, safe_code, session_limit

log = logging.getLogger(__name__)


def start_event(config, instructions, greet, greeting=None):
    context = GREETING if greet else greeted(greeting) if greeting else None
    return {"type": "session.start", "session": {
        "model": config.model, "instructions": instructions + "\nNão há ferramentas ou serviços de consulta disponíveis. "
        "Responda com os fatos fornecidos e reconheça quando não souber.",
        "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": config.voice}},
        "store": False, "delegation": {"type": "client"},
        "input": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": context}]}] if context else []}}


class LivePlayback:
    def __init__(self, audio):
        self.audio = audio
        self.last_user = time.monotonic()
        self.heard = False  # any visitor speech since the session started

    def event(self, event):
        kind = event.get("type")
        if kind == "session.output_audio.delta":
            self.audio.push(base64.b64decode(event["delta"]))
        elif kind == "session.input_transcript.delta":
            # Use text only as a speech signal; never retain or log its content.
            if event.get("delta"):
                self.heard = True
                new_speech = time.monotonic() - self.last_user > 1
                self.last_user = time.monotonic()
                if new_speech:
                    self.audio.flush()


async def conversation(config, key, instructions, audio, greet=True, state_callback=lambda _: None,
                       greeting=None, preroll=None, stats=None):
    stats = stats if stats is not None else {}
    stats.setdefault("replies", 0)
    from websockets.asyncio.client import connect
    async with connect("wss://api.openai.com/v1/live/sessions", additional_headers={"Authorization": f"Bearer {key}"},
                       open_timeout=20, close_timeout=3, ping_interval=20, ping_timeout=20,
                       max_size=4 * 1024 * 1024, max_queue=32) as ws:
        ready, closed = asyncio.Event(), asyncio.Event()
        playback = LivePlayback(audio)
        closing = False
        started = time.monotonic()
        barge_in = audio.config.barge_in()

        async def send(event):
            await ws.send(json.dumps(event))

        await send(start_event(config, instructions, greet, greeting))

        async def receive():
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.started":
                    playback.last_user = time.monotonic()
                    ready.set()
                    state_callback("conversando")
                elif kind == "session.closed":
                    closed.set()
                    return
                elif kind == "session.delegation.created":
                    delegation = event.get("delegation") or {}
                    if delegation.get("target") == "client":
                        await send({"type": "session.commentary.append", "event_id": str(uuid4()),
                                    "delegation_id": delegation["id"],
                                    "content": "Não há um serviço de consulta disponível neste robô. "
                                    "Avise o visitante se a informação solicitada não consta nos fatos fornecidos."})
                elif kind == "error":
                    raise APIProblem(safe_code((event.get("error") or {}).get("code")))
                else:
                    playback.event(event)
                    if playback.heard and not stats["replies"]:
                        stats["replies"] = 1  # GPT-Live has no turn events; any visitor speech counts
            if not closed.is_set():
                raise ConnectionError("GPT-Live desconectado")

        async def capture():
            async with asyncio.timeout(30):  # not wait_for: on Python 3.11 it can swallow the cancellation
                await ready.wait()
            audio.start_capture()
            pending = preroll.take() if preroll else b""
            while True:
                data = pending + await audio.input.get()
                end = len(data) - len(data) % 2
                pending = data[end:]
                if not barge_in and audio.busy(ECHO_TAIL_MS):
                    continue  # Noha's own voice must never become a visitor turn
                if end and not closing:
                    await send({"type": "session.input_audio.append", "audio": base64.b64encode(data[:end]).decode("ascii")})

        async def watchdog():
            nonlocal closing
            async with asyncio.timeout(30):
                await ready.wait()
            last_check = 0
            limit = None
            while True:
                audio.check()
                now = time.monotonic()
                if now - last_check >= 5:
                    from .audio import devices
                    names = {d["name"] for d in await asyncio.to_thread(devices)}
                    if any(n not in names for n in audio.config.required()):
                        raise RuntimeError("USB ou AEC desconectado")
                    limit = session_limit(config, started, now)
                    last_check = now
                if audio.busy():
                    playback.last_user = now  # idle counts from Noha's last word
                if not audio.busy() and (now - playback.last_user >= idle_limit(config, playback.heard) or limit):
                    if limit:
                        log.warning("Conversa encerrada pelo limite %s", limit)
                    closing = True
                    await send({"type": "session.close"})
                    async with asyncio.timeout(15):
                        await closed.wait()
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
            # GPT-Live is billed per second of open session, not per token.
            record(config.model, cost_usd=(time.monotonic() - started) / 60 * model_info(config.model).price.per_minute)
