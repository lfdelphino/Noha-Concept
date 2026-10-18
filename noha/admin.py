"""Restricted root helper. Only the unprivileged web account can reach its socket."""
import asyncio
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import socket
import time
import tomllib

from aiohttp import ClientSession, ClientTimeout, UnixConnector, web

from .config import Config
from .metrics import summary
from .models import GPIO_PINS, catalog, model_info
from .passwords import password_record, verify_password

ETC = Path("/etc/noha")
STATE = Path("/var/lib/noha")
SOCKET = "/run/noha-admin/control.sock"
SERVICES = {"voice": "noha.service", "network": "noha-network.service", "web": "noha-portal.service",
            "admin": "noha-admin.service", "ngrok": "noha-ngrok.service", "updates": "noha-update.service"}


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
            raise ValueError("Atualização ou configuração em andamento. Tente novamente em breve.")
        yield


async def command(*args, timeout=15, check=True, env=None):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if check and process.returncode:
        raise ValueError("Operação do sistema falhou. Consulte o estado do serviço.")
    return output.decode("utf-8", errors="replace").strip()


def account():
    import pwd
    return pwd.getpwnam(read_json(STATE / "install.json")["user"])


async def user_systemctl(*args, check=True):
    user = account()
    env = {**os.environ, "HOME": user.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{user.pw_uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{user.pw_uid}/bus"}
    return await command("runuser", "-u", user.pw_name, "--", "systemctl", "--user", *args, check=check, env=env)


def voice_status():
    status = read_json(voice_runtime() / "status.json")
    status["stale"] = time.time() - status.get("updated", 0) > 15
    return status


def voice_runtime():
    return Path(f"/run/user/{account().pw_uid}/noha")


def get_settings():
    config = Config.load(ETC / "noha.toml")
    ngrok = read_json(ETC / "ngrok.json")
    pending = read_json(STATE / "config-pending.json")
    return {"model": config.voice.model, "voice": config.voice.voice, "gpio": config.motion.gpio,
            "instructions": Path(config.voice.instructions).read_text(encoding="utf-8"),
            "openai_key_configured": bool(secret_key()), "ngrok_token_configured": bool(ngrok.get("authtoken")),
            "ngrok_url": ngrok.get("url", ""), "local_url": f"http://{socket.gethostname()}.local",
            "models": catalog(), "gpio_pins": GPIO_PINS,
            "pending": {k: v for k, v in pending.items() if k != "openai_key"},
            "apply": read_json(STATE / "config-result.json")}


def secret_key():
    path = ETC / "secrets.env"
    if path.exists():
        match = re.search(r"(?m)^OPENAI_API_KEY=(.*)$", path.read_text(encoding="utf-8"))
        return match.group(1).strip() if match else ""
    return ""


def edit_toml(text, section, key, value):
    replacement = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    pattern = rf"(?ms)(^\[{re.escape(section)}\]\s*\n)(.*?)(?=^\[|\Z)"
    def replace(match):
        body = match.group(2)
        line = rf"(?m)^{re.escape(key)}\s*=.*$"
        body = re.sub(line, lambda _: replacement, body) if re.search(line, body) else body + replacement + "\n"
        return match.group(1) + body
    return re.sub(pattern, replace, text)


def ngrok_config(token, url):
    # JSON is a YAML subset, avoiding interpolated YAML/command syntax entirely.
    return json.dumps({"version": 3, "agent": {"authtoken": token, "web_addr": "127.0.0.1:4040",
                       "inspect_db_size": -1, "console_ui": False, "log": "stdout", "log_format": "json"},
                       "endpoints": [{"name": "noha", "url": url,
                                      "upstream": {"url": "http://127.0.0.1:80"}}]})


async def validate_gpio(pin):
    if type(pin) is not int or pin not in GPIO_PINS:
        raise ValueError("GPIO BCM inválido")
    config = Config.load(ETC / "noha.toml")
    output = await command("pinctrl", "get", str(pin))
    if re.search(r"\b(SCL\d*|SDA\d*|TXD\d*|RXD\d*|SPI\w*|PWM\w*|GPCLK\w*)\b", output):
        raise ValueError("GPIO ocupado por uma função de hardware; escolha outro pino")
    if pin != config.motion.gpio:
        # Probe as the voice user; a busy line fails without changing boot overlays.
        user = account()
        await command("runuser", "-u", user.pw_name, "--", "/opt/noha/current/venv/bin/python", "-c",
                      "from noha.motion import open_sensor; import sys; s=open_sensor(int(sys.argv[1])); s.close()", str(pin))


def redact(message):
    for value in (secret_key(), read_json(ETC / "ngrok.json").get("authtoken", "")):
        if value:
            message = message.replace(value, "[oculto]")
    message = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|authtoken|password|senha)\s*[:=]\s*)[^\s,;]+", r"\1[oculto]", message)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[oculto]", message)


