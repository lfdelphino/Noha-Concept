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
    echo_cancel: str = "auto"  # auto | pipewire | xvf3800

    def hardware_aec(self):
        # The XVF3800 cancels only what it plays itself: its own jack/JST output is the reference.
        if self.echo_cancel == "auto":
            return same_xvf3800(self.microphone, self.speaker)
        return self.echo_cancel == "xvf3800"

    def endpoints(self):
        """(capture, playback) nodes opened by the voice pipeline."""
        if self.hardware_aec():
            return self.microphone, self.speaker
        return self.clean_source, self.reference_sink

    def required(self):
        return (self.microphone, self.speaker, *self.endpoints())


def same_xvf3800(microphone, speaker):
    # alsa_input.usb-Seeed_..._XVF3800_..._<serial>-00.analog-stereo vs. the matching alsa_output.
    def card(name, prefix):
        return name.removeprefix(prefix).rpartition(".")[0] if name.startswith(prefix) else None
    mic = card(microphone, "alsa_input.")
    return bool(mic) and "XVF3800" in mic and mic == card(speaker, "alsa_output.")


@dataclass
class Leds:
    enabled: bool = True
    brightness: int = 96  # 0-255


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
    leds: Leds = field(default_factory=Leds)

    @classmethod
    def load(cls, path="/etc/noha/noha.toml"):
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(raw) - {"voice", "motion", "audio", "network", "leds"}
        if unknown:
            raise ValueError("Seção desconhecida na configuração")
        config = cls(**{name: kind(**raw.get(name, {})) for name, kind in
                       (("voice", Voice), ("motion", Motion), ("audio", Audio), ("network", Network), ("leds", Leds))})
        from .models import model_info
        if config.voice.voice not in model_info(config.voice.model).voices:
            raise ValueError("Voz incompatível com o modelo")
        if config.voice.eagerness not in {"low", "medium", "high", "auto"}:
            raise ValueError("Eagerness inválida")
        for section in (config.voice, config.motion, config.network):
            for key, value in vars(section).items():
                if key.endswith("_seconds") and (not isinstance(value, (float, int)) or value <= 0):
                    raise ValueError(f"{key} precisa ser positivo")
        if config.audio.echo_cancel not in {"auto", "pipewire", "xvf3800"}:
            raise ValueError("echo_cancel: auto, pipewire ou xvf3800")
        if type(config.leds.enabled) is not bool or type(config.leds.brightness) is not int                 or not 0 <= config.leds.brightness <= 255:
            raise ValueError("LEDs: enabled booleano e brightness 0-255")
        if not 0 <= config.motion.gpio <= 27:
            raise ValueError("GPIO BCM inválido")
        if config.network.gateway != "192.168.50.1":
            raise ValueError("Gateway suportado: 192.168.50.1")
        if not config.network.interface.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Interface inválida")
        if not config.network.connectivity_urls or any(not u.startswith("https://") for u in config.network.connectivity_urls):
            raise ValueError("Use verificações HTTPS de conectividade")
        return config

