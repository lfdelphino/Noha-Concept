"""Restricted root helper. Only the unprivileged web account can reach its socket."""
import asyncio
import contextlib
import datetime
import functools
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import time

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

from . import settings
from .audio import ECHO_CONFIG, echo_cancel_config, parse_nodes, parse_volume
from .config import Config, presence_enabled
from .diagnostics import checks
from .metrics import summary
from .models import GPIO_PINS, catalog
from .network import split_nm
from .passwords import password_record, verify_password

ETC = Path("/etc/noha")
STATE = Path("/var/lib/noha")
SOCKET = "/run/noha-admin/control.sock"
SERVICES = {"voice": "noha.service", "network": "noha-network.service", "web": "noha-portal.service",
            "admin": "noha-admin.service", "ngrok": "noha-ngrok.service", "updates": "noha-update.service"}
AUDIO_STACK = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")
AUDIO_KEYS = ("audio.microphone", "audio.speaker", "audio.echo_cancel")
DEFAULT_PASSWORD = "admin"


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def atomic_write(path, value, mode=0o600, backup=False):
    """Private, same-filesystem replacement with an explicit rollback copy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = STATE / "backups" / stamp / str(path).lstrip("/").replace(":", "")
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copy2(path, target)
    temp = path.with_name(path.name + ".admin-tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    if path.exists():
        stat = path.stat()
        os.chown(temp, stat.st_uid, stat.st_gid)
    temp.replace(path)


@contextlib.contextmanager
def update_lock():
    import fcntl
    with (STATE / "update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Atualização ou configuração em andamento. Tente novamente em alguns segundos.")
        yield


async def command(*args, timeout=15, check=True, env=None):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if check and process.returncode:
        raise ValueError("Operação do sistema falhou. Consulte os eventos do serviço.")
    return output.decode("utf-8", errors="replace").strip()


def account():
    import pwd
    return pwd.getpwnam(read_json(STATE / "install.json")["user"])


async def user_command(*args, check=True, timeout=15):
    user = account()
    env = {**os.environ, "HOME": user.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{user.pw_uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{user.pw_uid}/bus"}
    return await command("runuser", "-u", user.pw_name, "--", *args, check=check, env=env, timeout=timeout)


async def user_systemctl(*args, check=True):
    return await user_command("systemctl", "--user", *args, check=check)


async def audio_nodes():
    # PipeWire is per-user: list the voice account's graph, not root's.
    return parse_nodes(await user_command("pw-dump", timeout=10))


async def audio_devices():
    return [n for n in await audio_nodes() if settings.DEVICE_NAME.fullmatch(n["name"])
            and not n["name"].startswith("noha_") and n["class"] != "Audio/Device"]


def echo_path():
    return Path(account().pw_dir) / ".config" / ECHO_CONFIG


def write_user_file(path, text):
    # PipeWire reads this as the voice user; the admin umask (0077) must not leave root-only dirs.
    user = account()
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o755)
            os.chown(parent, user.pw_uid, user.pw_gid)
    atomic_write(path, text, mode=0o644, backup=True)
    os.chown(path, user.pw_uid, user.pw_gid)


def voice_runtime():
    return Path(f"/run/user/{account().pw_uid}/noha")


def voice_status():
    status = read_json(voice_runtime() / "status.json")
    status["stale"] = time.time() - status.get("updated", 0) > 15
    return status


def secret_key():
    path = ETC / "secrets.env"
    if path.exists():
        match = re.search(r"(?m)^OPENAI_API_KEY=(.*)$", path.read_text(encoding="utf-8"))
        return match.group(1).strip() if match else ""
    return ""


_default_password = {}


def default_password(record):
    """Whether the dashboard still uses the installer's password; cached per password revision."""
    revision = record.get("revision")
    if revision not in _default_password:
        _default_password.clear()
        _default_password[revision] = bool(record) and verify_password(DEFAULT_PASSWORD, record)
    return _default_password[revision]


