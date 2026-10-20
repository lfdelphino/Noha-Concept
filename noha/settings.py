"""Settings the dashboard can change. One definition drives validation, the TOML writer and the forms."""
from dataclasses import dataclass
import json
import math
import re
import tomllib

from .models import GPIO_PINS, model_info

DEVICE_NAME = re.compile(r"[A-Za-z0-9_.:+-]{1,200}")
SECRET = re.compile(r"[A-Za-z0-9_-]{10,512}")


@dataclass(frozen=True)
class Setting:
    key: str  # "section.name" in noha.toml, or "personality" / "openai_key"
    group: str
    label: str
    kind: str  # bool int float choice model voice text string texts secret device gpio
    help: str = ""
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple = ()
    advanced: bool = False
    source: str = ""  # device kind: Audio/Source or Audio/Sink

    @property
    def section(self):
        return self.key.partition(".")[0] if "." in self.key else None

    @property
    def name(self):
        return self.key.partition(".")[2]

    def describe(self):
        return {"key": self.key, "group": self.group, "label": self.label, "kind": self.kind, "help": self.help,
                "unit": self.unit, "min": self.minimum, "max": self.maximum, "step": self.step,
                "choices": [list(choice) for choice in self.choices], "advanced": self.advanced, "source": self.source}


GROUPS = (
    ("personalidade", "Voz e personalidade", "Como Noha fala e o que ela sabe sobre a casa."),
    ("saudacoes", "Saudações", "O que Noha diz quando alguém chega."),
    ("conversa", "Conversa", "Quando Noha escuta, responde e encerra."),
    ("sensor", "Sensor de presença", "Quando um movimento vira uma saudação."),
    ("audio", "Áudio e LEDs", "Microfone, alto-falante e anel de luz."),
    ("custos", "Custos", "Limites de gasto com a OpenAI."),
    ("chave", "Chave OpenAI", "Credencial usada nas conversas e na síntese das saudações."),
)

SETTINGS = (
    Setting("voice.model", "personalidade", "Modelo de voz", "model",
            "O Mini é o mais barato. Modelos maiores entendem e respondem melhor."),
    Setting("voice.voice", "personalidade", "Voz", "voice",
            "Alloy é a mais neutra. Trocar a voz gera novas saudações automaticamente."),
    Setting("personality", "personalidade", "Personalidade e fatos", "text",
            "Instruções seguidas em toda conversa: tom, regras e informações sobre a casa e a Noha Concept.",
            maximum=60000),
    Setting("greeting.local", "saudacoes", "Saudação gravada no robô", "bool",
            "Toca uma saudação já sintetizada, sem custo. A conversa paga só começa quando o visitante responde."),
    Setting("greeting.texts", "saudacoes", "Frases de saudação", "texts",
            "Noha escolhe uma ao acaso. Cada frase é sintetizada uma vez, por cerca de US$ 0,002.",
            minimum=1, maximum=10),
    Setting("greeting.style", "saudacoes", "Jeito de falar", "string",
            "Tom, ritmo e emoção das saudações gravadas.", maximum=1000),
    Setting("voice.unanswered_seconds", "conversa", "Espera por resposta", "float",
            "Depois da saudação, quanto tempo Noha escuta antes de concluir que a pessoa só passou.", "s", 3, 60, 1),
    Setting("voice.idle_seconds", "conversa", "Silêncio para encerrar", "float",
            "A conversa termina depois desse tempo sem o visitante falar.", "s", 5, 300, 5),
    Setting("voice.max_session_seconds", "conversa", "Duração máxima da visita", "float",
            "Limite de cada conversa, mesmo que continue.", "s", 30, 1800, 10),
    Setting("voice.eagerness", "conversa", "Pressa para responder", "choice",
            "Baixa espera pausas mais longas antes de responder.",
            choices=(("low", "Baixa"), ("medium", "Média"), ("high", "Alta"), ("auto", "Automática")), advanced=True),
    Setting("voice.noise_reduction", "conversa", "Redução de ruído", "choice",
            "Distante: visitante a mais de um metro do microfone.",
            choices=(("far_field", "Distante"), ("near_field", "Próximo"), ("off", "Desligada")), advanced=True),
    Setting("voice.min_speech_ms", "conversa", "Fala mínima", "int",
            "Sons mais curtos são tratados como ruído e não recebem resposta.", "ms", 100, 3000, 50, advanced=True),
    Setting("greeting.speech_margin_db", "conversa", "Sensibilidade à voz", "float",
            "Quanto a voz precisa superar o ruído da sala para contar como resposta. Menor é mais sensível.",
            "dB", 3, 40, 1, advanced=True),
    Setting("motion.gpio", "sensor", "Pino do sensor", "gpio", "Numeração BCM. Confira a ligação física antes de trocar."),
    Setting("motion.confirm_seconds", "sensor", "Movimento contínuo para confirmar", "float",
            "Mantenha acima do tempo de retenção do sensor (HC-SR501: cerca de 3 s no mínimo).", "s", 1, 60, 1),
    Setting("motion.confirm_pulses", "sensor", "Detecções para confirmar", "int",
            "Detecções separadas que confirmam alguém por perto. 1 cumprimenta na primeira.", "", 1, 10, 1),
    Setting("motion.confirm_window_seconds", "sensor", "Janela das detecções", "float",
            "As detecções precisam acontecer dentro deste intervalo.", "s", 5, 120, 1),
    Setting("motion.rearm_seconds", "sensor", "Calma para rearmar", "float",
            "Tempo sem movimento antes de cumprimentar a próxima pessoa.", "s", 5, 600, 5),
    Setting("motion.greeting_cooldown_seconds", "sensor", "Intervalo entre saudações", "float",
            "Tempo mínimo entre duas saudações.", "s", 10, 3600, 10),
    Setting("motion.warmup_seconds", "sensor", "Aquecimento ao ligar", "float",
            "O sensor se estabiliza antes do primeiro disparo.", "s", 1, 600, 5, advanced=True),
    Setting("audio.microphone", "audio", "Microfone", "device", source="Audio/Source"),
    Setting("audio.speaker", "audio", "Alto-falante", "device", source="Audio/Sink"),
    Setting("audio.echo_cancel", "audio", "Cancelamento de eco", "choice",
            "O reSpeaker XVF3800 cancela o próprio eco quando o alto-falante está ligado nele.",
            choices=(("auto", "Automático"), ("pipewire", "PipeWire, qualquer alto-falante"),
                     ("xvf3800", "reSpeaker XVF3800, alto-falante no array"))),
    Setting("leds.enabled", "audio", "Anel de LEDs", "bool", "Mostra o estado da Noha no reSpeaker XVF3800."),
    Setting("leds.brightness", "audio", "Brilho dos LEDs", "int", "", "", 0, 255, 5),
    Setting("voice.daily_budget_usd", "custos", "Limite diário", "float",
            "Atingido o limite, Noha não inicia conversas até a meia-noite. 0 desativa.", "US$", 0, 1000, 0.5),
    Setting("voice.max_output_tokens", "custos", "Tamanho máximo da resposta", "int",
            "Corta respostas longas demais.", "tokens", 50, 4096, 50, advanced=True),
    Setting("voice.context_tokens", "custos", "Memória da conversa", "int",
            "Quanto da conversa é mantido. Cada resposta cobra essa memória de novo.", "tokens", 500, 32000, 500,
            advanced=True),
    Setting("openai_key", "chave", "Chave da API OpenAI", "secret",
            "Deixe vazio para manter a chave salva. A chave nunca é exibida."),
)
BY_KEY = {setting.key: setting for setting in SETTINGS}
SPECIAL = {"personality", "openai_key"}  # stored outside noha.toml


