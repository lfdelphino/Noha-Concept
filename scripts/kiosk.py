#!/usr/bin/env python3
"""Show the read-only status board full screen on the Pi's own display at boot. Run with sudo."""
import argparse
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys

TOKEN = Path("/etc/noha/kiosk-token")
LAUNCHER = Path("/usr/local/bin/noha-kiosk")
# Waits for the portal, then opens /kiosk, which only answers browsers on the Pi itself.
SCRIPT = r'''#!/bin/sh
# Noha status board (installed by scripts/kiosk.py).
for _ in $(seq 90); do curl -fs -o /dev/null http://localhost/board && break; sleep 2; done
token=$(cat /etc/noha/kiosk-token) || exit 1
browser=$(command -v chromium || command -v chromium-browser) || exit 1
# Size the page like a 1080p screen: a 4K TV at 100% scale would show it at phone size.
width=$(wlr-randr 2>/dev/null | awk '/current/ {split($1, size, "x"); print size[1]; exit}')
scale=$(awk -v w="${width:-0}" 'BEGIN {print (w >= 2560) ? w / 1920 : 1}')
exec "$browser" --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic \
  --disable-session-crashed-bubble --hide-crash-restore-bubble --disable-features=Translate --lang=pt-BR \
  --force-device-scale-factor="$scale" \
  --user-data-dir="$HOME/.local/share/noha-kiosk" "http://localhost/kiosk?token=$token"
'''
ENTRY = """[Desktop Entry]
Type=Application
Name=Noha painel
Comment=Painel do robô em tela cheia
Exec={exec}
X-GNOME-Autostart-enabled=true
"""


def autostart(account):
    return Path(account.pw_dir) / ".config/autostart/noha-kiosk.desktop"


def setup(user):
    account = pwd.getpwnam(user)
    if not TOKEN.exists():
        TOKEN.write_text(secrets.token_urlsafe(32) + "\n", encoding="ascii")
    # Readable by the desktop user that launches the browser; the portal account cannot read it.
    os.chown(TOKEN, 0, account.pw_gid)
    os.chmod(TOKEN, 0o640)
    LAUNCHER.write_text(SCRIPT, encoding="utf-8")
    os.chmod(LAUNCHER, 0o755)
    entry = autostart(account)
    for path in (entry.parent.parent, entry.parent):
        if not path.exists():
            path.mkdir(mode=0o755)
            os.chown(path, account.pw_uid, account.pw_gid)
    # lwrespawn (Raspberry Pi labwc) reopens the browser if it crashes or is closed.
    command = f"/usr/bin/lwrespawn {LAUNCHER}" if Path("/usr/bin/lwrespawn").exists() else str(LAUNCHER)
    entry.write_text(ENTRY.format(exec=command), encoding="utf-8")
    os.chown(entry, account.pw_uid, account.pw_gid)
    # Desktop autologin opens the session at boot; a display that never blanks also keeps
    # its HDMI audio output, which PipeWire loses when the screen sleeps.
    subprocess.run(["raspi-config", "nonint", "do_boot_behaviour", "B4"], check=True)
    subprocess.run(["raspi-config", "nonint", "do_blanking", "1"], check=True)


def remove(user):
    autostart(pwd.getpwnam(user)).unlink(missing_ok=True)
    LAUNCHER.unlink(missing_ok=True)
    TOKEN.unlink(missing_ok=True)  # also invalidates the display's saved login


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"), help="Conta do desktop")
    parser.add_argument("--remove", action="store_true", help="Desativa o painel na tela do Pi")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user:
        sys.exit("Execute com sudo python3 scripts/kiosk.py --user <conta do desktop>")
    if args.remove:
        remove(args.user)
        print("Painel local desativado.")
    else:
        setup(args.user)
        print("Painel local ativo: abre em tela cheia no próximo login do desktop (reinicie o Pi).")


if __name__ == "__main__":
    main()
