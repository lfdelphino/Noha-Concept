"""`noha sensor watch`: the motion sensor's pin level, live in the terminal.

Reads the level with pinctrl, which only reads the register: it works while the voice service
holds the GPIO line, and also with the service stopped."""
from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .models import GPIO_PINS

GATES = {"aquecendo": "aquecendo", "pronto": "pronto para cumprimentar", "confirmando": "confirmando visitante",
         "rearmando": "rearmando", "conversando": "em conversa", "desligado": "saudações desligadas"}


def parse_level(output):
    """True (hi) / False (lo) from `pinctrl get 17` ("17: ip pd | hi // GPIO17 = input"); None if unreadable."""
    match = re.search(r"\|\s*(hi|lo)\b", output)
    return None if match is None else match.group(1) == "hi"


def read_pin(pin):
    try:
        result = subprocess.run(["pinctrl", "get", str(pin)], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_level(result.stdout)


def read_robot():
    """What the voice service makes of the sensor, from its heartbeat; None when it is not running."""
    uid = os.environ.get("SUDO_UID") or os.getuid()
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}") / "noha/status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - status.get("updated", 0) > 15:
        return None
    gate = status.get("gate")
    label = GATES.get(gate, gate or "—")
    seconds = status.get("gate_seconds")
    return f"{label} {seconds} s" if gate in ("aquecendo", "rearmando", "confirmando") and seconds else label


class Strip:
    """The last `cells` seconds, one character per second: █ motion, ▁ quiet, ? unreadable."""
    def __init__(self, cells=60):
        self.cells = cells
        self.samples = deque()

    def add(self, now, level):
        self.samples.append((now, level))
        while self.samples and self.samples[0][0] < now - self.cells:
            self.samples.popleft()

    def render(self, now):
        marks = []
        for cell in range(self.cells):
            start = now - self.cells + cell
            seen = [level for at, level in self.samples if start < at <= start + 1]  # the newest cell ends now
            marks.append("█" if True in seen else "▁" if False in seen else "?" if seen else " ")
        return "".join(marks)


def seconds(value):
    return f"{value:.1f} s".replace(".", ",")


def watch(pin, interval=0.1, out=None, read=read_pin, robot=read_robot, clock=time.monotonic, wall=time.time,
          sleep=time.sleep, width=None):
    out = out or sys.stdout
    color = out.isatty() if hasattr(out, "isatty") else False
    green, dim, reset = ("\033[1;32m", "\033[2m", "\033[0m") if color else ("", "", "")
    columns = width or shutil.get_terminal_size((100, 20)).columns
    strip = Strip(max(10, min(60, columns - 62)))
    physical = GPIO_PINS.get(pin)
    out.write(f"Sensor no GPIO {pin}" + (f" (pino físico {physical})" if physical else "") +
              ". Ctrl+C para sair.\n")
    if pin in (2, 3):
        out.write("Aviso: este pino tem resistor pull-up na placa e sempre lê alto. Use o GPIO 17 (pino 11).\n")
    out.write(f"{dim}█ movimento  ▁ parado  · um caractere por segundo, mais recente à direita{reset}\n\n")
    state, since, started = object(), clock(), clock()
    detections, moving_total = 0, 0.0
    try:
        while True:
            now, level = clock(), read(pin)
            if level != state:
                held = now - since
                stamp = time.strftime("%H:%M:%S", time.localtime(wall())) + f".{int(wall() % 1 * 10)}"
                if level is True:
                    detections += 1
                    line = f"{stamp}  {green}▲ MOVIMENTO{reset}" + (f"  (parado por {seconds(held)})" if state is False else "")
                elif level is False:
                    line = f"{stamp}  ▼ parado" + (f"     (movimento durou {seconds(held)})" if state is True else "")
                else:
                    line = f"{stamp}  ? sem leitura: pinctrl indisponível ou pino inválido"
                if state is True:
                    moving_total += held
                out.write(f"\r\033[K{line}\n")
                state, since = level, now
            strip.add(now, level)
            current = f"{green}MOVIMENTO{reset}" if level else "parado   " if level is False else "?        "
            view = robot()
            out.write(f"\r\033[K{current} há {seconds(now - since):>8}  [{strip.render(now)}]  "
                      f"robô: {view if view else 'serviço de voz parado'}")
            out.flush()
            sleep(interval)
    except KeyboardInterrupt:
        if state is True:
            moving_total += clock() - since
        total = clock() - started
        out.write(f"\n\n{detections} {'detecção' if detections == 1 else 'detecções'} em {seconds(total)}; "
                  f"movimento em {seconds(moving_total)} ({moving_total / total:.0%} do tempo).\n" if total else "\n")