def schema():
    return {"groups": [{"id": g, "label": label, "help": text} for g, label, text in GROUPS],
            "settings": [setting.describe() for setting in SETTINGS]}


def values(config, personality):
    """Current value of every setting except secrets."""
    result = {"personality": personality}
    for setting in SETTINGS:
        if setting.section:
            result[setting.key] = getattr(getattr(config, setting.section), setting.name)
    return result


def _number(setting, value, kind):
    if type(value) is bool or not isinstance(value, (int, float)) or (kind is int and type(value) is not int):
        raise ValueError(f"{setting.label}: informe um número{' inteiro' if kind is int else ''}")
    if not math.isfinite(value) or not setting.minimum <= value <= setting.maximum:
        unit = f" {setting.unit}" if setting.unit and setting.unit != "US$" else ""
        raise ValueError(f"{setting.label}: use de {_plain(setting.minimum)} a {_plain(setting.maximum)}{unit}")
    return kind(value)


def _plain(number):
    return f"{number:g}".replace(".", ",")


def check(setting, value):
    """Normalized value, or ValueError naming the field. Device and GPIO availability is checked by the caller."""
    kind = setting.kind
    if kind == "bool":
        if type(value) is not bool:
            raise ValueError(f"{setting.label}: valor inválido")
        return value
    if kind in ("int", "float"):
        return _number(setting, value, int if kind == "int" else float)
    if kind == "choice":
        if value not in {choice[0] for choice in setting.choices}:
            raise ValueError(f"{setting.label}: opção inválida")
        return value
    if kind == "model":
        model_info(value if isinstance(value, str) else "")
        return value
    if kind == "voice":
        if not isinstance(value, str) or not value:
            raise ValueError(f"{setting.label}: escolha uma voz")
        return value
    if kind == "text":
        if not isinstance(value, str) or not 1 <= len(value.encode()) <= setting.maximum:
            raise ValueError(f"{setting.label}: use de 1 a {setting.maximum} bytes")
        return value
    if kind == "string":
        if not isinstance(value, str) or len(value) > setting.maximum:
            raise ValueError(f"{setting.label}: até {setting.maximum} caracteres")
        return value.strip()
    if kind == "texts":
        if not isinstance(value, list) or not all(isinstance(text, str) for text in value):
            raise ValueError(f"{setting.label}: lista inválida")
        texts = [text.strip() for text in value if text.strip()]
        if not setting.minimum <= len(texts) <= setting.maximum:
            raise ValueError(f"{setting.label}: de {setting.minimum:g} a {setting.maximum:g} frases")
        if any(len(text) > 1000 for text in texts):
            raise ValueError(f"{setting.label}: cada frase pode ter até 1000 caracteres")
        return texts
    if kind == "secret":
        if not isinstance(value, str) or not SECRET.fullmatch(value.strip()):
            raise ValueError(f"{setting.label}: formato inválido")
        return value.strip()
    if kind == "device":
        if not isinstance(value, str) or not DEVICE_NAME.fullmatch(value):
            raise ValueError(f"{setting.label}: dispositivo inválido")
        return value
    if kind == "gpio":
        if type(value) is not int or value not in GPIO_PINS:
            raise ValueError(f"{setting.label}: GPIO BCM inválido")
        return value
    raise ValueError("Campo desconhecido")