def ngrok_config(token, url):
    # JSON is a YAML subset, avoiding interpolated YAML/command syntax entirely.
    return json.dumps({"version": 3, "agent": {"authtoken": token, "web_addr": "127.0.0.1:4040",
                       "inspect_db_size": -1, "console_ui": False, "log": "stdout", "log_format": "json"},
                       "endpoints": [{"name": "noha", "url": url,
                                      "upstream": {"url": "http://127.0.0.1:80"}}]})


async def validate_gpio(pin, current):
    output = await command("pinctrl", "get", str(pin))
    if re.search(r"\b(SCL\d*|SDA\d*|TXD\d*|RXD\d*|SPI\w*|PWM\w*|GPCLK\w*)\b", output):
        raise ValueError("GPIO ocupado por uma função de hardware; escolha outro pino")
    if pin != current:
        # Probe as the voice user; a busy line fails without changing boot overlays.
        user = account()
        try:
            await command("runuser", "-u", user.pw_name, "--", "/opt/noha/current/venv/bin/python", "-c",
                          "from noha.motion import open_sensor; import sys; s=open_sensor(int(sys.argv[1])); s.close()", str(pin))
        except ValueError:
            raise ValueError(f"Não foi possível abrir o GPIO BCM{pin}; confira se outro programa usa o pino") from None


def redact(message):
    for value in (secret_key(), read_json(ETC / "ngrok.json").get("authtoken", "")):
        if value:
            message = message.replace(value, "[oculto]")
    message = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|authtoken|password|senha)\s*[:=]\s*)[^\s,;]+", r"\1[oculto]", message)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[oculto]", message)


def set_presence(enabled):
    """Motion greetings on/off, read by the voice service within a second; survives reboots."""
    if type(enabled) is not bool:
        raise ValueError("Informe ligado ou desligado")
    atomic_write(ETC / "presence.json", {"enabled": enabled}, mode=0o644)
    return {"accepted": True, "enabled": enabled,
            "message": "Saudações por movimento ligadas" if enabled else "Saudações por movimento desligadas"}


def pending_view(pending):
    """Pending changes for the browser: secrets become a flag."""
    return {key: (True if key == "openai_key" else value) for key, value in pending.items()}


