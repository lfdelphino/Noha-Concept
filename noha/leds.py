"""Status ring of the reSpeaker XVF3800 USB array, through XMOS vendor control transfers."""
import logging
import struct
import time

log = logging.getLogger(__name__)

VENDOR, PRODUCT = 0x2886, 0x001A
GPO, VERSION_RESID = 20, 48  # LED servicer and firmware version resource ids
EFFECT, BRIGHTNESS, GAMMIFY, COLOR, DOA_COLOR = 12, 13, 14, 16, 17
OFF, BREATH, RAINBOW, SOLID, DOA = 0, 1, 2, 3, 4
OUT, IN = 0x40, 0xC0  # vendor request to the device
RETRY = 64  # servicer busy; reissue the read
TIMEOUT_MS = 500

# (effect, colors...). DOA takes a base color plus the highlight that points at the speaker.
PATTERNS = {
    "off": (OFF,),
    "warming": (BREATH, 0xFF8C00),
    "connecting": (BREATH, 0x0078FF),
    "listening": (DOA, 0x001830, 0x00B4FF),
    "speaking": (SOLID, 0x00FFB4),
    "updating": (RAINBOW,),
    "error": (SOLID, 0xFF0000),
}


def pattern_for(state, speaking=False, warming=False, updating=False):
    if updating:
        return "updating"
    if state in ("conversando", "cumprimentando", "orientando_wifi"):
        return "speaking" if speaking else "listening"
    if state in ("conectando", "recuperando_audio_ou_conexao"):
        return "connecting"
    if state.startswith(("erro", "chave_ausente", "indisponivel", "gpio_indisponivel", "audio_indisponivel", "sem_conexao", "orcamento_esgotado")):
        return "error"
    # The voice loop never leaves "aquecendo_sensor" by itself; trust the warmup clock.
    return "warming" if warming else "off"


def scale(color, brightness):
    # Firmware brightness only affects breath/rainbow; dim solid and DOA colors here.
    return sum(((color >> shift & 0xFF) * brightness // 255) << shift for shift in (16, 8, 0))


def find():
    import usb.core
    device = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
    if device is None:
        raise LookupError("reSpeaker XVF3800 não encontrado")
    return device


def firmware_version(device):
    for _ in range(10):
        response = device.ctrl_transfer(IN, 0, 0x80 | 0, VERSION_RESID, 4, TIMEOUT_MS)
        if response[0] == 0:
            return ".".join(str(b) for b in response[1:4])
        if response[0] != RETRY:
            raise RuntimeError("XVF3800 recusou a leitura")
        time.sleep(0.01)
    raise TimeoutError("XVF3800 ocupado")


class Ring:
    def __init__(self, config, opener=find, clock=time.monotonic):
        self.config, self.opener, self.clock = config, opener, clock
        self.device = None
        self.shown = None
        self.retry_at = 0
        self.warned = False

    def _write(self, command, fmt, *values):
        self.device.ctrl_transfer(OUT, 0, command, GPO, struct.pack("<" + fmt * len(values), *values), TIMEOUT_MS)

    def _apply(self, pattern):
        effect, *colors = PATTERNS[pattern]
        level = self.config.brightness
        if effect in (BREATH, RAINBOW):
            self._write(BRIGHTNESS, "B", level)
        if effect == BREATH:
            self._write(COLOR, "I", colors[0])
        elif effect == SOLID:
            self._write(COLOR, "I", scale(colors[0], level))
        elif effect == DOA:
            self._write(DOA_COLOR, "I", *(scale(c, level) for c in colors))
        # Colors first, effect last: no flash of the previous color.
        self._write(EFFECT, "B", effect)

    def show(self, pattern):
        if not self.config.enabled or pattern == self.shown:
            return
        now = self.clock()
        try:
            if self.device is None:
                if now < self.retry_at:
                    return
                self.device = self.opener()
                self._write(GAMMIFY, "B", 1)
            self._apply(pattern)
            self.shown = pattern
            self.warned = False
        except Exception as exc:
            # LEDs are cosmetic: never interrupt the conversation, retry later.
            if not self.warned:
                log.warning("LEDs XVF3800 indisponíveis type=%s", type(exc).__name__)
                self.warned = True
            self.device = self.shown = None
            self.retry_at = now + 30

    def close(self):
        if self.device is not None:
            self.show("off")
            try:
                import usb.util
                usb.util.dispose_resources(self.device)
            except Exception:
                pass
            self.device = None
