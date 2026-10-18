"""The whole end-to-end suite again, with every service in its own server under its full-name prefix
(INKCAST_PATH_PREFIX=auto) and the built-in Gateway in front on the public URL. If this passes,
the services really are decoupled: they share only contracts, the store and HTTP."""

import asyncio
import dataclasses
import os
import tempfile
import unittest

import httpx2

from fakes import Fakes
from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from test_e2e import EndToEnd, Harness, free_port, serve


class SplitHarness(Harness):
    """Every service in its own server under /<full-name>, the built-in Gateway on the public URL."""

    @classmethod
    def setUpClass(cls):
        fport, gport = free_port(), free_port()
        cls.tmp = tempfile.mkdtemp()
        cls.fakes = Fakes(f"http://127.0.0.1:{fport}")
        base = Config(
            public_url=f"http://127.0.0.1:{gport}", port=gport, secret_key="k" * 48,
            database_path=os.path.join(cls.tmp, "inkcast.db"),
            login="oidc", oidc_issuer=cls.fakes.issuer, oidc_client_id="inkcast", oidc_client_secret="kc-secret",
            oidc_required_role="inkcast-user", path_prefix="auto",
            platform_urls={"medium": f"{cls.fakes.base}/medium/v1", "devto": f"{cls.fakes.base}/devto/api"},
        )
        cls.config = base
        cls.servers = [serve(cls.fakes.app(), fport)]
        upstreams, cls.contexts = {}, {}
        for name in ("IdentityService", "PublishingApi", "ConnectorService", "InsightsService"):
            [spec] = resolve(name)
            port = free_port()
            cfg = dataclasses.replace(base, port=port)
            cfg.validate(spec.needs)
            ctx = build(cfg, [spec])
            cls.contexts[name] = ctx
            cls.servers.append(serve(compose([spec], ctx), port))
            upstreams[spec.full_name] = f"http://127.0.0.1:{port}/{spec.full_name}"
        gw_cfg = dataclasses.replace(base, upstreams=upstreams, path_prefix="/")
        [gw] = resolve("Gateway")
        gw_cfg.validate(gw.needs)
        cls.gateway = build(gw_cfg, [gw])
        cls.servers.append(serve(compose([gw], cls.gateway), gport))
        cls.app = cls.contexts["PublishingApi"]  # what the inherited tests inspect (store, metrics)

    def insights_context(self):
        return self.contexts["InsightsService"]


class SplitServices(SplitHarness, EndToEnd):

    def test_services_answer_under_their_full_names(self):
        ports = {name: s.config.port for name, s in zip(("fakes", "IdentityService", "PublishingApi", "ConnectorService",
                                                          "InsightsService"), self.servers)}

        async def run():
            async with httpx2.AsyncClient() as c:
                direct = await c.get(f"http://127.0.0.1:{ports['PublishingApi']}/publishing-api/api/openapi.json")
                root = await c.get(f"http://127.0.0.1:{ports['PublishingApi']}/api/openapi.json")
                via_gw = await c.get(f"{self.config.public_url}/api/openapi.json")
                asm = await c.get(f"http://127.0.0.1:{ports['IdentityService']}/identity-service"
                                  "/.well-known/oauth-authorization-server")
                probe = await c.get(f"http://127.0.0.1:{ports['ConnectorService']}/livez")
                nowhere = await c.get(f"{self.config.public_url}/no/such/path")
            return direct, root, via_gw, asm, probe, nowhere
        direct, root, via_gw, asm, probe, nowhere = asyncio.run(run())
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(root.status_code, 404)             # with prefix=auto nothing answers at "/"
        self.assertEqual(via_gw.json()["paths"].keys(), direct.json()["paths"].keys())
        self.assertEqual(asm.json()["issuer"], self.config.public_url)  # public URLs, whatever the internal path
        self.assertEqual(probe.status_code, 200)           # probes stay at the root
        self.assertEqual(nowhere.status_code, 404)
        self.assertEqual(nowhere.headers["content-type"], "application/problem+json")

    def test_gateway_reports_a_missing_upstream(self):
        cfg = dataclasses.replace(self.config, upstreams={"publishing-api": "http://127.0.0.1:9"}, path_prefix="/")
        [gw] = resolve("Gateway")
        ctx = build(cfg, [gw])
        port = free_port()
        server = serve(compose([gw], ctx), port)
        try:
            async def run():
                async with httpx2.AsyncClient() as c:
                    return (await c.get(f"http://127.0.0.1:{port}/authorize"),
                            await c.get(f"http://127.0.0.1:{port}/api/openapi.json"))
            missing, down = asyncio.run(run())
        finally:
            server.should_exit = True
        self.assertEqual(missing.status_code, 502)
        self.assertEqual(missing.json()["error"], "no_upstream")
        self.assertIn("INKCAST_UPSTREAM_IDENTITY_SERVICE", missing.json()["message"])
        self.assertEqual(down.status_code, 502)
        self.assertEqual(down.json()["error"], "upstream_unreachable")


del EndToEnd, Harness  # don't run the single-process suite twice from this module

if __name__ == "__main__":
    unittest.main()
