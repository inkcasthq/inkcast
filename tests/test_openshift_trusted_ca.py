# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""On OpenShift, Inkcast's outbound HTTPS trusts the cluster's CA bundle (issue #308): the injected ConfigMap is
mounted and SSL_CERT_FILE names it. The HTTP client Inkcast builds (libs/runtime.py) honours the variable."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import os
import pathlib
import ssl
import subprocess
import tempfile
import unittest
from unittest import mock

import httpx2
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = pathlib.Path(__file__).resolve().parents[1]
KUSTOMIZE = ROOT / "deploy" / "kustomize"
INJECT = "config.openshift.io/inject-trusted-cabundle"
CA_FILE = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


def render(path: str) -> list[dict]:
    out = subprocess.run(["kubectl", "kustomize", str(KUSTOMIZE / path)], capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def inkcast_pod(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "inkcast")["spec"]["template"]["spec"]


class Manifests(unittest.TestCase):
    def test_openshift_mounts_the_injected_bundle_and_names_it(self):
        for path in ("overlays/openshift", "environments/staging", "environments/production"):
            with self.subTest(path=path):
                docs = render(path)
                cms = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"].get("labels", {}).get(INJECT) == "true"]
                self.assertEqual([d["metadata"]["name"] for d in cms], ["inkcast-trusted-ca"])
                self.assertNotIn("data", cms[0])   # the operator owns it and deletes anything else there
                pod = inkcast_pod(docs)
                volume = next(v for v in pod["volumes"] if v["name"] == "trusted-ca")
                self.assertEqual(volume["configMap"], {"name": "inkcast-trusted-ca",
                                                       "items": [{"key": "ca-bundle.crt", "path": "tls-ca-bundle.pem"}]})
                container = next(c for c in pod["containers"] if c["name"] == "inkcast")
                mount = next(m for m in container["volumeMounts"] if m["name"] == "trusted-ca")
                self.assertEqual(mount, {"name": "trusted-ca", "mountPath": "/etc/pki/ca-trust/extracted/pem",
                                         "readOnly": True})
                self.assertIn({"name": "SSL_CERT_FILE", "value": CA_FILE}, container["env"])
                self.assertEqual(f"{mount['mountPath']}/{volume['configMap']['items'][0]['path']}", CA_FILE)

    def test_kubernetes_is_unchanged(self):
        docs = render("overlays/kubernetes")
        self.assertFalse([d for d in docs if d["kind"] == "ConfigMap" and INJECT in d["metadata"].get("labels", {})])
        self.assertNotIn("SSL_CERT_FILE", yaml.safe_dump_all(docs))


def _ca_and_server(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """A throwaway CA, and a certificate it signs for 127.0.0.1: the CA file, the server's chain, the server's key."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "trust test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_file, chain, key_file = directory / "ca.pem", directory / "chain.pem", directory / "key.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return ca_file, chain, key_file


class TheClientHonoursSslCertFile(unittest.IsolatedAsyncioTestCase):
    """The premise the overlay rests on, proved against a real TLS handshake: a client built as libs/runtime.py builds
    it (no verify= override) trusts the CA that SSL_CERT_FILE names, and refuses the same server without it."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ca_file, chain, key = _ca_and_server(pathlib.Path(self.tmp.name))
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(chain, key)

        async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
                pass   # the refused handshake
            finally:
                writer.close()

        loop = asyncio.get_running_loop()

        def quiet_refused_handshake(loop: asyncio.AbstractEventLoop, context: dict) -> None:
            # The client that refuses the certificate drops the handshake; asyncio logs that on the server side.
            if not isinstance(context.get("exception"), (ConnectionResetError, ssl.SSLError)):
                loop.default_exception_handler(context)

        loop.set_exception_handler(quiet_refused_handshake)
        self.server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=server_ctx)
        self.url = f"https://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def get(self, env: dict[str, str]) -> httpx2.Response:
        clean = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True):
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, connect=5.0), follow_redirects=False) as http:
                return await http.get(self.url)

    async def test_trusted_with_ssl_cert_file(self):
        self.assertEqual((await self.get({"SSL_CERT_FILE": str(self.ca_file)})).text, "ok")

    async def test_refused_without_it(self):
        with self.assertRaises(httpx2.ConnectError):
            await self.get({})


if __name__ == "__main__":
    unittest.main()
