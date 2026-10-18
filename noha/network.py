"""Privileged NetworkManager adapter. No shell commands, passwords or raw NM errors logged."""
import asyncio
import hashlib
import os
from pathlib import Path
import uuid


class NetworkError(RuntimeError):
    pass


async def command(*args, timeout=20, check=True, input_text=None):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.PIPE,
                                              stdin=asyncio.subprocess.PIPE if input_text is not None else None)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(input_text.encode() if input_text is not None else None), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if check and proc.returncode:
        raise NetworkError("Operação de rede falhou")
    return stdout.decode("utf-8", errors="replace").strip()


def split_nm(line):
    fields, buf, escaped = [], [], False
    for char in line:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    fields.append("".join(buf))
    return fields


def validate_credentials(data):
    ssid, password = data.get("ssid", ""), data.get("password", "")
    security = data.get("security", "wpa-psk")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError("Informe um SSID válido de até 32 bytes")
    if not isinstance(password, str) or any(ord(c) < 32 for c in password):
        raise ValueError("Senha inválida")
    if security not in {"open", "wpa-psk", "sae"}:
        raise ValueError("Rede empresarial não é suportada")
    if security == "wpa-psk":
        is_hex = len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)
        if not is_hex and not 8 <= len(password.encode("utf-8")) <= 63:
            raise ValueError("WPA/WPA2: use uma senha de 8 a 63 bytes")
    if security == "sae" and not 1 <= len(password.encode("utf-8")) <= 63:
        raise ValueError("WPA3: informe a senha")
    if not isinstance(data.get("hidden", False), bool):
        raise ValueError("Opção de rede oculta inválida")
    return {"ssid": ssid, "password": password if security != "open" else "",
            "security": security, "hidden": data.get("hidden", False)}


