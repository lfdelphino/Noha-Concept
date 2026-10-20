"""Unprivileged web entry point: dashboard, status board and Wi-Fi portal. Secrets stay behind the admin socket."""
import asyncio
import hashlib
from importlib.resources import files
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, web

from .portal import make_app as portal_app

ADMIN = web.AppKey("admin", ClientSession)
SESSIONS = web.AppKey("sessions", dict)
COOKIE = "noha_session"
LIFETIME = 8 * 3600
KIOSK_LIFETIME = 10 * 365 * 86400  # the Pi's display stays logged in; revoke by replacing kiosk-token
TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".png": "image/png", ".svg": "image/svg+xml"}
# The Pi's display is a read-only status board; everything else needs the password.
BOARD = {("GET", "session"), ("GET", "events"), ("GET", "status")}
OPERATIONS = {("GET", "status"), ("GET", "settings"), ("POST", "settings"), ("POST", "discard"), ("GET", "usage"),
              ("GET", "logs"), ("GET", "volume"), ("POST", "volume"), ("POST", "presence"), ("POST", "trigger"),
              ("POST", "ngrok"), ("POST", "password"), ("POST", "check-update"), ("POST", "install-update")}
PASSWORD_CHANGE = {("GET", "session"), ("POST", "logout"), ("POST", "password")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def load_assets():
    """Static files of noha/web, served from memory with content ETags."""
    assets = {}

    def walk(folder, prefix):
        for entry in folder.iterdir():
            if entry.is_dir():
                walk(entry, f"{prefix}{entry.name}/")
            elif (kind := TYPES.get("." + entry.name.rpartition(".")[2])):
                body = entry.read_bytes()
                assets[prefix + entry.name] = (body, kind, '"' + hashlib.sha256(body).hexdigest()[:16] + '"')
    walk(files("noha").joinpath("web"), "")
    logo = files("noha").joinpath("logo.png").read_bytes()
    assets["logo.png"] = (logo, "image/png", '"' + hashlib.sha256(logo).hexdigest()[:16] + '"')
    return assets


def is_recovery(request, gateway):
    # sockname is kernel-provided. A LAN/ngrok Host header cannot select this route.
    local = request.transport.get_extra_info("sockname") if request.transport else None
    if not local:
        return False
    address = ipaddress.ip_address(local[0])
    return str(getattr(address, "ipv4_mapped", None) or address) == gateway


def loopback(request):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return bool(peer) and ipaddress.ip_address(peer[0]).is_loopback


def forwarded(request):
    return any(h in request.headers for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded"))


def on_device(request):
    """A browser on the Pi itself. ngrok also connects from loopback, but with its own Host and
    X-Forwarded-For headers, so a public visitor can never pass this check."""
    hostname = urlsplit("//" + request.headers.get("Host", "")).hostname
    return loopback(request) and hostname in {"localhost", "127.0.0.1", "::1"} and not forwarded(request)


def via_tunnel(request):
    """Reached through ngrok: the public internet, not the venue's network."""
    return loopback(request) and not on_device(request)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def asset(request, item):
    body, kind, etag = item
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type=kind, charset="utf-8" if kind.startswith("text") else None,
                        headers=headers)


class Dashboard:
    def __init__(self):
        self.attempts = {}
        self.login_lock = asyncio.Lock()
        self.assets = load_assets()

    async def call(self, app, operation, method="GET", data=None):
        kwargs = {"json": data} if method == "POST" else {"params": data or {}}
        async with app[ADMIN].request(method, "http://localhost/" + operation, **kwargs) as response:
            return response.status, await response.json()

    def new_session(self, app, token, revision, scope, lifetime, must_change=False):
        session = {"csrf": secrets.token_urlsafe(32), "revision": revision, "expires": time.time() + lifetime,
                   "scope": scope, "must_change": must_change}
        app[SESSIONS][digest(token)] = session
        return session

    async def kiosk_session(self, app, token):
        code, result = await self.call(app, "kiosk", "POST", {"token": token})
        if code != 200 or "revision" not in result:
            return None
        return self.new_session(app, token, result["revision"], "board", KIOSK_LIFETIME)

    async def session(self, request):
        token = request.cookies.get(COOKIE, "")
        session = request.app[SESSIONS].get(digest(token))
        if session and session["expires"] >= time.time():
            code, auth = await self.call(request.app, "auth")
            if code == 200 and session["revision"] == auth.get("revision"):
                return session
        request.app[SESSIONS].pop(digest(token), None)
        # Sessions live in memory: after a restart or password change the display's cookie
        # (its kiosk token) is checked again instead of showing the login screen.
        if token and on_device(request) and (session := await self.kiosk_session(request.app, token)):
            return session
        raise web.HTTPUnauthorized()

    def allowed_hosts(self, settings, request):
        allowed = {socket.gethostname() + ".local", "localhost", "127.0.0.1", "::1"}
        if ngrok_host := urlsplit(settings.get("ngrok_url", "")).hostname:
            allowed.add(ngrok_host)
        if local := request.transport.get_extra_info("sockname"):
            allowed.add(local[0])
        return allowed

    async def handle(self, request):
        app = request.app
        # Bound the allowed hosts without trusting forwarded Host headers.
        host = request.headers.get("Host", "")
        parsed = urlsplit("//" + host)
        if not parsed.hostname or parsed.username or parsed.password or parsed.path:
            raise web.HTTPForbidden()
        _, settings = await self.call(app, "settings")
        if parsed.hostname not in self.allowed_hosts(settings, request):
            raise web.HTTPForbidden(text="Endereço não autorizado")
        secure = request.secure or (loopback(request) and request.headers.get("X-Forwarded-Proto") == "https")
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("Origin") != ("https://" if secure else "http://") + host:
                raise web.HTTPForbidden(text="Origem inválida")
        path = request.path
        if request.method == "GET":
            if path in {"/", "/index.html"}:
                return asset(request, self.assets["index.html"])
            if path == "/board":
                return asset(request, self.assets["board.html"])
            if path == "/logo.png":
                return asset(request, self.assets["logo.png"])
            if path == "/dashboard.css":  # the installed kiosk launcher polls this until the portal is up
                return asset(request, self.assets["app.css"])
            if path.startswith("/assets/") and (item := self.assets.get(path.removeprefix("/assets/"))):
                return asset(request, item)
            if path == "/kiosk":
                return await self.kiosk(request)
        if path == "/api/hello" and request.method == "GET":
            # Before login: whether the first-access hint may be shown (only on the venue's network).
            code, auth = await self.call(app, "auth")
            tunnel = via_tunnel(request)
            return web.json_response({"tunnel": tunnel, "default_hint": code == 200 and bool(auth.get("default")) and not tunnel})
        if path == "/api/login" and request.method == "POST":
            return await self.login(request, secure)
        session = await self.session(request)
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-Noha-Token", ""), session["csrf"]):
            raise web.HTTPForbidden()
        operation = path.removeprefix("/api/") if path.startswith("/api/") else None
        allowed = (BOARD if session["scope"] == "board" else
                   PASSWORD_CHANGE if session.get("must_change") else OPERATIONS | {("GET", "session"), ("POST", "logout"), ("GET", "events")})
        if operation is None or (request.method, operation) not in allowed:
            if session.get("must_change") and operation is not None:
                return web.json_response({"error": "Troque a senha padrão para continuar", "code": "password_change_required"},
                                         status=403)
            raise web.HTTPNotFound()
        if operation == "session":
            return web.json_response({"csrf": session["csrf"], "scope": session["scope"], "must_change": session.get("must_change", False)})
        if operation == "logout":
            app[SESSIONS].pop(digest(request.cookies.get(COOKIE, "")), None)
            response = web.json_response({"accepted": True})
            response.del_cookie(COOKIE, path="/")
            return response
        if operation == "events":
            return await self.events(request, session)
        data = await request.json() if request.method == "POST" else dict(request.query)
        code, result = await self.call(app, operation, request.method, data)
        result.pop("auth_revision", None)
        if operation == "password" and code == 200:
            app[SESSIONS].clear()
        return web.json_response(result, status=code)

    async def kiosk(self, request):
        if not on_device(request):
            raise web.HTTPNotFound()
        token = request.query.get("token", "")
        if not token or not await self.kiosk_session(request.app, token):
            raise web.HTTPForbidden(text="Token do painel local inválido")
        response = web.Response(status=302, headers={"Location": "/board"})
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=KIOSK_LIFETIME, path="/")
        return response

    async def login(self, request, secure):
        app = request.app
        # Trust client-address forwarding only through the loopback ngrok upstream.
        address = request.remote or "unknown"
        if loopback(request):
            address = request.headers.get("X-Forwarded-For", address).split(",")[-1].strip()
        async with self.login_lock:
            now = time.time()
            self.attempts = {k: v for k, v in self.attempts.items() if v[1] > now}
            count, deadline = self.attempts.get(address, (0, now + 300))
            if count >= 5 or sum(v[0] for v in self.attempts.values()) >= 50:
                return web.json_response({"error": "Muitas tentativas. Aguarde cinco minutos."}, status=429)
            data = await request.json()
            code, result = await self.call(app, "login", "POST", data)
            if code != 200:
                self.attempts[address] = (count + 1, deadline)
                return web.json_response(result, status=code)
            self.attempts.pop(address, None)
            if result.get("default") and via_tunnel(request):
                return web.json_response({"error": "A senha padrão só funciona na rede do local. Troque-a por lá antes "
                                                   "de usar o acesso pela internet."}, status=403)
            for key in [k for k, v in app[SESSIONS].items() if v["expires"] <= now]:
                app[SESSIONS].pop(key, None)
            token = secrets.token_urlsafe(32)
            session = self.new_session(app, token, result["revision"], "admin", LIFETIME, bool(result.get("default")))
            response = web.json_response({"csrf": session["csrf"], "must_change": session["must_change"]})
            response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="Strict", max_age=LIFETIME, path="/")
            return response

    async def events(self, request, session):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                               "X-Accel-Buffering": "no"})
        await response.prepare(request)
        logs = session["scope"] == "admin" and request.query.get("logs") == "1"
        try:
            while True:
                await self.session(request)
                code, status = await self.call(request.app, "status")
                status.pop("auth_revision", None)
                payload = {"status": status, "available": code == 200}
                if logs:
                    _, payload["logs"] = await self.call(request.app, "logs", data={
                        "service": request.query.get("service", "voice"), "level": request.query.get("level", "info")})
                await response.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                await asyncio.sleep(2)  # fast enough for the live motion indicator
        except (ConnectionError, asyncio.CancelledError, web.HTTPUnauthorized):
            pass
        return response


