import unittest

from aiohttp.test_utils import TestClient, TestServer

from noha.config import Network
from noha.controller import Controller
from noha.portal import make_app


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_captive_redirect_and_local_page_and_csrf(self):
        app = make_app(Network())
        # The hardware service uses a Unix socket; the page/CSRF paths don't need it.
        app.cleanup_ctx.clear()
        async with TestClient(TestServer(app)) as client:
            probe = await client.get("/generate_204", allow_redirects=False)
            self.assertEqual(probe.status, 302)
            self.assertEqual(probe.headers["Location"], "http://192.168.50.1/")
            canonical = await client.get("/", allow_redirects=False)
            self.assertEqual(canonical.status, 302)
            page = await client.get("/", headers={"Host": "192.168.50.1"})
            html = await page.text()
            self.assertEqual(page.status, 200)
            self.assertIn("Conectar Noha", html)
            self.assertNotIn("__TOKEN__", html)
            self.assertNotIn("OPENAI_API_KEY", html)
            logo = await client.get("/logo.png", allow_redirects=False)  # the only asset on the hotspot
            self.assertEqual((logo.status, logo.content_type), (200, "image/png"))
            denied = await client.post("/api/connect", json={"ssid": "Home", "password": "secret123"})
            self.assertEqual(denied.status, 403)

    async def test_controller_acknowledges_before_connection_attempt(self):
        adapter = type("Adapter", (), {"ssid": "Noha-Setup-TEST"})()
        controller = Controller(Network(), adapter)
        controller.mode = "configuracao"
        captured = []
        def launch(coroutine):
            captured.append(coroutine)
        controller.launch = launch
        try:
            async with TestClient(TestServer(controller.web_app())) as client:
                response = await client.post("/connect", json={"ssid": "Home", "password": "secret123"})
                self.assertEqual(response.status, 202)
                self.assertEqual(await response.json(), {"accepted": True})
                self.assertEqual(len(captured), 1)
                invalid = await client.post("/connect", json={"ssid": "Home", "password": "short"})
                self.assertEqual(invalid.status, 400)
        finally:
            for coroutine in captured:
                coroutine.close()


if __name__ == "__main__":
    unittest.main()
