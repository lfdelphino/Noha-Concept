#!/usr/bin/env python3
"""Installer for Raspberry Pi OS. Run with sudo; API key never goes in argv."""
import argparse
import datetime
import getpass
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEST = Path("/opt/noha")
ETC = Path("/etc/noha")
STATE = Path("/var/lib/noha")
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=capture, **kwargs)


def backup(path):
    if path.exists():
        target = STATE / "backups" / STAMP / str(path).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def write(path, content, mode=0o644):
    if path.exists() and path.read_text() == content:
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".noha-tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    os.chmod(temp, mode)
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Instala Noha no Raspberry Pi OS Desktop 64-bit")
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--mic", help="PipeWire node.name estável do microfone")
    parser.add_argument("--speaker", help="PipeWire node.name estável do alto-falante")
    parser.add_argument("--country", help="País Wi-Fi de duas letras; solicitado se ainda não configurado")
    parser.add_argument("--disable-i2c", action="store_true", help="Desativa I2C após conflito no GPIO3; requer reiniciar")
    parser.add_argument("--hostname", default=None, help="Nome mDNS do Pi (padrão na primeira instalação: noha)")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Execute com sudo bash scripts/install.sh --user SEU_USUARIO")
    if not args.user or args.user == "root":
        sys.exit("Informe --user com o usuário do desktop, não root.")
    account = pwd.getpwnam(args.user)
    os_release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    codename = os_release.get("VERSION_CODENAME", "").strip('"')
    model_path = Path("/proc/device-tree/model")
    model = model_path.read_text().rstrip("\0") if model_path.exists() else ""
    if platform.machine() != "aarch64" or "Raspberry Pi 5" not in model or codename not in {"bookworm", "trixie"}:
        sys.exit("Requer Raspberry Pi 5 com Raspberry Pi OS Desktop Bookworm/Trixie ARM64.")
    if not Path("/etc/rpi-issue").exists() or not shutil.which("NetworkManager"):
        sys.exit("Requer Raspberry Pi OS com NetworkManager.")
    if not shutil.which("pipewire"):
        sys.exit("PipeWire não encontrado. Instale/use Raspberry Pi OS Desktop com PipeWire.")
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    update_lock = (STATE / "update.lock").open("a")
    try:
        fcntl.flock(update_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Atualização em andamento; aguarde antes de instalar.")
    old_manifest = json.loads((STATE / "install.json").read_text()) if (STATE / "install.json").exists() else {}
    if old_manifest and old_manifest["user"] != args.user:
        sys.exit("Instalação existente usa outro usuário. Remova o serviço anterior primeiro.")
    if (STATE / "update-transaction.json").exists():
        from updater import recover
        recover()
    os.umask(0o022)
    config_path = ETC / "noha.toml"
    existing = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    configured_gpio = existing.get("motion", {}).get("gpio", 3)
    pin = run("pinctrl", "get", str(configured_gpio), capture=True).stdout
    if "SCL" in pin or "SDA" in pin:
        if args.disable_i2c:
            run("raspi-config", "nonint", "do_i2c", "1")
            sys.exit(f"I2C desativado para liberar GPIO{configured_gpio}. Reinicie o Pi e execute o instalador novamente.")
        sys.exit(f"GPIO{configured_gpio} está ocupado por I2C. Se não usa dispositivos I2C, repita com --disable-i2c e reinicie.")

    print("Instalando dependências de voz, GPIO e rede…")
    run("apt-get", "update")
    run("apt-get", "install", "-y", "python3-venv", "python3-gi", "python3-gst-1.0",
        "python3-gpiozero", "python3-lgpio", "gir1.2-gstreamer-1.0", "gstreamer1.0-tools",
        "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good", "pipewire", "pipewire-pulse",
        "wireplumber", "libspa-0.2-modules", "dnsmasq-base", "network-manager", "nftables", "iw",
        "avahi-daemon", "avahi-utils", "libnss-mdns", "ca-certificates", "gnupg")
    if not shutil.which("ngrok"):
        # Install through ngrok's signed apt repository; never pipe a remote shell script.
        key = urllib.request.urlopen("https://ngrok-agent.s3.amazonaws.com/ngrok.asc", timeout=30).read()
        key_path = Path("/etc/apt/keyrings/noha-ngrok.asc")
        write(key_path, key.decode("ascii"), 0o644)
        write(Path("/etc/apt/sources.list.d/noha-ngrok.list"),
              "deb [signed-by=/etc/apt/keyrings/noha-ngrok.asc] https://ngrok-agent.s3.amazonaws.com bookworm main\n")
        run("apt-get", "update")
        run("apt-get", "install", "-y", "ngrok")
    hostname = args.hostname or old_manifest.get("hostname", "noha")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", hostname):
        sys.exit("Hostname inválido; use letras, números e hífen, até 63 caracteres")
    if platform.node() != hostname:
        backup(Path("/etc/hostname"))
        run("hostnamectl", "set-hostname", hostname)
        hosts = Path("/etc/hosts")
        hosts_text = hosts.read_text()
        if re.search(r"(?m)^127\.0\.1\.1\s+", hosts_text):
            hosts_text = re.sub(r"(?m)^127\.0\.1\.1\s+.*$", "127.0.1.1\t" + hostname, hosts_text)
        else:
            hosts_text += "\n127.0.1.1\t" + hostname + "\n"
        write(hosts, hosts_text)
    # Advertise an HTTP service as well as the hostname for local discovery.
    write(Path("/etc/avahi/services/noha.service"),
          '<?xml version="1.0" standalone="no"?><!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
          '<service-group><name replace-wildcards="yes">Noha — %h</name><service><type>_http._tcp</type>'
          '<port>80</port><txt-record>path=/</txt-record></service></service-group>\n')
    run("systemctl", "enable", "--now", "avahi-daemon.service")
    run("systemctl", "restart", "avahi-daemon.service")
    reg = run("iw", "reg", "get", capture=True).stdout
    if args.country or not re.search(r"country (?!00)[A-Z]{2}:", reg):
        country = args.country or input("País de uso do Wi-Fi (BR, US, etc.): ").strip().upper()
        if not re.fullmatch("[A-Z]{2}", country):
            sys.exit("País inválido")
        run("raspi-config", "nonint", "do_wifi_country", country)
    run("nmcli", "radio", "wifi", "on")
    # A dedicated portal account cannot read the robot's API credentials.
    if subprocess.run(["getent", "passwd", "noha-portal"], stdout=subprocess.DEVNULL).returncode:
        run("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", "noha-portal")
    if subprocess.run(["getent", "group", "noha"], stdout=subprocess.DEVNULL).returncode:
        run("groupadd", "--system", "noha")
    if subprocess.run(["getent", "passwd", "noha-ngrok"], stdout=subprocess.DEVNULL).returncode:
        run("useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", "noha-ngrok")
    run("usermod", "-a", "-G", "audio,gpio,noha", args.user)
    manifest_path = STATE / "install.json"
    linger_before = old_manifest.get("linger_before", Path(f"/var/lib/systemd/linger/{args.user}").exists())
    run("loginctl", "enable-linger", args.user)
    run("systemctl", "start", f"user@{account.pw_uid}.service")
    user_env = {**os.environ, "HOME": account.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{account.pw_uid}/bus"}
    def user_run(*cmd, capture=False, **kwargs):
        return run("runuser", "-u", args.user, "--", *cmd, capture=capture, env=user_env, **kwargs)
    user_run("systemctl", "--user", "start", "pipewire.service", "pipewire-pulse.service", "wireplumber.service")
    nodes = json.loads(user_run("pw-dump", capture=True).stdout)
    props = [n.get("info", {}).get("props", {}) for n in nodes]
    config_path = ETC / "noha.toml"
    existing = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    def choose(kind, supplied, old):
        choices = [p for p in props if p.get("media.class") == kind
                   and not p.get("node.name", "").startswith("noha_")]
        by_name = {p["node.name"]: p for p in choices}
        selected = supplied or old
        if selected:
            if selected not in by_name:
                sys.exit(f"Dispositivo configurado ausente: {selected}. Conecte-o ou use --mic / --speaker.")
            return selected
        if not choices:
            sys.exit(f"Nenhum dispositivo {kind}. Conecte o áudio USB e repita.")
        for index, item in enumerate(choices, 1):
            print(f"{index}: {item.get('node.description', '')} [{item['node.name']}]")
        selected_index = int(input(f"Selecione {kind} (número): ")) - 1
        if not 0 <= selected_index < len(choices):
            sys.exit("Seleção inválida")
        return choices[selected_index]["node.name"]
    mic = choose("Audio/Source", args.mic, existing.get("audio", {}).get("microphone"))
    speaker = choose("Audio/Sink", args.speaker, existing.get("audio", {}).get("speaker"))

    DEST.mkdir(parents=True, exist_ok=True)
    package_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    deployment = DEST / "releases" / f"installed-{package_version}-{STAMP}"
    source = deployment / "source"
    source.mkdir(parents=True, exist_ok=True)
    # Copy only application sources; never copy a developer's environment or credentials.
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(ROOT / "noha", source / "noha", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "systemd", source / "systemd", dirs_exist_ok=True)
    release_metadata = {"format": 1, "version": package_version, "requires_installer": False}
    write(deployment / "release.json", json.dumps(release_metadata), 0o644)
    run("python3", "-m", "venv", "--system-site-packages", str(deployment / "venv"))
    run(str(deployment / "venv/bin/pip"), "install", str(source))
    # Keep deployment paths stable: virtual environments contain absolute entrypoint paths.
    from updater import switch
    switch(deployment)
    ETC.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        write(config_path, (ROOT / "config/noha.toml").read_text())
    content = config_path.read_text()
    for key, value in (("microphone", mic), ("speaker", speaker)):
        content = re.sub(rf"(?m)^{key}\s*=.*$", lambda _: f"{key} = {json.dumps(value)}", content)
    write(config_path, content)
    if not (ETC / "personality.txt").exists():
        write(ETC / "personality.txt", (ROOT / "config/personality.txt").read_text())
    if not (ETC / "updates.toml").exists():
        write(ETC / "updates.toml", (ROOT / "config/updates.toml").read_text())
    secret_path = ETC / "secrets.env"
    if not secret_path.exists():
        key = getpass.getpass("Chave OpenAI (oculta; Enter para configurar depois): ").strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            sys.exit("Formato da chave inválido")
        write(secret_path, f"OPENAI_API_KEY={key}\n", 0o640)
    import grp
    os.chown(secret_path, 0, grp.getgrnam("noha").gr_gid)
    os.chmod(secret_path, 0o640)
    # Import directly from this checkout, before switching the service to its venv.
    sys.path.insert(0, str(ROOT))
    from noha.passwords import password_record
    password_path = ETC / "dashboard-password.json"
    if not password_path.exists():
        write(password_path, json.dumps(password_record("admin")), 0o600)
    metrics_dir = Path("/var/lib/noha-metrics")
    metrics_dir.mkdir(mode=0o700, exist_ok=True)
    os.chown(metrics_dir, account.pw_uid, account.pw_gid)
    os.chmod(metrics_dir, 0o700)

    user_config = Path(account.pw_dir) / ".config"
    echo_path = user_config / "pipewire/pipewire.conf.d/80-noha-echo.conf"
    echo = '''context.modules = [
  { name = libpipewire-module-echo-cancel
    args = {
      library.name = aec/libspa-aec-webrtc
      audio.rate = 48000
      audio.channels = 1
      audio.position = [ MONO ]
      capture.props = { node.name = noha_echo_capture target.object = MIC node.dont-fallback = true node.passive = true }
      source.props = { node.name = noha_echo_source node.description = "Noha microphone (AEC)" }
      sink.props = { node.name = noha_echo_sink node.description = "Noha speaker (AEC)" }
      playback.props = { node.name = noha_echo_playback target.object = SPEAKER node.dont-fallback = true node.passive = true }
    }
  }
]
'''.replace("MIC", json.dumps(mic)).replace("SPEAKER", json.dumps(speaker))
    write(echo_path, echo)
    user_unit = user_config / "systemd/user/noha.service"
    write(user_unit, (ROOT / "systemd/noha.service").read_text())
    for path in (echo_path, echo_path.parent, echo_path.parent.parent,
                 user_unit, user_unit.parent, user_unit.parent.parent, user_config):
        os.chown(path, account.pw_uid, account.pw_gid)
    for name in ("noha-network.service", "noha-portal.service", "noha-admin.service", "noha-ngrok.service"):
        write(Path("/etc/systemd/system") / name, (ROOT / "systemd" / name).read_text())
    for name in ("noha-update.service", "noha-update.timer", "noha-update-recover.service", "noha-update-check.service",
                 "noha-update-boot-check.service", "noha-update-check.timer"):
        content = (ROOT / "systemd" / name).read_text().replace("@NOHA_UID@", str(account.pw_uid))
        write(Path("/etc/systemd/system") / name, content)
    write(DEST / "updater.py", (ROOT / "scripts/updater.py").read_text(), 0o644)
    write(Path("/usr/local/bin/noha-update"), '#!/bin/sh\nexec /usr/bin/python3 /opt/noha/updater.py "$@"\n', 0o755)
    launcher = '#!/bin/sh\nexec /opt/noha/current/venv/bin/noha "$@"\n'
    write(Path("/usr/local/bin/noha"), launcher, 0o755)
    write(manifest_path, json.dumps({"user": args.user, "hostname": hostname, "linger_before": linger_before,
                                   "echo_path": str(echo_path), "user_unit": str(user_unit)}, indent=2), 0o600)
    run("systemctl", "daemon-reload")
    user_run("systemctl", "--user", "daemon-reload")
    user_run("systemctl", "--user", "restart", "pipewire.service", "pipewire-pulse.service", "wireplumber.service")
    for _ in range(10):
        snapshot = json.loads(user_run("pw-dump", capture=True).stdout)
        names = {n.get("info", {}).get("props", {}).get("node.name") for n in snapshot}
        if {"noha_echo_source", "noha_echo_sink"}.issubset(names):
            print("Cancelamento de eco: fonte e saída PipeWire disponíveis.")
            break
        time.sleep(1)
    else:
        sys.exit("AEC não iniciou. Consulte journalctl --user -u pipewire; corrija antes de habilitar Noha.")
    user_run("systemctl", "--user", "enable", "noha.service")
    run("systemctl", "enable", "--now", "noha-network.service")
    run("systemctl", "restart", "noha-network.service")
    for name in ("noha-admin.service", "noha-portal.service", "noha-ngrok.service"):
        run("systemctl", "enable", "--now", name)
        run("systemctl", "restart", name)
    run("systemctl", "enable", "noha-update-recover.service")
    run("systemctl", "enable", "--now", "noha-update.timer")
    run("systemctl", "enable", "--now", "noha-update-check.timer")
    # New audio/gpio/noha group membership takes effect reliably after reboot.
    print("\nInstalado. Reinicie o Pi para aplicar grupos e iniciar Noha sem login.")
    print("API key: /etc/noha/secrets.env (não publique esse arquivo)")
    print("Depois de reiniciar: noha doctor; noha trigger; journalctl --user -u noha -f")
    print("Sem internet por 90s: conecte a Noha-Setup-XXXX e abra http://192.168.50.1")
    print("Backups de configuração: /var/lib/noha/backups")
    print("Atualizações automáticas: releases estáveis de github.com/lfdelphino/Noha-Concept")
    print(f"Painel: http://{hostname}.local — senha inicial admin (preservada se já alterada)")
    print("Configure o authtoken e hostname ngrok em Ajustes → Acesso ngrok para acesso pela internet.")


if __name__ == "__main__":
    main()