def make_app(network, admin_socket="/run/noha-admin/control.sock"):
    app = portal_app(network)
    app._client_max_size = 128 * 1024
    app[SESSIONS] = {}
    dashboard = Dashboard()

    @web.middleware
    async def route(request, handler):
        try:
            response = await handler(request) if is_recovery(request, network.gateway) else await dashboard.handle(request)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, headers=exc.headers, text=exc.text)
        except (ClientError, OSError, TimeoutError):
            response = web.json_response({"error": "Serviço iniciando. Aguarde alguns segundos."}, status=503)
        except (ValueError, TypeError):
            response = web.json_response({"error": "Dados inválidos"}, status=400)
        # The Wi-Fi portal page sets its own policy for its inline script.
        for header, value in (("Cache-Control", "no-store"), ("Content-Security-Policy", CSP)):
            response.headers.setdefault(header, value)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        return response
    app.middlewares.append(route)

    async def resources(app):
        async with ClientSession(connector=UnixConnector(path=admin_socket), timeout=ClientTimeout(total=20)) as client:
            app[ADMIN] = client
            yield
    app.cleanup_ctx.append(resources)
    return app


async def serve(network):
    runner = web.AppRunner(make_app(network), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", 80).start()
        if socket.has_ipv6:
            try:
                await web.TCPSite(runner, "::", 80).start()
            except OSError:
                pass  # IPv4 remains available when IPv6 is disabled on this Pi.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
