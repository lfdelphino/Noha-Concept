#!/usr/bin/env python3
"""Standalone, stdlib-only release updater; runs independently of the app venv."""
import argparse
import contextlib
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile

ROOT = Path("/opt/noha")
STATE = Path("/var/lib/noha")
CONFIG = Path("/etc/noha/updates.toml")
REPO = "lfdelphino/Noha-Concept"
log = logging.getLogger("noha.update")


def version(tag):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag)
    if not match:
        raise ValueError("Use versões estáveis no formato v1.2.3")
    return tuple(map(int, match.groups()))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def switch(target):
    target = target.resolve()
    target.relative_to((ROOT / "releases").resolve())
    if not (target / "venv/bin/noha").is_file():
        raise RuntimeError("Versão incompleta")
    temporary = ROOT / "current.next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(ROOT / "current")
    # Persist the atomic pointer before considering a transaction complete.
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def request(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Noha-Updater/1",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Download excedeu o limite")
    return payload


def latest():
    try:
        release = json.loads(request(f"https://api.github.com/repos/{REPO}/releases/latest", 2_000_000))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if release.get("draft") or release.get("prerelease"):
        return None
    version(release["tag_name"])
    asset = next((a for a in release.get("assets", []) if a["name"] == "noha-pi.zip"), None)
    if not asset or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
        raise ValueError("Release sem noha-pi.zip ou SHA-256 do GitHub")
    prefix = f"https://github.com/{REPO}/releases/download/"
    if not asset["browser_download_url"].startswith(prefix):
        raise ValueError("Asset fora do repositório configurado")
    return {"tag": release["tag_name"], "id": int(release["id"]), "asset_id": int(asset["id"]),
            "digest": asset["digest"][7:], "url": asset["browser_download_url"]}


def extract(archive, destination):
    """Reject links, traversal, duplicates, non-project roots and oversized zip bombs."""
    with zipfile.ZipFile(archive) as bundle:
        total, seen = 0, set()
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if (not path.parts or path.parts[0] != "noha-pi" or ".." in path.parts
                    or "\\" in entry.filename or path.is_absolute() or entry.filename in seen
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Caminho inválido no pacote")
            seen.add(entry.filename)
            total += entry.file_size
            if total > 50_000_000 or len(seen) > 2000:
                raise ValueError("Pacote expandido excedeu o limite")
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            relative = path.parts[1:]
            if not relative:
                continue
            target = destination.joinpath(*relative)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)


def command(*args, check=True, timeout=60, env=None):
    # Installer/build output is not a health signal and can contain arbitrary project text.
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=timeout, env=env)
    if check and result.returncode:
        raise RuntimeError("Comando de atualização falhou")
    return result.returncode == 0


def account():
    import pwd
    manifest = read_json(STATE / "install.json")
    if not manifest:
        raise RuntimeError("Reexecute o instalador para habilitar atualizações")
    return pwd.getpwnam(manifest["user"])


