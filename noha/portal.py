import asyncio
from importlib.resources import files
import secrets

from aiohttp import ClientError, ClientSession, ClientTimeout, UnixConnector, web

CONTROLLER = web.AppKey("controller", ClientSession)


def make_app(config):
    token = secrets.token_urlsafe(32)
    page = files("noha").joinpath("portal.html").read_text(encoding="utf-8").replace("__TOKEN__", token)
    logo = files("noha").joinpath("logo.png").read_bytes()
    app = web.Application(client_max_size=4096)

    async def resources(app):
        async with ClientSession(connector=UnixConnector(path=config.socket),
                                 timeout=ClientTimeout(total=15)) as client:
            app[CONTROLLER] = client
            yield
    app.cleanup_ctx.append(resources)

    async def index(request):
        if request.host not in {config.gateway, config.gateway + ":80"}:
            raise web.HTTPFound(f"http://{config.gateway}/")
        return web.Response(text=page, content_type="text/html", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'"})

    async def api(request):
        operation = request.match_info["operation"]
        allowed = {("GET", "status"), ("GET", "networks"), ("POST", "connect"), ("POST", "scan")}
        if (request.method, operation) not in allowed:
            raise web.HTTPNotFound()
        if request.method == "POST":
            if not secrets.compare_digest(request.headers.get("X-Noha-Token", ""), token):
                raise web.HTTPForbidden()
            origin = request.headers.get("Origin")
            if origin and origin != f"http://{config.gateway}":
                raise web.HTTPForbidden()
        body = await request.read() if request.method == "POST" else None
        try:
            async with app[CONTROLLER].request(request.method, f"http://localhost/{operation}",
                                                 data=body, headers={"Content-Type": "application/json"}) as response:
                return web.Response(body=await response.read(), status=response.status,
                                    content_type="application/json", headers={"Cache-Control": "no-store"})
        except (ClientError, OSError, TimeoutError):
            return web.json_response({"error": "Serviço iniciando. Aguarde alguns segundos."}, status=503)

    async def logo_png(request):
        return web.Response(body=logo, content_type="image/png", headers={"Cache-Control": "max-age=3600"})

    async def captive(request):
        raise web.HTTPFound(f"http://{config.gateway}/")

    app.router.add_get("/", index)
    app.router.add_get("/logo.png", logo_png)
    app.router.add_route("*", "/api/{operation}", api)
    # Android /generate_204, Apple /hotspot-detect.html, Windows /connecttest.txt,
    # and unknown HTTP URLs all lead to the same fully local page.
    app.router.add_route("*", "/{tail:.*}", captive)
    return app


async def serve(config):
    runner = web.AppRunner(make_app(config), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, config.gateway, 80).start()
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

