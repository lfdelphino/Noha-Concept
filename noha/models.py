"""Explicit voice-model capabilities; never silently substitute a model."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens (developers.openai.com/api/docs/pricing, 2026-10), or per minute of session."""
    text_in: float = 0
    text_cached: float = 0
    text_out: float = 0
    audio_in: float = 0
    audio_cached: float = 0
    audio_out: float = 0
    per_minute: float = 0


FULL = Price(4, 0.4, 16, 32, 0.4, 64)
MINI = Price(0.6, 0.06, 2.4, 10, 0.3, 20)


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    transport: str = "realtime"
    voices: tuple[str, ...] = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
    deprecated: bool = False
    retired: bool = False
    price: Price = FULL


MODELS = {m.id: m for m in (
    Model("gpt-realtime-2.1-mini", "GPT Realtime 2.1 Mini", price=MINI),
    Model("gpt-realtime-1.5", "GPT Realtime 1.5"),
    Model("gpt-live-1", "GPT Live 1", "live", ("marin", "cedar"), price=Price(per_minute=0.05)),
    Model("gpt-realtime-2.1", "GPT Realtime 2.1", price=Price(4, 0.4, 24, 32, 0.4, 64)),
    Model("gpt-realtime-2", "GPT Realtime 2", price=Price(4, 0.4, 24, 32, 0.4, 64)),
    # No longer on the pricing page: last published rates.
    Model("gpt-4o-realtime", "GPT-4o Realtime", deprecated=True, price=Price(5, 2.5, 20, 40, 2.5, 80)),
    Model("gpt-4o-mini-realtime", "GPT-4o Mini Realtime", deprecated=True, price=MINI),
    Model("gpt-4o-realtime-preview", "GPT-4o Realtime Preview (retirado)", retired=True),
    Model("gpt-4o-mini-realtime-preview", "GPT-4o Mini Realtime Preview (retirado)", retired=True),
)}


def response_cost(model, usage):
    """Estimated USD of one Realtime response.done usage. Missing details count as uncached audio."""
    price = model_info(model).price
    def count(details, key):
        value = (details or {}).get(key)
        return value if type(value) is int and value >= 0 else 0
    total_in, total_out = count(usage, "input_tokens"), count(usage, "output_tokens")
    inp, out = usage.get("input_token_details") or {}, usage.get("output_token_details") or {}
    cached = inp.get("cached_tokens_details") or {"audio_tokens": count(inp, "cached_tokens")}
    cached_text, cached_audio = count(cached, "text_tokens"), count(cached, "audio_tokens")
    text_in = max(0, count(inp, "text_tokens") - cached_text)
    audio_in = max(0, total_in - text_in - cached_text - cached_audio)
    text_out = count(out, "text_tokens")
    return (text_in * price.text_in + cached_text * price.text_cached + audio_in * price.audio_in
            + cached_audio * price.audio_cached + text_out * price.text_out
            + max(0, total_out - text_out) * price.audio_out) / 1_000_000


def model_info(name):
    model = MODELS.get(name)
    if not model or model.retired:
        raise ValueError("Modelo desconhecido ou retirado")
    return model


def catalog():
    return [asdict(m) for m in MODELS.values()]


GPIO_PINS = dict(zip(range(28), (27, 28, 3, 5, 7, 29, 31, 26, 24, 21, 19, 23, 32, 33,
                                8, 10, 36, 11, 12, 35, 38, 40, 15, 16, 18, 22, 37, 13)))
