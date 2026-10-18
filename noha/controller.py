"""Network recovery state machine and restricted local provisioning API."""
import asyncio
import logging
import os
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web
from .network import NetworkManager, validate_credentials

log = logging.getLogger(__name__)


async def internet_available(config):
    # Probe a real expected response, not just Wi-Fi association or DNS resolution.
    async def probe(client, url):
        try:
            async with client.get(url, allow_redirects=False) as response:
                return response.status == 204
        except Exception:
            return False
    async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as client:
        return any(await asyncio.gather(*(probe(client, url) for url in config.connectivity_urls)))


class Controller:
    def __init__(self, config, adapter, probe=internet_available, clock=time.monotonic):
        self.config, self.adapter, self.probe, self.clock = config, adapter, probe, clock
        self.mode = "iniciando"
        self.message = "Aguardando internet nas redes salvas."
        self.networks = []
        self.offline_since = clock()
        self.last_activity = float("-inf")
        self.last_retry = clock()
        self.last_good = None
        self.retry_cursor = 0
        self.operation = None
        self.lock = asyncio.Lock()

    def status(self):
        return {"mode": self.mode, "message": self.message, "ssid": self.adapter.ssid,
                "portal_url": f"http://{self.config.gateway}",
                "busy": self.operation is not None and not self.operation.done()}

    async def enter_portal(self, message=None):
        try:
            self.networks = await self.adapter.scan()
        except Exception:
            pass  # Manual SSID entry remains usable.
        await self.adapter.hotspot_up()
        self.mode = "configuracao"
        self.message = message or "Escolha uma rede Wi-Fi para conectar o Noha."
        self.last_retry = self.clock()
        log.info("network state=configuracao")

    async def submit(self, credentials):
        # Let the HTTP response reach the phone before its AP disappears.
        await asyncio.sleep(3)
        async with self.lock:
            self.mode = "conectando"
            self.message = "Tentando conectar. Se falhar, reconecte à rede de configuração."
            ident = None
            associated = False
            try:
                ident = await self.adapter.add_wifi(credentials)
                credentials.clear()
                await self.adapter.hotspot_down()
                try:
                    await self.adapter.activate(ident)
                except Exception:
                    pass
                associated = await self.adapter.associated(ident)
                if associated and await self.probe(self.config):
                    self.last_good = ident
                    self.mode, self.message = "online", "Noha conectado à internet."
                    self.offline_since = None
                    log.info("network state=online")
                    return
                if not associated:
                    await self.adapter.remove(ident)
                    ident = None
                await self.enter_portal("A rede conectou, mas está sem internet. Configuração salva para tentar novamente."
                                        if associated else "Não foi possível conectar. Confira a senha, a segurança e o sinal.")
            except Exception:
                log.error("Falha na configuração de rede; restaurando hotspot")
                if ident and not associated:
                    await self.adapter.remove(ident)
                await self.enter_portal("Não foi possível configurar a rede. Tente novamente.")
            finally:
                credentials.clear()

    async def retry_saved(self):
        self.mode = "reconectando"
        await self.adapter.hotspot_down()
        try:
            profiles = await self.adapter.saved()
            if self.last_good in profiles:
                profiles.remove(self.last_good)
                profiles.insert(0, self.last_good)
            # Bound each AP interruption; rotate through profiles across retries.
            if profiles:
                offset = self.retry_cursor % len(profiles)
                profiles = profiles[offset:] + profiles[:offset]
                self.retry_cursor += min(3, len(profiles))
            profiles = profiles[:3]
            for ident in profiles:
                try:
                    await self.adapter.activate(ident)
                except Exception:
                    continue
                if await self.probe(self.config):
                    self.last_good = ident
                    self.mode, self.message = "online", "Conexão recuperada."
                    self.offline_since = None
                    return
        finally:
            if self.mode != "online":
                await self.enter_portal()

    async def refresh(self):
        await asyncio.sleep(3)
        async with self.lock:
            self.mode = "buscando"
            await self.adapter.hotspot_down()
            try:
                self.networks = await self.adapter.scan()
            finally:
                await self.enter_portal("Busca atualizada. Escolha uma rede.")

    async def tick(self):
        if self.lock.locked() or (self.operation and not self.operation.done()):
            return
        if await self.probe(self.config):
            async with self.lock:
                if self.mode != "online":
                    await self.adapter.hotspot_down()
                    log.info("network state=online")
                self.mode, self.message = "online", "Conectado à internet."
                self.offline_since = None
            return
        now = self.clock()
        if self.mode == "configuracao":
            if not await self.adapter.hotspot_healthy():
                async with self.lock:
                    await self.enter_portal("Rede de configuração recuperada.")
            elif now - self.last_retry >= self.config.retry_seconds and now - self.last_activity >= self.config.portal_quiet_seconds:
                async with self.lock:
                    await self.retry_saved()
        else:
            if self.offline_since is None:
                self.offline_since = now
            if now - self.offline_since >= self.config.offline_seconds:
                async with self.lock:
                    await self.enter_portal()

    def launch(self, coroutine):
        self.operation = asyncio.create_task(coroutine)
        def completed(task):
            if not task.cancelled() and task.exception():
                log.error("Operação de rede falhou; o monitor tentará recuperar")
                self.mode = "recuperando"
                self.offline_since = self.clock() - self.config.offline_seconds
        self.operation.add_done_callback(completed)

    def web_app(self):
        app = web.Application(client_max_size=4096)

        async def status(request):
            return web.json_response(self.status())

        async def networks(request):
            self.last_activity = self.clock()
            return web.json_response({"networks": self.networks})

        async def mutate(request):
            if self.mode != "configuracao" or self.lock.locked() or (self.operation and not self.operation.done()):
                return web.json_response({"error": "Uma operação já está em andamento."}, status=409)
            self.last_activity = self.clock()
            if request.path == "/connect":
                try:
                    data = await request.json()
                    if not isinstance(data, dict):
                        raise ValueError("Dados inválidos")
                    credentials = validate_credentials(data)
                except (ValueError, TypeError):
                    return web.json_response({"error": "Confira SSID, senha e tipo de segurança."}, status=400)
                self.launch(self.submit(credentials))
            else:
                self.launch(self.refresh())
            return web.json_response({"accepted": True}, status=202)

        app.add_routes([web.get("/status", status), web.get("/networks", networks),
                        web.post("/connect", mutate), web.post("/scan", mutate)])
        return app


async def serve(config):
    adapter = NetworkManager(config)
    controller = Controller(config, adapter)
    # Clear stale AP/DNS/portal state from an interrupted previous process.
    await adapter.hotspot_down()
    runner = web.AppRunner(controller.web_app(), access_log=None)
    await runner.setup()
    path = Path(config.socket)
    path.unlink(missing_ok=True)
    site = web.UnixSite(runner, str(path))
    await site.start()
    os.chmod(path, 0o660)
    try:
        while True:
            try:
                await controller.tick()
            except Exception:
                log.error("Monitor de rede: tentativa falhou; nova tentativa em breve")
                controller.mode = "recuperando"
            await asyncio.sleep(config.check_seconds)
    finally:
        if controller.operation:
            controller.operation.cancel()
            await asyncio.gather(controller.operation, return_exceptions=True)
        await adapter.hotspot_down()
        await runner.cleanup()

