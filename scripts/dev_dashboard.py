#!/usr/bin/env python3
"""Preview the dashboard and the status board with sample data, without a Pi.

    python scripts/dev_dashboard.py [--scenario ok|conversando|offline|problemas|primeiro-acesso] [--port 8080]

Open http://localhost:8080 (password: noha; first-run scenario: admin) or http://localhost:8080/kiosk?token=dev.
"""
import argparse
import asyncio
import math
from pathlib import Path
import random
import sys
import time

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from noha import diagnostics, settings  # noqa: E402
from noha.config import Config, Network  # noqa: E402
from noha.dashboard import Dashboard, make_app  # noqa: E402
from noha.models import GPIO_PINS, catalog  # noqa: E402

DEVICES = [
    {"id": 51, "name": "alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_1149-00.analog-stereo",
     "description": "reSpeaker XVF3800 4-Mic Array", "class": "Audio/Source"},
    {"id": 57, "name": "alsa_output.usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_1149-00.analog-stereo",
     "description": "reSpeaker XVF3800 (saída P2)", "class": "Audio/Sink"},
    {"id": 60, "name": "alsa_output.platform-107c701400.hdmi.hdmi-stereo", "description": "Tela HDMI", "class": "Audio/Sink"},
]


class Sample:
    def __init__(self, scenario):
        self.scenario = scenario
        self.started = time.time()
        self.presence = True
        self.volume = 70
        self.pending = {}
        self.apply = {}
        self.default = scenario == "primeiro-acesso"
        self.config = Config.load(ROOT / "config/noha.toml")
        self.personality = (ROOT / "config/personality.txt").read_text(encoding="utf-8")

    def voice(self, now):
        talking = self.scenario == "conversando"
        offline = self.scenario == "offline"
        cycle = now % 40
        moving = 6 < cycle < 14 or talking
        history = [[now - 560, now - 548], [now - 410, now - 396], [now - 300, now - 291], [now - 140, now - 128]]
        if moving:
            history.append([now - (cycle - 6), None])
        voice = {"state": "aguardando_movimento" if self.presence else "sensor_desligado", "detail": "", "busy": False,
                 "speaking": False, "ready": True, "updating": False, "audio_ready": True, "audio_missing": [], "gpio_ready": True,
                 "motion": moving, "last_motion": now if moving else now - 128 + (cycle if cycle < 6 else 0),
                 "motion_history": history, "gate": "pronto" if self.presence else "desligado", "gate_seconds": 0,
                 "sensor_enabled": self.presence, "greeting_local": True, "local_greetings": 4, "setup_ready": not offline,
                 "offline_voice": True, "key_configured": True, "config_digest": self.config.digest, "model": self.config.voice.model,
                 "gpio": 3, "version": "2.0.0", "pid": 4242, "updated": now, "stale": False}
        if talking:
            voice.update(state="conversando", busy=True, speaking=int(now) % 6 < 3, gate="conversando")
        if offline:
            voice.update(state="orientando_wifi", detail="Sem internet: Noha explica como configurar o Wi-Fi pelo celular", busy=True,
                         gate="conversando")
        if self.scenario == "problemas":
            voice.update(state="audio_indisponivel", detail="Não encontrado: Microfone. Confira cabos, tela e a seleção nos ajustes",
                         audio_ready=False, audio_missing=["Microfone"], local_greetings=0, setup_ready=False)
        return voice

    def status(self):
        now = time.time()
        hourly = [0] * 24
        for hour, count in ((9, 1), (10, 2), (11, 3), (12, 1), (14, 4), (15, 2), (16, 1)):
            hourly[hour] = count
        offline = self.scenario == "offline"
        status = {
            "voice": self.voice(now),
            "network": {"mode": "configuracao" if offline else "online", "ssid": "Noha-Setup-A1B2", "busy": False,
                        "message": "", "portal_url": "http://192.168.50.1", "wifi": "" if offline else "Casa Modelo"},
            "model": self.config.voice.model, "voice_name": self.config.voice.voice,
            "system": {"cpu_percent": round(18 + 6 * math.sin(now / 7), 1), "uptime_seconds": 3 * 86400 + 5 * 3600 + now - self.started,
                       "temperature": 52.4, "memory_total": 8 * 2**30, "memory_used": int(1.9 * 2**30),
                       "disk_total": 58 * 2**30, "disk_used": int(12.6 * 2**30), "hostname": "noha"},
            "ngrok": {"url": "https://noha-casa.ngrok-free.app", "state": "reconectando" if offline else "online"},
            "local_url": "http://noha.local", "pending": bool(self.pending), "pending_keys": sorted(self.pending), "apply": self.apply,
            "updates": {"state": "available" if self.scenario == "problemas" else "current", "installed": "2.0.0",
                        "latest": "v2.0.1" if self.scenario == "problemas" else "v2.0.0", "checked_at": now - 1800,
                        "message": "Verificação concluída."},
            "presence": self.presence, "key_configured": self.scenario != "problemas",
            "budget": {"limit": 2.0, "spent": 1.74 if self.scenario == "problemas" else 0.42},
            "today": {"visits": 14, "conversations": 6, "greetings": 12, "cost_usd": 1.74 if self.scenario == "problemas" else 0.42,
                      "duration_seconds": 1262, "hourly": hourly},
            "security": {"default_password": self.default},
        }
        status["checks"] = diagnostics.checks(status)
        return status

    def usage(self, days):
        rng = random.Random(days)
        now = time.time()
        from noha.metrics import buckets
        series = []
        for start, label in buckets(days, now):
            if days == 1:
                hour = int(label[:-1])
                visits = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 1, 0, 4, 2, 1, 0, 0, 0, 0, 0, 0, 0][hour]
            else:
                visits = rng.randint(4, 22)
            talks = round(visits * rng.uniform(.3, .6))
            series.append({"start": start, "label": label, "visits": visits, "conversations": talks, "cost_usd": talks * 0.06})
        outcomes = {"conversou": sum(s["conversations"] for s in series), "sem_resposta": 0, "wifi": 1 if days > 1 else 0,
                    "falha": 1 if days > 7 else 0, "sem_chave": 0}
        total = sum(s["visits"] for s in series)
        outcomes["sem_resposta"] = total - sum(outcomes.values())
        visits = []
        moment = now - 300
        for index in range(min(total, 25)):
            outcome = ("conversou", "sem_resposta", "sem_resposta", "conversou", "wifi")[index % 5]
            length = rng.randint(60, 260) if outcome == "conversou" else rng.randint(8, 14)
            visits.append({"started": moment - length, "ended": moment, "outcome": outcome, "model": self.config.voice.model,
                           "cost_usd": round(length / 60 * 0.05, 4) if outcome == "conversou" else 0.0,
                           "replies": rng.randint(2, 7) if outcome == "conversou" else 0})
            moment -= rng.randint(600, 4000) * (days if days > 1 else 1)
        cost = sum(s["cost_usd"] for s in series)
        models = [{"model": self.config.voice.model, "conversations": outcomes["conversou"], "greetings": total - 2,
                   "duration_seconds": outcomes["conversou"] * 150, "input_tokens": 412000 * days, "output_tokens": 36000 * days,
                   "cached_tokens": 298000 * days, "errors": 0, "reconnects": 1, "cost_usd": cost - 0.012},
                  {"model": "gpt-4o-mini-tts", "conversations": 0, "greetings": 0, "duration_seconds": 0, "input_tokens": None,
                   "output_tokens": None, "cached_tokens": None, "errors": 0, "reconnects": 0, "cost_usd": 0.012}]
        totals = {"visits": total, "conversations": outcomes["conversou"], "greetings": total - 2,
                  "duration_seconds": outcomes["conversou"] * 150, "cost_usd": cost, "input_tokens": 412000 * days,
                  "output_tokens": 36000 * days, "cached_tokens": 298000 * days, "errors": 0, "reconnects": 1}
        return {"days": days, "models": models, "totals": totals, "series": series, "visits": visits, "outcomes": outcomes}

    def logs(self):
        now = time.time()
        lines = [("6", "voice state=aguardando_movimento"), ("6", "Saudações locais prontas count=4"),
                 ("6", "voice state=cumprimentando"), ("6", "Fala após a saudação piso_dbfs=-58 pico_dbfs=-21; conectando"),
                 ("6", "response status=completed input_tokens=2210 output_tokens=184 cached_tokens=1536"),
                 ("4", "Sessão interrompida type=ConnectionClosedError tentativa=1"),
                 ("6", "session duration_seconds=94.2 replies=4 ignored=1"), ("6", "Saudação sem resposta piso_dbfs=-61 pico_dbfs=-44; nenhuma chamada à API")]
        return {"entries": [{"id": str(i), "at": str(int((now - 600 + i * 70) * 1e6)), "priority": p, "message": m}
                            for i, (p, m) in enumerate(lines)]}

    async def call(self, operation, method, data):
        data = data or {}
        if operation == "auth":
            return 200, {"revision": "dev", "default": self.default}
        if operation == "login":
            expected = "admin" if self.default else "noha"
            return (200, {"revision": "dev", "default": self.default}) if data.get("password") == expected else (401, {"error": "Senha inválida"})
        if operation == "kiosk":
            return (200, {"revision": "dev"}) if data.get("token") == "dev" else (401, {"error": "Token inválido"})
        if operation == "password":
            self.default = False
            return 200, {"accepted": True}
        if operation == "status":
            return 200, self.status()
        if operation == "settings" and method == "GET":
            return 200, {"values": settings.values(self.config, self.personality), "schema": settings.schema(), "models": catalog(),
                         "gpio_pins": GPIO_PINS, "devices": DEVICES, "openai_key_configured": True,
                         "ngrok_url": "https://noha-casa.ngrok-free.app", "ngrok_token_configured": True,
                         "local_url": "http://noha.local", "pending": {k: (True if k == "openai_key" else v) for k, v in self.pending.items()},
                         "apply": self.apply}
        if operation == "settings":
            try:
                changes = settings.validate(data.get("changes"), self.config)
            except ValueError as exc:
                return 400, {"error": str(exc)}
            self.pending.update(changes)
            asyncio.get_running_loop().call_later(8, self.applied)
            return 200, {"accepted": True, "pending": {k: (True if k == "openai_key" else v) for k, v in self.pending.items()},
                         "message": "Salvo. Noha aplica assim que estiver livre e reinicia a voz em alguns segundos."}
        if operation == "discard":
            self.pending = {}
            return 200, {"accepted": True, "message": "Alterações pendentes descartadas"}
        if operation == "presence":
            self.presence = bool(data.get("enabled"))
            return 200, {"accepted": True, "enabled": self.presence,
                         "message": "Saudações por movimento ligadas" if self.presence else "Saudações por movimento desligadas"}
        if operation == "trigger":
            return 200, {"accepted": True, "message": "Noha vai cumprimentar agora, como se alguém tivesse chegado."}
        if operation == "volume":
            if method == "POST":
                self.volume = data.get("level", self.volume)
            return 200, {"level": self.volume, "muted": False}
        if operation == "usage":
            return 200, self.usage(int(data.get("days", 1)))
        if operation == "logs":
            return 200, self.logs()
        if operation == "ngrok":
            return 200, {"accepted": True, "url": data.get("url"), "message": "Acesso pela internet reconectando em 3 segundos."}
        if operation in {"check-update", "install-update"}:
            return 200, {"accepted": True, "message": "Verificação solicitada"}
        return 404, {"error": "Operação desconhecida"}

    def applied(self):
        for key, value in self.pending.items():
            if key == "personality":
                self.personality = value
            elif settings.BY_KEY[key].section:
                section, name = key.split(".")
                setattr(getattr(self.config, section), name, value)
        self.pending = {}
        self.apply = {"state": "applied", "message": "Alterações aplicadas", "at": time.time()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="ok", choices=("ok", "conversando", "offline", "problemas", "primeiro-acesso"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    sample = Sample(args.scenario)

    async def call(self, app, operation, method="GET", data=None):
        return await sample.call(operation, method, data)
    Dashboard.call = call
    app = make_app(Network())
    app.cleanup_ctx.clear()  # no admin socket: the sample answers instead
    print(f"Painel: http://localhost:{args.port}  ·  quadro: http://localhost:{args.port}/kiosk?token=dev  ·  "
          f"senha: {'admin' if sample.default else 'noha'}")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