class Admin:
    def __init__(self):
        self.cpu = None
        self.routes = {
            ("GET", "auth"): self.auth, ("POST", "login"): self.login, ("POST", "kiosk"): self.kiosk,
            ("POST", "password"): self.password, ("GET", "status"): self.status,
            ("GET", "settings"): self.get_settings, ("POST", "settings"): self.save_settings,
            ("POST", "discard"): self.discard_pending, ("POST", "presence"): self.presence,
            ("POST", "trigger"): self.trigger, ("GET", "volume"): self.volume, ("POST", "volume"): self.set_volume,
            ("GET", "usage"): self.usage, ("GET", "logs"): self.logs, ("POST", "ngrok"): self.save_ngrok,
            ("POST", "check-update"): functools.partial(self.update, install=False),
            ("POST", "install-update"): functools.partial(self.update, install=True),
        }

    # Authentication ----------------------------------------------------------------------------

    async def auth(self, data):
        record = read_json(ETC / "dashboard-password.json")
        return {"revision": record.get("revision"), "default": await asyncio.to_thread(default_password, record)}

    async def login(self, data):
        record = read_json(ETC / "dashboard-password.json")
        if not await asyncio.to_thread(verify_password, data.get("password"), record):
            raise PermissionError("Senha inválida")
        return {"revision": record["revision"], "default": await asyncio.to_thread(default_password, record)}

    async def kiosk(self, data):
        # The local display's credential (scripts/kiosk.py); separate from the password.
        path = ETC / "kiosk-token"
        expected = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if not expected or not secrets.compare_digest(str(data.get("token", "")), expected):
            raise PermissionError("Token inválido")
        return {"revision": read_json(ETC / "dashboard-password.json").get("revision")}

    async def password(self, data):
        if not await asyncio.to_thread(verify_password, data.get("current"), read_json(ETC / "dashboard-password.json")):
            raise PermissionError("Senha atual inválida")
        new = data.get("password")
        if not isinstance(new, str) or len(new) < 6 or new == DEFAULT_PASSWORD:
            raise ValueError("Use uma senha com pelo menos 6 caracteres, diferente da senha padrão")
        atomic_write(ETC / "dashboard-password.json", await asyncio.to_thread(password_record, new), backup=True)
        return {"accepted": True, "message": "Senha alterada. Entre de novo com a nova senha."}

    # Status ------------------------------------------------------------------------------------

    async def network(self, config):
        try:
            async with ClientSession(connector=UnixConnector(path=config.network.socket),
                                     timeout=ClientTimeout(total=2)) as client:
                async with client.get("http://localhost/status") as response:
                    network = await response.json()
        except Exception:
            # A network controller failure must not take down the dashboard.
            network = {"mode": "indisponivel"}
        try:
            # The controller's "ssid" is the setup hotspot; report the network actually in use.
            rows = await command("nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no",
                                 timeout=3, check=False)
            network["wifi"] = next((fields[1] for fields in map(split_nm, rows.splitlines())
                                    if len(fields) == 2 and fields[0] == "yes"), "")
        except Exception:
            network["wifi"] = ""
        return network

    async def tunnel(self):
        ngrok = read_json(ETC / "ngrok.json")
        tunnel = {"url": ngrok.get("url", ""), "state": "configuracao_necessaria"}
        if ngrok.get("authtoken") and ngrok.get("url"):
            tunnel["state"] = "reconectando"
            try:
                async with ClientSession(timeout=ClientTimeout(total=1)) as client:
                    async with client.get("http://127.0.0.1:4040/api/tunnels") as response:
                        data = await response.json()
                        if any(t.get("public_url") == ngrok["url"] for t in data.get("tunnels", [])):
                            tunnel["state"] = "online"
            except Exception:
                pass
        return tunnel

    def system(self):
        counts = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        sample = (sum(counts[:8]), counts[3] + counts[4])
        cpu = None
        if self.cpu and sample[0] > self.cpu[0]:
            cpu = round(100 * (1 - (sample[1] - self.cpu[1]) / (sample[0] - self.cpu[0])), 1)
        self.cpu = sample
        memory = {k: int(v.split()[0]) * 1024 for k, v in
                  (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        disk = shutil.disk_usage("/var/lib")
        temperature = Path("/sys/class/thermal/thermal_zone0/temp")
        return {"cpu_percent": cpu, "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
                "temperature": float(temperature.read_text()) / 1000 if temperature.exists() else None,
                "memory_total": memory["MemTotal"], "memory_used": memory["MemTotal"] - memory["MemAvailable"],
                "disk_total": disk.total, "disk_used": disk.used, "hostname": socket.gethostname()}

    async def status(self, data=None):
        config = Config.load(ETC / "noha.toml")
        network, tunnel = await asyncio.gather(self.network(config), self.tunnel())
        today = await asyncio.to_thread(summary, 1)
        record = read_json(ETC / "dashboard-password.json")
        pending = read_json(STATE / "config-pending.json")
        result = {"voice": voice_status(), "network": network, "model": config.voice.model, "voice_name": config.voice.voice,
                  "system": self.system(), "ngrok": tunnel, "local_url": f"http://{socket.gethostname()}.local",
                  "pending": bool(pending), "pending_keys": sorted(pending), "apply": read_json(STATE / "config-result.json"),
                  "updates": read_json(STATE / "update-status.json"), "presence": presence_enabled(ETC / "presence.json"),
                  "key_configured": bool(secret_key()),
                  "budget": {"limit": config.voice.daily_budget_usd, "spent": today["totals"]["cost_usd"]},
                  "today": {"visits": today["totals"]["visits"], "conversations": today["outcomes"]["conversou"],
                            "greetings": today["totals"]["greetings"], "cost_usd": today["totals"]["cost_usd"],
                            "duration_seconds": today["totals"]["duration_seconds"],
                            "hourly": [bar["visits"] for bar in today["series"]]},
                  "security": {"default_password": await asyncio.to_thread(default_password, record)},
                  "auth_revision": record.get("revision")}
        result["checks"] = checks(result)
        return result

    async def logs(self, data):
        service = data.get("service", "voice")
        level = data.get("level", "info")
        if service not in SERVICES or level not in {"debug", "info", "warning", "err"}:
            raise ValueError("Filtro inválido")
        args = ["journalctl", "--no-pager", "-o", "json", "-n", "100", "--since", "24 hours ago", "-p", level]
        if service == "voice":
            args += [f"_UID={account().pw_uid}", "_SYSTEMD_USER_UNIT=noha.service"]
        else:
            args += ["-u", SERVICES[service]]
        output = await command(*args)
        rows = []
        for line in output.splitlines():
            try:
                entry = json.loads(line)
                rows.append({"id": entry.get("__CURSOR"), "at": entry.get("__REALTIME_TIMESTAMP"),
                             "priority": entry.get("PRIORITY"), "message": redact(str(entry.get("MESSAGE", "")))[:2000]})
            except ValueError:
                continue
        return {"entries": rows}

    async def usage(self, data):
        days = int(data.get("days", 1))
        if days not in {1, 7, 30}:
            raise ValueError("Período inválido")
        return await asyncio.to_thread(summary, days)

    # Live controls -----------------------------------------------------------------------------

    async def presence(self, data):
        return set_presence(data.get("enabled"))

    async def trigger(self, data):
        status = voice_status()
        if status["stale"]:
            raise ValueError("O serviço de voz não está respondendo")
        if status.get("busy"):
            raise ValueError("Noha já está atendendo alguém")
        (voice_runtime() / "trigger").touch(mode=0o644)
        return {"accepted": True, "message": "Noha vai cumprimentar agora, como se alguém tivesse chegado."}

    async def speaker_node(self):
        name = Config.load(ETC / "noha.toml").audio.speaker
        node = next((n for n in await audio_nodes() if n["name"] == name and n["class"] == "Audio/Sink"), None)
        if node is None or node.get("id") is None:
            raise ValueError("Alto-falante não encontrado")
        return str(node["id"])

    async def volume(self, data=None):
        level, muted = parse_volume(await user_command("wpctl", "get-volume", await self.speaker_node(), timeout=5))
        return {"level": level, "muted": muted}

    async def set_volume(self, data):
        level = data.get("level")
        if type(level) is not int or not 0 <= level <= 100:
            raise ValueError("Volume de 0 a 100")
        node = await self.speaker_node()
        await user_command("wpctl", "set-volume", node, f"{level / 100:.2f}", timeout=5)
        await user_command("wpctl", "set-mute", node, "0", timeout=5)
        return {"level": level, "muted": False}

    # Settings ----------------------------------------------------------------------------------

    async def get_settings(self, data=None):
        config = Config.load(ETC / "noha.toml")
        try:
            devices = await audio_devices()
        except Exception:
            devices = []  # PipeWire down: the page still shows the saved names
        ngrok = read_json(ETC / "ngrok.json")
        return {"values": settings.values(config, Path(config.voice.instructions).read_text(encoding="utf-8")),
                "schema": settings.schema(), "models": catalog(), "gpio_pins": GPIO_PINS, "devices": devices,
                "openai_key_configured": bool(secret_key()), "ngrok_url": ngrok.get("url", ""),
                "ngrok_token_configured": bool(ngrok.get("authtoken")),
                "local_url": f"http://{socket.gethostname()}.local",
                "pending": pending_view(read_json(STATE / "config-pending.json")),
                "apply": read_json(STATE / "config-result.json")}

    async def save_settings(self, data):
        config = Config.load(ETC / "noha.toml")
        changes = settings.validate(data.get("changes"), config)
        changed = [key for key in ("audio.microphone", "audio.speaker")
                   if key in changes and changes[key] != getattr(config.audio, key.partition(".")[2])]
        if changed:
            available = {(n["name"], n["class"]) for n in await audio_devices()}
            for key in changed:
                if (changes[key], settings.BY_KEY[key].source) not in available:
                    raise ValueError("Dispositivo de áudio indisponível; conecte-o e atualize a página")
        if "motion.gpio" in changes:
            await validate_gpio(changes["motion.gpio"], config.motion.gpio)
        with update_lock():
            pending = read_json(STATE / "config-pending.json")
            pending.update(changes)
            atomic_write(STATE / "config-pending.json", pending)
            atomic_write(STATE / "config-result.json", {"state": "pending", "message": "Aguardando Noha ficar livre"})
        return {"accepted": True, "pending": pending_view(pending),
                "message": "Salvo. Noha aplica assim que estiver livre e reinicia a voz em alguns segundos."}

    async def discard_pending(self, data=None):
        with update_lock():
            if (STATE / "config-transaction.json").exists():
                raise ValueError("As alterações já estão sendo aplicadas")
            (STATE / "config-pending.json").unlink(missing_ok=True)
            atomic_write(STATE / "config-result.json", {"state": "discarded", "message": "Alterações descartadas"})
        with contextlib.suppress(OSError, KeyError):
            (voice_runtime() / "config-pending").unlink(missing_ok=True)
        return {"accepted": True, "message": "Alterações pendentes descartadas"}

    async def apply_pending(self):
        data = read_json(STATE / "config-pending.json")
        if not data:
            return
        with update_lock():
            runtime = voice_runtime()
            marker = runtime / "config-pending"
            if not runtime.is_dir():
                return
            marker.touch(mode=0o644)  # the voice service starts no new visit meanwhile
            try:
                await asyncio.sleep(0.3)
                status = voice_status()
                if status.get("busy") and not status["stale"]:
                    return
                if status["stale"] and await user_systemctl("is-active", "noha.service", check=False) == "active":
                    return
                config_path = ETC / "noha.toml"
                current = Config.load(config_path)
                instructions_path = Path(current.voice.instructions)
                toml_changes = {key: value for key, value in data.items() if key not in settings.SPECIAL}
                audio_changed = any(key in toml_changes and toml_changes[key] != getattr(current.audio, key.partition(".")[2])
                                    for key in AUDIO_KEYS)
                echo = echo_path() if audio_changed else None
                targets = [config_path, instructions_path, ETC / "secrets.env", *([echo] if echo else [])]
                # A file that did not exist is recorded as None: recovery removes it again.
                originals = {str(p): {"text": p.read_text(encoding="utf-8"), "mode": p.stat().st_mode & 0o777}
                             if p.exists() else {"text": None, "mode": 0o644} for p in targets}
                atomic_write(STATE / "config-transaction.json", originals)
                text = settings.apply(originals[str(config_path)]["text"], toml_changes)
                atomic_write(config_path, text, mode=0o644, backup=True)
                new = Config.load(config_path)
                if echo:
                    if new.audio.hardware_aec():
                        echo.unlink(missing_ok=True)
                    else:
                        write_user_file(echo, echo_cancel_config(new.audio.microphone, new.audio.speaker))
                    await user_systemctl("restart", *AUDIO_STACK)
                if "personality" in data:
                    atomic_write(instructions_path, data["personality"], mode=0o644, backup=True)
                if data.get("openai_key"):
                    secrets_text = originals[str(ETC / "secrets.env")]["text"] or ""
                    secrets_text = re.sub(r"(?m)^OPENAI_API_KEY=.*\n?", "", secrets_text)
                    atomic_write(ETC / "secrets.env", secrets_text.rstrip() + "\nOPENAI_API_KEY=" + data["openai_key"] + "\n",
                                 mode=0o640, backup=True)
                started = time.time()
                await user_systemctl("reset-failed", "noha.service", check=False)
                await user_systemctl("restart", "noha.service")
                for _ in range(30):
                    state = voice_status()
                    # The restarted service reports the digest of the file it loaded.
                    if (not state["stale"] and state.get("updated", 0) >= started
                            and state.get("config_digest") == new.digest and (state.get("audio_ready") or not audio_changed)):
                        break
                    await asyncio.sleep(1)
                else:
                    raise ValueError("A nova configuração não iniciou; configuração anterior restaurada")
                (STATE / "config-transaction.json").unlink()
                (STATE / "config-pending.json").unlink()
                atomic_write(STATE / "config-result.json", {"state": "applied", "message": "Alterações aplicadas", "at": time.time()})
            finally:
                # Keep the gate while waiting for an active visit; remove it after success/failure.
                if not (STATE / "config-pending.json").exists():
                    marker.unlink(missing_ok=True)

    async def recover_config(self):
        transaction = read_json(STATE / "config-transaction.json")
        if not transaction:
            return
        with update_lock():
            for path, original in transaction.items():
                path = Path(path)
                if path.name == Path(ECHO_CONFIG).name:
                    if original["text"] is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_user_file(path, original["text"])
                elif original["text"] is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, original["text"], mode=original["mode"])
            if any(Path(path).name == Path(ECHO_CONFIG).name for path in transaction):
                await user_systemctl("restart", *AUDIO_STACK)
            await user_systemctl("restart", "noha.service")
            (STATE / "config-transaction.json").unlink()
            (STATE / "config-pending.json").unlink(missing_ok=True)
            (voice_runtime() / "config-pending").unlink(missing_ok=True)
            atomic_write(STATE / "config-result.json", {"state": "error", "message": "Configuração anterior restaurada após falha"})

    # Remote access and updates -----------------------------------------------------------------

    async def save_ngrok(self, data):
        old = read_json(ETC / "ngrok.json")
        token = data.get("authtoken") or old.get("authtoken", "")
        url = str(data.get("url", "")).strip().rstrip("/")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", token):
            raise ValueError("Informe o authtoken do agente ngrok")
        if not re.fullmatch(r"https://[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", url):
            raise ValueError("Informe somente o endereço HTTPS atribuído à sua conta ngrok")
        with update_lock():
            candidate = ETC / "ngrok-candidate.yml"
            try:
                atomic_write(candidate, ngrok_config(token, url), mode=0o600)
                await command("ngrok", "config", "check", "--config", str(candidate))
                atomic_write(ETC / "ngrok.json", {"authtoken": token, "url": url}, backup=True)
                atomic_write(ETC / "ngrok.yml", candidate.read_text(), mode=0o640, backup=True)
                import grp
                os.chown(ETC / "ngrok.yml", 0, grp.getgrnam("noha-ngrok").gr_gid)
                atomic_write(STATE / "ngrok-restart.json", {"at": time.time() + 3})
            finally:
                candidate.unlink(missing_ok=True)
        return {"accepted": True, "url": url, "message": "Acesso pela internet reconectando em 3 segundos."}

    async def update(self, data, install):
        if (STATE / "config-pending.json").exists():
            raise ValueError("Aguarde a aplicação das alterações pendentes")
        service = "noha-update.service" if install else "noha-update-check.service"
        await command("systemctl", "start", "--no-block", service)
        return {"accepted": True, "message": "Instalação solicitada" if install else "Verificação solicitada"}

    async def worker(self):
        while True:
            try:
                await self.recover_config()
                await self.apply_pending()
                restart = read_json(STATE / "ngrok-restart.json")
                if restart and time.time() >= restart["at"]:
                    with update_lock():
                        await command("systemctl", "restart", "noha-ngrok.service")
                        (STATE / "ngrok-restart.json").unlink()
            except asyncio.CancelledError:
                raise
            except Exception:
                if (STATE / "config-transaction.json").exists():
                    with contextlib.suppress(Exception):
                        await self.recover_config()
            await asyncio.sleep(2)

    async def dispatch(self, request):
        operation = request.match_info["operation"]
        handler = self.routes.get((request.method, operation))
        if handler is None:
            return web.json_response({"error": "Operação desconhecida"}, status=404)
        try:
            data = await request.json() if request.method == "POST" else dict(request.query)
            if not isinstance(data, dict):
                raise ValueError("Dados inválidos")
            return web.json_response(await handler(data))
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=401 if operation != "password" else 403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (TypeError, KeyError):
            return web.json_response({"error": "Confira os dados e o estado dos serviços."}, status=400)
        except (OSError, TimeoutError):
            return web.json_response({"error": "Serviço indisponível. Tente novamente."}, status=503)


async def serve():
    admin = Admin()
    app = web.Application(client_max_size=128 * 1024)
    app.router.add_route("*", "/{operation}", admin.dispatch)

    async def background(app):
        task = asyncio.create_task(admin.worker())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    app.cleanup_ctx.append(background)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    Path(SOCKET).unlink(missing_ok=True)
    try:
        await web.UnixSite(runner, SOCKET).start()
        os.chmod(SOCKET, 0o660)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