def user_command(user, *args, check=True):
    env = {**os.environ, "HOME": user.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{user.pw_uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{user.pw_uid}/bus"}
    return command("runuser", "-u", user.pw_name, "--", "systemctl", "--user", *args, check=check, env=env)


def paths(user):
    return {"noha.service": Path(user.pw_dir) / ".config/systemd/user/noha.service",
            "noha-network.service": Path("/etc/systemd/system/noha-network.service"),
            "noha-portal.service": Path("/etc/systemd/system/noha-portal.service")}


def maintenance(user):
    return Path(f"/run/user/{user.pw_uid}/noha/update-pending")


def network_status():
    config = tomllib.loads(Path("/etc/noha/noha.toml").read_text(encoding="utf-8"))
    path = config.get("network", {}).get("socket", "/run/noha-network/control.sock")
    connection = http.client.HTTPConnection("localhost", timeout=3)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(3)
        connection.sock.connect(path)
        connection.request("GET", "/status")
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Estado da rede indisponível")
        return json.loads(response.read(4096))
    finally:
        connection.close()


def voice_status(user):
    path = Path(f"/run/user/{user.pw_uid}/noha/status.json")
    status = read_json(path, {})
    if time.time() - status.get("updated", 0) > 15:
        raise RuntimeError("Heartbeat de voz ausente")
    os.kill(int(status["pid"]), 0)
    return status


def wait_idle(user, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            voice, network = voice_status(user), network_status()
            if voice.get("ready") and not voice.get("busy") and network.get("mode") == "online" and not network.get("busy"):
                return True
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(2)
    return False


def health(user, tag, seconds):
    deadline = time.monotonic() + seconds
    stable_since = None
    while time.monotonic() < deadline:
        try:
            voice, network = voice_status(user), network_status()
            okay = (voice.get("ready") and voice.get("version") == tag.lstrip("v")
                    and bool(network.get("mode"))
                    and user_command(user, "is-active", "--quiet", "noha.service", check=False)
                    and command("systemctl", "is-active", "--quiet", "noha-network.service", check=False))
            stable_since = (stable_since or time.monotonic()) if okay else None
            if stable_since and time.monotonic() - stable_since >= 15:
                return True
        except (OSError, ValueError, RuntimeError):
            stable_since = None
        time.sleep(2)
    return False


@contextlib.contextmanager
def readable_deployment():
    # Code and dependencies are public; system/user services must traverse them.
    # The updater service otherwise uses 0077 for its private state/journal.
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def stage(release):
    with readable_deployment():
        return _stage(release)


def _stage(release):
    target = ROOT / "releases" / f"{release['tag']}-{release['asset_id']}"
    marker = target / "prepared.json"
    if read_json(marker, {}).get("digest") == release["digest"]:
        return target
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)
    archive = target / "download.zip"
    payload = request(release["url"], 20_000_000)
    if hashlib.sha256(payload).hexdigest() != release["digest"]:
        raise ValueError("SHA-256 do pacote não confere")
    archive.write_bytes(payload)
    source = target / "source"
    # A previous interrupted extraction must not leave stale modules in the next build.
    if source.exists():
        source.resolve().relative_to((ROOT / "releases").resolve())
        shutil.rmtree(source)
    source.mkdir()
    extract(archive, source)
    manifest = read_json(source / "release.json", {})
    package = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    if (manifest.get("format") != 1 or manifest.get("requires_installer", False)
            or version(manifest.get("version", "")) != version(release["tag"])
            or version(package["project"]["version"]) != version(release["tag"])):
        raise ValueError("Pacote incompatível: confira a versão ou reexecute o instalador")
    for name in paths_for_bundle():
        if not (source / name).is_file():
            raise ValueError("Pacote incompleto")
    atomic_json(target / "release.json", manifest)
    command("python3", "-m", "venv", "--clear", "--system-site-packages", str(target / "venv"), timeout=120)
    (target / "venv").chmod(0o755)
    python = str(target / "venv/bin/python")
    command(python, "-m", "pip", "install", "--disable-pip-version-check", str(source), timeout=600)
    command(python, "-c", "from noha.config import Config; from noha.portal import make_app; "
            "from noha.realtime import Playback; Config.load('/etc/noha/noha.toml')", timeout=30)
    atomic_json(marker, release)
    archive.unlink()
    return target


def paths_for_bundle():
    return ("systemd/noha.service", "systemd/noha-network.service", "systemd/noha-portal.service")


def restore_units(user, saved):
    for name, path in paths(user).items():
        temporary = path.with_name(path.name + ".update-tmp")
        temporary.write_text(saved[name], encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(path)
    command("systemctl", "daemon-reload")
    user_command(user, "daemon-reload", check=False)


def recover(start_services=True):
    path = STATE / "update-transaction.json"
    transaction = read_json(path)
    if not transaction:
        return False
    user = account()
    if start_services:
        user_command(user, "stop", "noha.service", check=False)
        command("systemctl", "stop", "noha-network.service", check=False)
    switch(Path(transaction["previous"]))
    restore_units(user, transaction["units"])
    state = transaction.get("state", {})
    state["rejected"] = transaction["release"]
    state["result"] = "rollback"
    atomic_json(STATE / "updates.json", state)
    if start_services:
        command("systemctl", "reset-failed", "noha-network.service", "noha-portal.service", check=False)
        user_command(user, "reset-failed", "noha.service", check=False)
        command("systemctl", "restart", "noha-network.service")
        user_command(user, "restart", "noha.service")
    path.unlink()
    maintenance(user).unlink(missing_ok=True)
    log.warning("Versão anterior restaurada")
    return True


def apply(target, release, settings):
    user = account()
    pending = maintenance(user)
    if not pending.parent.is_dir():
        raise RuntimeError("Serviço de voz ainda não iniciou")
    pending.touch(mode=0o644)
    try:
        if not wait_idle(user, settings.get("idle_wait_seconds", 300)):
            log.info("Atualização adiada: conversa, configuração ou hardware indisponível")
            return False
        previous = (ROOT / "current").resolve(strict=True)
        old_units = {name: path.read_text(encoding="utf-8") for name, path in paths(user).items()}
        atomic_json(STATE / "update-transaction.json", {
            "previous": str(previous), "candidate": str(target), "units": old_units, "release": release,
            "state": read_json(STATE / "updates.json", {})})
        try:
            user_command(user, "stop", "noha.service")
            command("systemctl", "stop", "noha-network.service")
            switch(target)
            new_units = {name: (target / "source/systemd" / name).read_text(encoding="utf-8")
                         for name in paths(user)}
            restore_units(user, new_units)
            command("systemctl", "reset-failed", "noha-network.service", "noha-portal.service", check=False)
            user_command(user, "reset-failed", "noha.service", check=False)
            command("systemctl", "start", "noha-network.service")
            user_command(user, "start", "noha.service")
            if not health(user, release["tag"], settings.get("health_seconds", 90)):
                raise RuntimeError("A nova versão não passou na verificação de inicialização")
            state = {"current": release, "previous": str(previous), "result": "updated"}
            atomic_json(STATE / "updates.json", state)
            (STATE / "update-transaction.json").unlink()
            log.info("Atualização aplicada: %s", release["tag"])
            return True
        except Exception:
            recover()
            raise
    finally:
        pending.unlink(missing_ok=True)


def run(check_only=False, retry=False):
    settings = tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("updates", {})
    if settings.get("repository", REPO) != REPO:
        raise ValueError("Repositório inesperado")
    if not settings.get("enabled", True) and not check_only:
        log.info("Atualizações desativadas")
        return
    release = latest()
    if not release:
        log.info("Nenhuma release publicada; mantendo versão atual")
        return
    installed = read_json(ROOT / "current/release.json", {})
    state = read_json(STATE / "updates.json", {})
    log.info("Instalada=%s disponível=%s", installed.get("version", "?"), release["tag"])
    if check_only or version(release["tag"]) <= version(installed.get("version", "0.0.0")):
        return
    if state.get("rejected") == release and not retry:
        log.warning("Release anteriormente rejeitada; publique uma nova versão ou use --retry")
        return
    apply(stage(release), release, settings)


def main():
    parser = argparse.ArgumentParser(description="Atualizações Noha por releases públicas do GitHub")
    parser.add_argument("--check", action="store_true", help="Consulta a versão sem instalar")
    parser.add_argument("--retry", action="store_true", help="Tenta novamente uma release que falhou")
    parser.add_argument("--recover", action="store_true", help="Restaura transação interrompida no boot")
    parser.add_argument("--rollback", action="store_true", help="Restaura a versão anterior manualmente")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        sys.exit("Execute sudo noha-update")
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Outra atualização já está em andamento")
            return
        try:
            if args.recover:
                recover(start_services=False)
            elif recover():
                return
            elif args.rollback:
                state = read_json(STATE / "updates.json", {})
                if not state.get("previous"):
                    raise RuntimeError("Não há versão anterior registrada")
                user = account()
                previous = Path(state["previous"])
                # Reuse the same journaled, idle-aware activation and health checks.
                release = read_json(previous / "release.json")
                current_release = state.get("current")
                if apply(previous, {"tag": release["version"]}, {"idle_wait_seconds": 300, "health_seconds": 90}):
                    state = read_json(STATE / "updates.json", {})
                    state["rejected"] = current_release
                    state["result"] = "manual_rollback"
                    atomic_json(STATE / "updates.json", state)
            else:
                run(args.check, args.retry)
        except Exception as exc:
            log.error("Atualização não aplicada (%s). Consulte a configuração e a conectividade.", type(exc).__name__)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