def validate(changes, config):
    """Normalized changes. Raises ValueError with a message for the dashboard."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Nenhuma alteração para salvar")
    result = {}
    for key, value in changes.items():
        setting = BY_KEY.get(key)
        if setting is None:
            raise ValueError("Campo desconhecido")
        if setting.kind == "secret" and value in ("", None):
            continue  # empty keeps the saved credential
        result[key] = check(setting, value)
    model = result.get("voice.model", config.voice.model)
    voice = result.get("voice.voice", config.voice.voice)
    if voice not in model_info(model).voices:
        raise ValueError("Voz incompatível com o modelo escolhido")
    if not result:
        raise ValueError("Nenhuma alteração para salvar")
    return result


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)  # always has a dot or exponent: stays a TOML float
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON escapes are valid TOML basic strings
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError("Tipo sem representação TOML")


def _value_end(text, start):
    """Index where the TOML value starting at text[start] ends: its comment or line end. Arrays may span lines."""
    depth, index, length = 0, start, len(text)
    while index < length:
        char = text[index]
        if char in "\"'":
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            while index < length and not text.startswith(quote, index):
                if char == '"' and text[index] == "\\":
                    index += 2
                    continue
                if len(quote) == 1 and text[index] == "\n":
                    break  # unterminated string: the line ends the value
                index += 1
            if text.startswith(quote, index):
                index += len(quote)
            continue
        if char == "#":
            if depth <= 0:
                return index
            while index < length and text[index] != "\n":  # comment inside a multi-line array
                index += 1
            continue
        if char == "\n" and depth <= 0:
            return index
        depth += (char == "[") - (char == "]")
        index += 1
    return index


def set_toml(text, section, key, value):
    """Replace or add `key = value` in [section]; comments and other keys are kept as written."""
    rendered = f"{key} = {toml_value(value)}"
    header = re.search(rf"(?m)^[ \t]*\[[ \t]*{re.escape(section)}[ \t]*\][ \t]*(?:#.*)?$", text)
    if header is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{rendered}\n"
    following = re.search(r"(?m)^[ \t]*\[", text[header.end():])
    end = header.end() + following.start() if following else len(text)
    body = text[header.end():end]
    line = re.search(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*", body)
    if line is None:
        keys = list(re.finditer(r"(?m)^[ \t]*[A-Za-z0-9_-]+[ \t]*=", body))
        if keys:  # after the value of the section's last key
            insert = header.end() + _value_end(body, keys[-1].end())
            insert = text.find("\n", insert)
            insert = len(text) if insert < 0 else insert + 1
        else:
            insert = header.end() + 1
        prefix = "" if text[insert - 1:insert] == "\n" or insert == 0 else "\n"
        return text[:insert] + prefix + rendered + "\n" + text[insert:]
    value_start = header.end() + line.end()
    value_end = value_start + _value_end(text[value_start:end], 0)
    trailing = text[value_end:end].split("\n", 1)[0]
    comment = trailing.strip()
    start = header.end() + line.start()
    replacement = rendered + (f" {comment}" if comment.startswith("#") else "")
    return text[:start] + replacement + text[value_end + len(trailing):]


def apply(text, changes):
    """noha.toml text with the changes written, verified by parsing it back."""
    for key, value in changes.items():
        setting = BY_KEY[key]
        if setting.section:
            text = set_toml(text, setting.section, setting.name, value)
    parsed = tomllib.loads(text)
    for key, value in changes.items():
        setting = BY_KEY[key]
        if setting.section and parsed.get(setting.section, {}).get(setting.name) != value:
            raise ValueError("Não foi possível gravar a configuração com segurança")
    return text
