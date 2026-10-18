import asyncio
import base64
from collections import deque
import json
import logging
import re
import time
from urllib.parse import urlencode

from .state import ConversationState

log = logging.getLogger(__name__)


class APIProblem(RuntimeError):
    pass


def safe_code(value):
    value = str(value or "unknown")
    return value if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value) else "unknown"


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
    async with connect(url, additional_headers={"Authorization": f"Bearer {key}"},
                       open_timeout=20, close_timeout=3, ping_interval=20, ping_timeout=20,
                       max_size=4 * 1024 * 1024, max_queue=32) as ws:
        async def send(event):
            await ws.send(json.dumps(event))

        await send({"type": "session.update", "session": {
            "type": "realtime", "instructions": instructions, "output_modalities": ["audio"],
            "tools": [], "max_output_tokens": config.max_output_tokens,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "turn_detection": {"type": "semantic_vad", "eagerness": config.eagerness,
                                             "create_response": True, "interrupt_response": True}},
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
                        await send({"type": "response.create", "response": {"instructions":
                            "Cumprimente brevemente em português brasileiro quem acabou de chegar. "
                            "Apresente-se como Noha, anfitrião robótico com voz gerada por IA. "
                            "Uma ou duas frases; não enumere ambientes ou parceiros."}})
                elif kind == "input_audio_buffer.speech_started":
                    state.speaking = True
                    state.last_user = time.monotonic()
                    truncate = playback.interrupt(generating_id)
                    if truncate:
                        await send(truncate)
                elif kind == "input_audio_buffer.speech_stopped":
                    state.speaking = False
                    state.last_user = time.monotonic()
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
                    # Never log server messages or transcripts: only numeric usage counters.
                    log.info("response status=%s input_tokens=%s output_tokens=%s cached_tokens=%s",
                             safe_code(response.get("status")), int(usage.get("input_tokens", 0)),
                             int(usage.get("output_tokens", 0)),
                             int((usage.get("input_token_details") or {}).get("cached_tokens", 0)))
                    if response.get("status") == "failed":
                        details = response.get("status_details") or {}
                        raise APIProblem(safe_code((details.get("error") or {}).get("code")))
                elif kind == "error":
                    raise APIProblem(safe_code(event.get("error", {}).get("code")))
            raise ConnectionError("Realtime desconectado")

        async def capture():
            await asyncio.wait_for(ready.wait(), 30)
            # Do not fill the mic queue while DNS/TLS/session negotiation is pending.
            audio.start_capture()
            while True:
                data = await audio.input.get()
                await send({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode("ascii")})

        async def watchdog():
            await asyncio.wait_for(ready.wait(), 30)
            last_device_check = 0
            while True:
                audio.check()
                now = time.monotonic()
                if now - last_device_check >= 5:
                    from .audio import devices
                    names = {d["name"] for d in await asyncio.to_thread(devices)}
                    if any(n not in names for n in (audio.config.microphone, audio.config.speaker,
                                                    audio.config.clean_source, audio.config.reference_sink)):
                        raise RuntimeError("USB ou AEC desconectado")
                    last_device_check = now
                if state.idle(now, config.idle_seconds, audio.busy()):
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
            log.info("session duration_seconds=%.1f", time.monotonic() - started)

