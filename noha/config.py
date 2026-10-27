from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import tomllib

PRESENCE = Path("/etc/noha/presence.json")  # dashboard on/off switch; written by the admin helper


def presence_enabled(path=PRESENCE):
    """Whether motion may greet visitors. A missing or unreadable switch means on."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("enabled") is not False
    except (OSError, ValueError, AttributeError):
        return True


@dataclass
class Voice:
    model: str = "gpt-realtime-2.1-mini"
    voice: str = "alloy"
    instructions: str = "/etc/noha/personality.txt"
    idle_seconds: float = 30  # since the last accepted visitor turn or Noha's last word
    unanswered_seconds: float = 10  # after the greeting, before any visitor speech: a passer-by
    max_session_seconds: float = 180  # hard limit per visit, whatever the microphone hears
    min_speech_ms: int = 700  # shorter detections are noise: no reply, and removed from the context
    context_tokens: int = 4000  # conversation kept after the instructions; each reply re-bills it
    daily_budget_usd: float = 2.0  # estimated; 0 disables the limit
    noise_reduction: str = "far_field"  # far_field | near_field | off
    eagerness: str = "medium"
    max_output_tokens: int = 300


@dataclass
class Motion:
    gpio: int = 17  # physical pin 11: no board pull-up and no default alternate function
    warmup_seconds: float = 60
    rearm_seconds: float = 30
    greeting_cooldown_seconds: float = 120
    # A passer-by makes one short detection. Greet after this much continuous motion
    # or after confirm_pulses separate detections within confirm_window_seconds; 1 greets at once.
    confirm_seconds: float = 6
    confirm_pulses: int = 2
    confirm_window_seconds: float = 20


GREETINGS = [
    "Olá, boas-vindas à casa modelo da Noha Concept! Eu sou Noha, o anfitrião robótico daqui, "
    "com voz gerada por inteligência artificial. Se quiser saber algo sobre a casa, é só perguntar.",
    "Oi, que bom ter você por aqui! Eu sou Noha, um robô anfitrião com voz de inteligência artificial. "
    "Posso ajudar com alguma dúvida sobre a casa?",
    "Olá! Sou Noha, o anfitrião robótico da Noha Concept, e minha voz é gerada por inteligência artificial. "
    "Meus circuitos estão prontos para as suas perguntas.",
    "Oi! Eu sou Noha, o robô anfitrião desta casa modelo, com voz criada por inteligência artificial. "
    "Fique à vontade e me pergunte o que quiser.",
]


@dataclass
class Greeting:
    """Greetings synthesized once and played locally: motion alone never costs a Realtime reply."""
    local: bool = True  # false: the voice model generates every greeting (each one billed)
    texts: list[str] = field(default_factory=lambda: list(GREETINGS))
    tts_model: str = "gpt-4o-mini-tts"
    style: str = ("Anfitrião simpático, acolhedor e levemente brincalhão, em português brasileiro natural, "
                  "ritmo calmo e claro.")
    speech_margin_db: float = 12  # local speech detection: level above the room's noise floor


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

    def barge_in(self):
        # Only the XVF3800 cancels echo well enough to listen while speaking; with a software AEC
        # (HDMI latency) Noha hears itself and answers its own voice, so the mic waits for it to finish.
        return self.hardware_aec()


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
    greeting: Greeting = field(default_factory=Greeting)
    digest: str = field(default="", compare=False)  # of the loaded file: the dashboard confirms what is running

    @classmethod
    def load(cls, path="/etc/noha/noha.toml"):
        data = Path(path).read_bytes()
        raw = tomllib.loads(data.decode("utf-8"))
        sections = {"voice": Voice, "motion": Motion, "audio": Audio, "network": Network, "leds": Leds,
                    "greeting": Greeting}
        if set(raw) - set(sections):
            raise ValueError("Seção desconhecida na configuração")
        parts = {}
        for name, kind in sections.items():
            try:
                parts[name] = kind(**raw.get(name, {}))
            except TypeError:
                raise ValueError(f"Opção desconhecida em [{name}]") from None
        config = cls(**parts, digest=hashlib.sha256(data).hexdigest()[:16])
        config.validate()
        return config

    def validate(self):
        from .settings import SETTINGS, check
        # Dashboard-editable values share the dashboard's ranges; texts and devices may be empty here.
        for setting in SETTINGS:
            if setting.section and setting.kind in {"bool", "int", "float", "choice", "gpio", "model"}:
                check(setting, getattr(getattr(self, setting.section), setting.name))
        from .models import model_info
        if self.voice.voice not in model_info(self.voice.model).voices:
            raise ValueError("Voz incompatível com o modelo")
        for section in (self.voice, self.motion, self.network):
            for key, value in vars(section).items():
                if key.endswith("_seconds") and (type(value) not in (int, float) or value <= 0):
                    raise ValueError(f"{key} precisa ser positivo")
        greeting = self.greeting
        if (not isinstance(greeting.texts, list) or len(greeting.texts) > 10
                or not all(isinstance(t, str) and 0 < len(t) <= 1000 for t in greeting.texts)
                or not isinstance(greeting.style, str) or not isinstance(greeting.tts_model, str)):
            raise ValueError("Saudação: até 10 frases de 1 a 1000 caracteres")
        if not all(isinstance(getattr(self.audio, key), str) for key in ("microphone", "speaker")):
            raise ValueError("Dispositivos de áudio inválidos")
        if self.network.gateway != "192.168.50.1":
            raise ValueError("Gateway suportado: 192.168.50.1")
        if not self.network.interface.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Interface inválida")
        if not self.network.connectivity_urls or any(not u.startswith("https://") for u in self.network.connectivity_urls):
            raise ValueError("Use verificações HTTPS de conectividade")
