"""Explicit voice-model capabilities; never silently substitute a model."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    transport: str = "realtime"
    voices: tuple[str, ...] = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
    deprecated: bool = False
    retired: bool = False


MODELS = {m.id: m for m in (
    Model("gpt-realtime-1.5", "GPT Realtime 1.5"),
    Model("gpt-live-1", "GPT Live 1", "live", ("marin", "cedar")),
    Model("gpt-realtime-2.1-mini", "GPT Realtime 2.1 Mini"),
    Model("gpt-realtime-2.1", "GPT Realtime 2.1"),
    Model("gpt-realtime-2", "GPT Realtime 2"),
    Model("gpt-4o-realtime", "GPT-4o Realtime", deprecated=True),
    Model("gpt-4o-mini-realtime", "GPT-4o Mini Realtime", deprecated=True),
    Model("gpt-4o-realtime-preview", "GPT-4o Realtime Preview (retirado)", retired=True),
    Model("gpt-4o-mini-realtime-preview", "GPT-4o Mini Realtime Preview (retirado)", retired=True),
)}


def model_info(name):
    model = MODELS.get(name)
    if not model or model.retired:
        raise ValueError("Modelo desconhecido ou retirado")
    return model


def catalog():
    return [asdict(m) for m in MODELS.values()]


GPIO_PINS = dict(zip(range(28), (27, 28, 3, 5, 7, 29, 31, 26, 24, 21, 19, 23, 32, 33,
                                8, 10, 36, 11, 12, 35, 38, 40, 15, 16, 18, 22, 37, 13)))