class Admin:
    def __init__(self):
        self.cpu = None

    async def status(self):
        config = Config.load(ETC / "noha.toml")
        network = {"mode": "indisponivel"}
        try:
            async with ClientSession(connector=UnixConnector(path=config.network.socket),
                                     timeout=ClientTimeout(total=2)) as client:
                async with client.get("http://localhost/status") as response:
                    network = await response.json()
        except Exception:
            # A network controller failure must not take down the dashboard.
            network = {"mode": "indisponivel"}
        ngrok = read_json(ETC / "ngrok.json")
        tunnel = {"url": ngrok.get("url", ""), "state": "configuracao_necessaria"}
        if ngrok.get("authtoken") and ngrok.get("url"):
            tunnel["state"] = "reconectando"
            try:
                async with ClientSession(timeout=ClientTimeout(total=1)) as client:
                    async with client.get("http://127.0.0.1:4040/api/tunnels") as response:
                        data = await response.json()
                        tunnel["state"] = "online" if any(t.get("public_url") == ngrok["url"] for t in data.get("tunnels", [])) else "reconectando"
            except Exception:
                pass
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
        pending = read_json(STATE / "config-pending.json")
        return {"voice": voice_status(), "network": network, "model": config.voice.model,
                "system": {"cpu_percent": cpu, "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
                           "temperature": float(temperature.read_text()) / 1000 if temperature.exists() else None,
                           "memory_total": memory["MemTotal"], "memory_used": memory["MemTotal"] - memory["MemAvailable"],
                           "disk_total": disk.total, "disk_used": disk.used},
                "ngrok": tunnel, "local_url": f"http://{socket.gethostname()}.local",
                "pending": bool(pending), "apply": read_json(STATE / "config-result.json"),
                "updates": read_json(STATE / "update-status.json"),
                "auth_revision": read_json(ETC / "dashboard-password.json").get("revision")}

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
                             "message": redact(str(entry.get("MESSAGE", "")))[:2000]})
            except ValueError:
                continue
        return {"entries": rows}

    async def save_settings(self, data):
        if set(data) - {"model", "voice", "gpio", "instructions", "openai_key"}:
            raise ValueError("Campo desconhecido")
        model = model_info(data.get("model"))
        if data.get("voice") not in model.voices:
            raise ValueError("Voz incompatível com o modelo")
        instructions = data.get("instructions")
        if not isinstance(instructions, str) or not 1 <= len(instructions.encode()) <= 60000:
            raise ValueError("Instruções devem ter de 1 a 60000 bytes")
        key = data.get("openai_key", "")
        if not isinstance(key, str) or (key and not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", key)):
            raise ValueError("Formato da chave OpenAI inválido")
        await validate_gpio(data.get("gpio"))
        with update_lock():
            if (STATE / "config-pending.json").exists():
                raise ValueError("Uma configuração já está aguardando aplicação")
            atomic_write(STATE / "config-pending.json", data)
            atomic_write(STATE / "config-result.json", {"state": "pending", "message": "Aguardando o fim da conversa"})
        return {"accepted": True, "message": "Configuração salva. Será aplicada após a conversa atual."}

    async def save_ngrok(self, data):
        if set(data) - {"authtoken", "url"}:
            raise ValueError("Campo desconhecido")
        old = read_json(ETC / "ngrok.json")
        token = data.get("authtoken") or old.get("authtoken", "")
        url = data.get("url", "").strip().rstrip("/")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", token):
            raise ValueError("Informe o authtoken do agente ngrok")
        if not re.fullmatch(r"https://[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", url):
            raise ValueError("Informe somente o hostname HTTPS atribuído à sua conta ngrok")
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
        return {"accepted": True, "url": url, "message": "ngrok será reconectado em 3 segundos. Abra o endereço salvo."}

    async def apply_pending(self):
        data = read_json(STATE / "config-pending.json")
        if not data:
            return
        with update_lock():
            runtime = voice_runtime()
            marker = runtime / "config-pending"
            if not runtime.is_dir():
                return
            marker.touch(mode=0o644)
            try:
                await asyncio.sleep(0.3)
                status = voice_status()
                if status.get("busy") and not status["stale"]:
                    return
                if status["stale"] and await user_systemctl("is-active", "noha.service", check=False) == "active":
                    return
                config_path = ETC / "noha.toml"
                cfg = Config.load(config_path)
                instructions_path = Path(cfg.voice.instructions)
                targets = [config_path, instructions_path, ETC / "secrets.env"]
                originals = {str(p): {"text": p.read_text(encoding="utf-8"), "mode": p.stat().st_mode & 0o777} for p in targets}
                atomic_write(STATE / "config-transaction.json", originals)
                text = originals[str(config_path)]["text"]
                for section, key, value in (("voice", "model", data["model"]), ("voice", "voice", data["voice"]),
                                            ("motion", "gpio", data["gpio"])):
                    text = edit_toml(text, section, key, value)
                atomic_write(config_path, text, mode=0o644, backup=True)
                Config.load(config_path)
                atomic_write(instructions_path, data["instructions"], mode=0o644, backup=True)
                if data.get("openai_key"):
                    secrets_text = originals[str(ETC / "secrets.env")]["text"]
                    secrets_text = re.sub(r"(?m)^OPENAI_API_KEY=.*\n?", "", secrets_text)
                    atomic_write(ETC / "secrets.env", secrets_text.rstrip() + "\nOPENAI_API_KEY=" + data["openai_key"] + "\n", mode=0o640, backup=True)
                started = time.time()
                await user_systemctl("reset-failed", "noha.service", check=False)
                await user_systemctl("restart", "noha.service")
                for _ in range(30):
                    state = voice_status()
                    if not state["stale"] and state.get("updated", 0) >= started and state.get("model") == data["model"] and state.get("gpio") == data["gpio"]:
                        break
                    await asyncio.sleep(1)
                else:
                    raise ValueError("A nova configuração não iniciou; configuração anterior restaurada")
                (STATE / "config-transaction.json").unlink()
                (STATE / "config-pending.json").unlink()
                atomic_write(STATE / "config-result.json", {"state": "applied", "message": "Configuração aplicada", "at": time.time()})
            finally:
                # Keep the gate while waiting for an active session; remove it after success/failure.
                if not (STATE / "config-pending.json").exists():
                    marker.unlink(missing_ok=True)

    async def recover_config(self):
        transaction = read_json(STATE / "config-transaction.json")
        if not transaction:
            return
        with update_lock():
            for path, original in transaction.items():
                atomic_write(Path(path), original["text"], mode=original["mode"])
            await user_systemctl("restart", "noha.service")
            (STATE / "config-transaction.json").unlink()
            (STATE / "config-pending.json").unlink(missing_ok=True)
            (voice_runtime() / "config-pending").unlink(missing_ok=True)
            atomic_write(STATE / "config-result.json", {"state": "error", "message": "Configuração anterior restaurada após falha"})

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
                    try:
                        await self.recover_config()
                    except Exception:
                        pass
            await asyncio.sleep(2)

    async def dispatch(self, request):
        operation = request.match_info["operation"]
        try:
            data = await request.json() if request.method == "POST" else dict(request.query)
            if not isinstance(data, dict):
                raise ValueError("Dados inválidos")
            if operation == "login" and request.method == "POST":
                record = read_json(ETC / "dashboard-password.json")
                if not await asyncio.to_thread(verify_password, data.get("password"), record):
                    return web.json_response({"error": "Senha inválida"}, status=401)
                result = {"revision": record["revision"]}
            elif operation == "password" and request.method == "POST":
                if not await asyncio.to_thread(verify_password, data.get("current"), read_json(ETC / "dashboard-password.json")):
                    return web.json_response({"error": "Senha atual inválida"}, status=403)
                atomic_write(ETC / "dashboard-password.json", await asyncio.to_thread(password_record, data.get("password")), backup=True)
                result = {"accepted": True}
            elif operation == "auth" and request.method == "GET":
                result = {"revision": read_json(ETC / "dashboard-password.json").get("revision")}
            elif operation == "status" and request.method == "GET":
                result = await self.status()
            elif operation == "settings" and request.method == "GET":
                result = get_settings()
            elif operation == "settings" and request.method == "POST":
                result = await self.save_settings(data)
            elif operation == "ngrok" and request.method == "POST":
                result = await self.save_ngrok(data)
            elif operation == "usage" and request.method == "GET":
                days = int(data.get("days", 1))
                if days not in {1, 7, 30}:
                    raise ValueError("Período inválido")
                result = await asyncio.to_thread(summary, days)
            elif operation == "logs" and request.method == "GET":
                result = await self.logs(data)
            elif operation in {"check-update", "install-update"} and request.method == "POST":
                if (STATE / "config-pending.json").exists():
                    raise ValueError("Aguarde a aplicação da configuração")
                service = "noha-update-check.service" if operation == "check-update" else "noha-update.service"
                await command("systemctl", "start", "--no-block", service)
                result = {"accepted": True, "message": "Operação de atualização solicitada"}
            else:
                raise web.HTTPNotFound()
            return web.json_response(result)
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