class NetworkManager:
    HOTSPOT_ID = "noha-setup"

    def __init__(self, config):
        self.config = config
        self.dns = None
        self.profile_dir = Path("/etc/NetworkManager/system-connections")
        self.hotspot_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "noha.local.setup"))
        machine = Path("/etc/machine-id").read_text().strip()
        self.ssid = config.hotspot_ssid or "Noha-Setup-" + hashlib.sha256(machine.encode()).hexdigest()[:4].upper()

    async def nm(self, *args, **kwargs):
        return await command("nmcli", "--colors", "no", *args, **kwargs)

    async def scan(self):
        raw = await self.nm("-t", "-e", "yes", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                            "ifname", self.config.interface, "--rescan", "yes", timeout=20)
        found = {}
        for line in raw.splitlines():
            fields = split_nm(line)
            if len(fields) != 3 or not fields[0]:
                continue
            ssid, signal, security = fields
            unsupported = "802.1X" in security or "WEP" in security
            mode = "enterprise" if unsupported else (
                "open" if security in {"", "--"} else "sae" if "WPA3" in security and "WPA2" not in security else "wpa-psk")
            item = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
                    "security": mode}
            if ssid not in found or item["signal"] > found[ssid]["signal"]:
                found[ssid] = item
        return sorted(found.values(), key=lambda n: n["signal"], reverse=True)

    async def _profile(self, ident, connection_uuid, settings):
        from gi.repository import GLib
        keyfile = GLib.KeyFile()
        sections = {
            "connection": {"id": ident, "uuid": connection_uuid, "type": "wifi",
                           "interface-name": self.config.interface}, **settings}
        for section, values in sections.items():
            for name, value in values.items():
                if isinstance(value, bool):
                    keyfile.set_boolean(section, name, value)
                elif isinstance(value, int):
                    keyfile.set_integer(section, name, value)
                else:
                    keyfile.set_string(section, name, value)
        keyfile.set_boolean("connection", "autoconnect", ident != self.HOTSPOT_ID)
        path = self.profile_dir / f"noha-{connection_uuid}.nmconnection"
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(keyfile.to_data()[0])
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            await self.nm("connection", "load", str(path))
        except BaseException:
            temp.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        return connection_uuid

    async def add_wifi(self, credentials):
        ident = str(uuid.uuid4())
        settings = {"wifi": {"mode": "infrastructure", "ssid": credentials["ssid"],
                             "hidden": credentials["hidden"]},
                    "ipv4": {"method": "auto"}, "ipv6": {"method": "auto"}}
        if credentials["security"] != "open":
            settings["wifi-security"] = {"key-mgmt": credentials["security"], "psk": credentials["password"]}
        return await self._profile("noha-wifi-" + ident[:8], ident, settings)

    async def activate(self, ident):
        await self.nm("--wait", str(int(self.config.connect_seconds)), "connection", "up", "uuid", ident,
                      "ifname", self.config.interface, timeout=self.config.connect_seconds + 3)

    async def remove(self, ident):
        await self.nm("connection", "delete", "uuid", ident, check=False)
        (self.profile_dir / f"noha-{ident}.nmconnection").unlink(missing_ok=True)

    async def saved(self):
        raw = await self.nm("-t", "-f", "UUID,TYPE", "connection", "show")
        return [p[0] for line in raw.splitlines() if len(p := split_nm(line)) == 2
                and p[1] == "802-11-wireless" and p[0] != self.hotspot_uuid]

    async def associated(self, ident):
        raw = await self.nm("-t", "-f", "UUID", "connection", "show", "--active")
        return ident in raw.splitlines()

    async def hotspot_healthy(self):
        return bool(self.dns and self.dns.returncode is None
                    and await self.associated(self.hotspot_uuid))

    async def hotspot_up(self):
        await self._profile(self.HOTSPOT_ID, self.hotspot_uuid, {
            "wifi": {"mode": "ap", "band": "bg", "ssid": self.ssid},
            "ipv4": {"method": "manual", "address1": f"{self.config.gateway}/24", "never-default": True},
            "ipv6": {"method": "disabled"}})
        await self.activate(self.hotspot_uuid)
        # Only the setup services are reachable through the open AP. No forwarding.
        await command("nft", "delete", "table", "inet", "noha_setup", check=False)
        interface = self.config.interface
        await command("nft", "-f", "-", input_text=f'''table inet noha_setup {{
 chain input {{ type filter hook input priority -10; policy accept;
  iifname "{interface}" udp dport {{ 53, 67 }} accept
  iifname "{interface}" ip daddr {self.config.gateway} tcp dport {{ 53, 80 }} accept
  iifname "{interface}" drop
  ip daddr {self.config.gateway} drop
 }}
 chain forward {{ type filter hook forward priority -10; policy accept;
  iifname "{interface}" drop
  oifname "{interface}" drop
 }}
}}''')
        if self.dns is None or self.dns.returncode is not None:
            # Dedicated dnsmasq process: NetworkManager uses manual addressing, not shared DHCP.
            self.dns = await asyncio.create_subprocess_exec(
                "dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", "--bind-dynamic",
                f"--interface={self.config.interface}", f"--listen-address={self.config.gateway}",
                "--except-interface=lo", "--no-resolv", "--no-hosts",
                f"--address=/#/{self.config.gateway}",
                "--dhcp-range=192.168.50.20,192.168.50.150,255.255.255.0,1h",
                f"--dhcp-option=3,{self.config.gateway}", f"--dhcp-option=6,{self.config.gateway}",
                "--dhcp-leasefile=/run/noha-network/leases", "--user=root",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.sleep(0.3)
            if self.dns.returncode is not None:
                raise NetworkError("DNS/DHCP indisponível; verifique conflito na porta 53")
        # Shared web service is always running; destination address selects setup.

    async def hotspot_down(self):
        if self.dns and self.dns.returncode is None:
            self.dns.terminate()
            try:
                await asyncio.wait_for(self.dns.wait(), 5)
            except TimeoutError:
                self.dns.kill()
                await self.dns.wait()
        self.dns = None
        await self.nm("connection", "down", "uuid", self.hotspot_uuid, check=False)
        await command("nft", "delete", "table", "inet", "noha_setup", check=False)

