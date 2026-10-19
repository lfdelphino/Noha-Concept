#!/usr/bin/env python3
"""Remove only Noha-owned services/configuration; keep credentials and saved Wi-Fi."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import uuid


def run(*args, **kwargs):
    return subprocess.run(args, check=False, **kwargs)


def main():
    if os.geteuid() != 0:
        sys.exit("Execute com sudo python3 scripts/uninstall.py")
    manifest = Path("/var/lib/noha/install.json")
    if not manifest.exists():
        sys.exit("Manifesto de instalação não encontrado; consulte a remoção manual no README.")
    data = json.loads(manifest.read_text())
    account = pwd.getpwnam(data["user"])
    env = {**os.environ, "HOME": account.pw_dir, "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{account.pw_uid}/bus"}
    def user_run(*args):
        run("runuser", "-u", data["user"], "--", *args, env=env)
    run("systemctl", "disable", "--now", "noha-update.timer")
    run("systemctl", "stop", "noha-update.service")
    run("systemctl", "disable", "--now", "noha-update-check.timer")
    run("systemctl", "stop", "noha-update-check.service", "noha-update-boot-check.service")
    for name in ("noha-ngrok.service", "noha-portal.service", "noha-admin.service"):
        run("systemctl", "disable", "--now", name)
    run("systemctl", "disable", "--now", "noha-update-recover.service")
    user_run("systemctl", "--user", "disable", "--now", "noha.service")
    run("systemctl", "disable", "--now", "noha-network.service")
    run("systemctl", "stop", "noha-portal.service")
    hotspot = str(uuid.uuid5(uuid.NAMESPACE_DNS, "noha.local.setup"))
    run("nmcli", "connection", "delete", "uuid", hotspot)
    run("nft", "delete", "table", "inet", "noha_setup")
    # Fixed paths derived from the selected account; never recursively delete a user directory.
    files = [Path(account.pw_dir) / ".config/systemd/user/noha.service",
             Path(account.pw_dir) / ".config/pipewire/pipewire.conf.d/80-noha-echo.conf",
             Path("/etc/systemd/system/noha-network.service"),
             Path("/etc/systemd/system/noha-portal.service"), Path("/usr/local/bin/noha"),
             Path("/usr/local/bin/noha-update"), Path("/etc/systemd/system/noha-update.service"),
             Path("/etc/systemd/system/noha-update.timer"), Path("/etc/systemd/system/noha-update-recover.service"),
             Path("/etc/systemd/system/noha-update-check.timer"), Path("/etc/systemd/system/noha-update-check.service"),
             Path("/etc/systemd/system/noha-update-boot-check.service"), Path("/etc/systemd/system/noha-admin.service"),
             Path("/etc/systemd/system/noha-ngrok.service"), Path("/etc/avahi/services/noha.service"),
             Path("/etc/udev/rules.d/90-noha-respeaker.rules")]
    for path in files:
        path.unlink(missing_ok=True)
    from kiosk import remove as kiosk_remove
    kiosk_remove(data["user"])
    user_run("systemctl", "--user", "daemon-reload")
    user_run("systemctl", "--user", "restart", "pipewire.service", "pipewire-pulse.service", "wireplumber.service")
    run("systemctl", "daemon-reload")
    run("systemctl", "try-restart", "avahi-daemon.service")
    if not data["linger_before"]:
        run("loginctl", "disable-linger", data["user"])
    print("Serviços removidos. Redes Wi-Fi salvas preservadas.")
    print("Código, chave e backups preservados em /opt/noha, /etc/noha e /var/lib/noha.")
    print("Histórico preservado em /var/lib/noha-metrics. Hostname e pacotes ngrok/Avahi preservados.")
    print("Nenhum pacote compartilhado ou configuração I2C foi removido/alterado.")


if __name__ == "__main__":
    main()

