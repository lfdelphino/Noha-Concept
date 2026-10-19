import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import subprocess

from .config import Config


async def with_signals(coroutine):
    task = asyncio.create_task(coroutine)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def doctor(config):
    from .audio import devices
    failures = 0
    def report(ok, text):
        nonlocal failures
        print(("OK   " if ok else "ERRO ") + text)
        failures += not ok
    report(os.uname().machine == "aarch64", "Sistema ARM64")
    report(Path(config.voice.instructions).is_file(), "Arquivo de personalidade")
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    try:
        has_key = has_key or any(line.startswith("OPENAI_API_KEY=") and bool(line.partition("=")[2].strip())
                                 for line in Path("/etc/noha/secrets.env").read_text().splitlines())
    except OSError:
        pass
    report(has_key, "Chave OpenAI configurada (valor oculto)")
    try:
        names = {d["name"] for d in devices()}
    except Exception:
        names = set()
    checks = [("Microfone", config.audio.microphone), ("Alto-falante", config.audio.speaker)]
    if config.audio.hardware_aec():
        print("OK   Cancelamento de eco: reSpeaker XVF3800 (saída de áudio do próprio array)")
    else:
        checks += [("Fonte AEC PipeWire", config.audio.clean_source), ("Saída AEC PipeWire", config.audio.reference_sink)]
    for label, name in checks:
        report(bool(name and name in names), f"{label}: {name or 'não configurado'}")
    from .audio import profile_missing
    if profile_missing(config.audio, names):
        print("     Placa presente sem saída/entrada: tela HDMI desligada no boot? "
              "Com a tela ligada: systemctl --user restart pipewire pipewire-pulse wireplumber")
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        report(all(Gst.ElementFactory.find(n) for n in ("pulsesrc", "pulsesink", "appsink", "appsrc", "audioresample")),
               "Plugins GStreamer")
    except Exception:
        report(False, "GStreamer / PyGObject indisponível")
    try:
        result = subprocess.run(["pinctrl", "get", str(config.motion.gpio)], capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
        report(result.returncode == 0 and "SCL" not in value and "SDA" not in value,
               f"GPIO BCM{config.motion.gpio}: {value}")
    except Exception:
        report(False, "Não foi possível consultar pinctrl")
    try:
        from .motion import rp1_chip
        report(True, f"Controlador GPIO RP1: gpiochip{rp1_chip()}")
    except Exception as exc:
        report(False, f"Controlador GPIO RP1 ({type(exc).__name__}: {exc})")
    if config.leds.enabled:
        from .leds import find, firmware_version
        try:
            report(True, f"LEDs reSpeaker XVF3800: firmware {firmware_version(find())}")
        except Exception as exc:
            report(False, f"LEDs reSpeaker XVF3800 ({type(exc).__name__}); confira USB e o grupo plugdev")
    print("Verifique também: systemctl status noha-network; systemctl --user status noha")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Noha — voz, movimento e configuração Wi-Fi")
    parser.add_argument("--config", default="/etc/noha/noha.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--simulate-motion", action="store_true")
    for name in ("doctor", "audio-list", "status", "trigger", "network", "portal", "web", "admin", "dashboard-password", "ngrok"):
        sub.add_parser(name)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.command == "dashboard-password":
        if os.geteuid() != 0:
            raise SystemExit("Execute sudo noha dashboard-password")
        import getpass
        from .admin import atomic_write, ETC
        from .passwords import password_record
        password = getpass.getpass("Nova senha do painel (Enter para admin): ") or "admin"
        if password != (getpass.getpass("Confirme a senha: ") or "admin"):
            raise SystemExit("As senhas não coincidem")
        atomic_write(ETC / "dashboard-password.json", password_record(password), backup=True)
        print("Senha alterada. Todos os acessos anteriores foram encerrados.")
        return
    if args.command == "admin":
        from .admin import serve
        asyncio.run(with_signals(serve()))
        return
    if args.command == "ngrok":
        # The service waits for dashboard setup without failing in a restart loop.
        import time
        while not Path("/etc/noha/ngrok.yml").is_file():
            time.sleep(5)
        os.execvp("ngrok", ["ngrok", "start", "noha", "--config", "/etc/noha/ngrok.yml"])
    if args.command == "audio-list":
        from .audio import devices
        print(json.dumps(devices(), ensure_ascii=False, indent=2))
        return
    if args.command in {"trigger", "status"}:
        from .app import runtime_dir
        if args.command == "trigger":
            (runtime_dir() / "trigger").touch(mode=0o600)
            print("Movimento simulado enviado ao serviço do usuário.")
        else:
            path = runtime_dir() / "status.json"
            print(path.read_text() if path.exists() else "Serviço ainda não publicou estado.")
            print("Rede: systemctl status noha-network")
        return
    config = Config.load(args.config)
    if args.command == "doctor":
        raise SystemExit(doctor(config))
    if args.command == "run":
        from .app import run
        coroutine = run(config, simulate=args.simulate_motion)
    elif args.command == "network":
        from .controller import serve
        coroutine = serve(config.network)
    elif args.command == "web":
        from .dashboard import serve
        coroutine = serve(config.network)
    else:
        from .portal import serve
        coroutine = serve(config.network)
    asyncio.run(with_signals(coroutine))


if __name__ == "__main__":
    main()
