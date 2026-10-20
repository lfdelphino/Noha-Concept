"""Unprivileged shared web entry point. Secrets stay behind the admin socket."""
import asyncio
import hashlib
import ipaddress
import json
from importlib.resources import files
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
KIOSK_LIFETIME = 10 * 365 * 86400  # the local display stays logged in; revoke by replacing kiosk-token


def is_recovery(request, gateway):
    # sockname is kernel-provided. A LAN/ngrok Host header cannot select this route.
    local = request.transport.get_extra_info("sockname") if request.transport else None
    if not local:
        return False
    address = ipaddress.ip_address(local[0])
    return str(getattr(address, "ipv4_mapped", None) or address) == gateway


def on_device(request):
    """A browser on the Pi itself. ngrok also connects from loopback, but with its own Host and
    X-Forwarded-For headers, so a public visitor can never pass this check."""
    peer = request.transport.get_extra_info("peername") if request.transport else None
    hostname = urlsplit("//" + request.headers.get("Host", "")).hostname
    return (bool(peer) and ipaddress.ip_address(peer[0]).is_loopback and hostname in {"localhost", "127.0.0.1", "::1"}
            and not any(h in request.headers for h in ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded")))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Dashboard:
    def __init__(self):
        self.attempts = {}
        self.login_lock = asyncio.Lock()

    async def call(self, app, operation, method="GET", data=None):
        kwargs = {"json": data} if method == "POST" else {"params": data or {}}
        async with app[ADMIN].request(method, "http://localhost/" + operation, **kwargs) as response:
            return response.status, await response.json()

    async def kiosk_session(self, app, token):
        code, result = await self.call(app, "kiosk", "POST", {"token": token})
        if code != 200 or "revision" not in result:
            return None
        session = {"csrf": secrets.token_urlsafe(32), "revision": result["revision"], "expires": time.time() + KIOSK_LIFETIME}
        app[SESSIONS][digest(token)] = session
        return session

    async def session(self, request):
        token = request.cookies.get(COOKIE, "")
        session = request.app[SESSIONS].get(digest(token))
        if session and session["expires"] >= time.time():
            code, auth = await self.call(request.app, "auth")
            if code == 200 and session["revision"] == auth.get("revision"):
                return session
        request.app[SESSIONS].pop(digest(token), None)
        # Sessions live in memory: after a restart or password change the local display's
        # cookie (its kiosk token) is checked again instead of showing the login screen.
        if token and on_device(request) and (session := await self.kiosk_session(request.app, token)):
            return session
        raise web.HTTPUnauthorized()

    async def handle(self, request):
        app = request.app
        # Bound the allowed hosts without trusting forwarded Host headers.
        host = request.headers.get("Host", "")
        parsed = urlsplit("//" + host)
        hostname = parsed.hostname
        if not hostname or parsed.username or parsed.password or parsed.path:
            raise web.HTTPForbidden()
        _, settings = await self.call(app, "settings")
        allowed = {socket.gethostname() + ".local", "localhost", "127.0.0.1", "::1"}
        ngrok_host = urlsplit(settings.get("ngrok_url", "")).hostname
        if ngrok_host:
            allowed.add(ngrok_host)
        local = request.transport.get_extra_info("sockname")
        if local:
            allowed.add(local[0])
        if hostname not in allowed:
            raise web.HTTPForbidden(text="Endereço não autorizado")
        peer = request.transport.get_extra_info("peername")
        loopback = bool(peer and ipaddress.ip_address(peer[0]).is_loopback)
        secure = request.secure or (loopback and request.headers.get("X-Forwarded-Proto") == "https")
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("Origin")
            if origin != ("https://" if secure else "http://") + host:
                raise web.HTTPForbidden(text="Origem inválida")
        path = request.path
        if path == "/" and request.method == "GET":
            return web.Response(text=files("noha").joinpath("dashboard.html").read_text(encoding="utf-8"), content_type="text/html")
        assets = {"/dashboard.css": "text/css", "/dashboard.js": "application/javascript", "/logo.png": "image/png"}
        if path in assets and request.method == "GET":
            return web.Response(body=files("noha").joinpath(path[1:]).read_bytes(),
                                content_type=assets[path])
        if path == "/kiosk" and request.method == "GET":
            if not on_device(request):
                raise web.HTTPNotFound()
            token = request.query.get("token", "")
            if not token or not await self.kiosk_session(app, token):
                raise web.HTTPForbidden(text="Token do painel local inválido")
            response = web.Response(status=302, headers={"Location": "/"})
            response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=KIOSK_LIFETIME, path="/")
            return response
        if path == "/api/login" and request.method == "POST":
            # Trust client-address forwarding only through the loopback ngrok upstream.
            address = request.remote or "unknown"
            if loopback:
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
                # Expired entries are removed on every successful login.
                expired = [k for k, v in app[SESSIONS].items() if v["expires"] <= now]
                for k in expired:
                    app[SESSIONS].pop(k, None)
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                app[SESSIONS][digest(token)] = {"csrf": csrf, "revision": result["revision"], "expires": now + LIFETIME}
                response = web.json_response({"csrf": csrf})
                response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite="Strict", max_age=LIFETIME, path="/")
                return response
        session = await self.session(request)
        if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("X-Noha-Token", ""), session["csrf"]):
            raise web.HTTPForbidden()
        if path == "/api/session" and request.method == "GET":
            return web.json_response({"csrf": session["csrf"]})
        if path == "/api/logout" and request.method == "POST":
            app[SESSIONS].pop(digest(request.cookies.get(COOKIE, "")), None)
            response = web.json_response({"accepted": True})
            response.del_cookie(COOKIE, path="/")
            return response
        if path == "/api/events" and request.method == "GET":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                                    "X-Accel-Buffering": "no"})
            await response.prepare(request)
            try:
                while True:
                    await self.session(request)
                    code, status = await self.call(app, "status")
                    status.pop("auth_revision", None)
                    payload = {"status": status, "available": code == 200}
                    if request.query.get("logs") == "1":
                        _, payload["logs"] = await self.call(app, "logs", data={"service": request.query.get("service", "voice"),
                                                                              "level": request.query.get("level", "info")})
                    await response.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                    await asyncio.sleep(2)  # fast enough for the live motion indicator
            except (ConnectionError, asyncio.CancelledError, web.HTTPUnauthorized):
                pass
            return response
        operations = {("GET", "status"), ("GET", "settings"), ("POST", "settings"), ("GET", "usage"),
                      ("GET", "logs"), ("POST", "ngrok"), ("POST", "password"), ("POST", "presence"),
                      ("POST", "check-update"), ("POST", "install-update")}
        operation = path.removeprefix("/api/")
        if not path.startswith("/api/") or (request.method, operation) not in operations:
            raise web.HTTPNotFound()
        data = await request.json() if request.method == "POST" else dict(request.query)
        code, result = await self.call(app, operation, request.method, data)
        result.pop("auth_revision", None)
        if operation == "password" and code == 200:
            app[SESSIONS].clear()
        return web.json_response(result, status=code)


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
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
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
