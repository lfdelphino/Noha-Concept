from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass
class Voice:
    model: str = "gpt-realtime-1.5"
    voice: str = "marin"
    instructions: str = "/etc/noha/personality.txt"
    idle_seconds: float = 60
    eagerness: str = "medium"
    max_output_tokens: int = 512


@dataclass
class Motion:
    gpio: int = 3
    warmup_seconds: float = 60
    rearm_seconds: float = 30
    greeting_cooldown_seconds: float = 120


@dataclass
class Audio:
    microphone: str = ""
    speaker: str = ""
    clean_source: str = "noha_echo_source"
    reference_sink: str = "noha_echo_sink"
    playback_latency_ms: int = 80


@dataclass
class Network:
    interface: str = "wlan0"
    hotspot_ssid: str = ""
    gateway: str = "192.168.50.1"
    offline_seconds: float = 90
    check_seconds: float = 15
    connect_seconds: float = 45
    retry_seconds: float = 300
    portal_quiet_seconds: float = 120
    connectivity_urls: list[str] = field(default_factory=lambda: [
        "https://www.gstatic.com/generate_204",
        "https://connectivitycheck.gstatic.com/generate_204",
    ])
    socket: str = "/run/noha-network/control.sock"


@dataclass
class Config:
    voice: Voice = field(default_factory=Voice)
    motion: Motion = field(default_factory=Motion)
    audio: Audio = field(default_factory=Audio)
    network: Network = field(default_factory=Network)

    @classmethod
    def load(cls, path="/etc/noha/noha.toml"):
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(raw) - {"voice", "motion", "audio", "network"}
        if unknown:
            raise ValueError("Seção desconhecida na configuração")
        config = cls(**{name: kind(**raw.get(name, {})) for name, kind in
                       (("voice", Voice), ("motion", Motion), ("audio", Audio), ("network", Network))})
        if config.voice.model != "gpt-realtime-1.5":
            raise ValueError("Este projeto usa gpt-realtime-1.5")
        if config.voice.eagerness not in {"low", "medium", "high", "auto"}:
            raise ValueError("Eagerness inválida")
        for section in (config.voice, config.motion, config.network):
            for key, value in vars(section).items():
                if key.endswith("_seconds") and (not isinstance(value, (float, int)) or value <= 0):
                    raise ValueError(f"{key} precisa ser positivo")
        if not 0 <= config.motion.gpio <= 27:
            raise ValueError("GPIO BCM inválido")
        if config.network.gateway != "192.168.50.1":
            raise ValueError("Gateway suportado: 192.168.50.1")
        if not config.network.interface.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Interface inválida")
        if not config.network.connectivity_urls or any(not u.startswith("https://") for u in config.network.connectivity_urls):
            raise ValueError("Use verificações HTTPS de conectividade")
        return config

