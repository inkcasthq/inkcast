# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Every path that carries a credential is masked in the logs, and the next one cannot be missed (issue #149).

A one-time ticket (/connect/<ticket>, /account/delete/<ticket>) is a bearer capability: whoever reads it in a log can
act as the person until it expires or is used. The redaction of such paths is derived from the route templates
(``observability.ROUTE_TEMPLATES``, the parameters named in ``CREDENTIAL_PATH_PARAMS``), not kept by hand. These tests
walk every route the services mount and fail on a credential-bearing parameter that no template covers, and on a
parameter they cannot classify; then they drive each such path through both log formats as a message, a field and a
traceback, the ways a URL reaches a log line on the error paths."""

import io
import logging
import re
import secrets
import unittest

from starlette.routing import Mount, Route

from inkcast.launcher import build, resolve
from inkcast.libs.config import Config
from inkcast.libs.http.app import compose
from inkcast.libs.observability import (CREDENTIAL_PATH_PARAMS, JsonFormatter, Redactor, TextFormatter,
                                        credential_path_patterns, redact, route_label)

# Path parameters that are ids, never credentials: an id may stay in a log line (it says which post, token, platform
# or asset). A new parameter name must be classified: here, or in observability.CREDENTIAL_PATH_PARAMS.
PLAIN_PATH_PARAMS = frozenset({"platform_id", "post_id", "token_id", "name", "path"})
PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z_][a-zA-Z0-9_]*)?\}")   # Starlette's own grammar


def mounted_paths(routes, prefix: str = ""):
    for route in routes:
        if isinstance(route, Mount):
            yield from mounted_paths(route.routes, prefix + route.path)
        elif isinstance(route, Route):
            yield prefix + route.path


def every_route() -> list[str]:
    """Every path template the product services and the Gateway mount, as the launcher builds them."""
    cfg = Config(public_url="http://localhost:8787", secret_key="k" * 40, database_path=":memory:", login="password",
                 password="p" * 12)
    paths: set[str] = set()
    for names in ("All", "Gateway"):
        specs = resolve(names)
        ctx = build(cfg, specs)
        app = compose(specs, ctx)                           # the RequestContext; .app is the Starlette app
        paths |= set(mounted_paths(app.app.routes))
        if ctx.store is not None:                           # the Gateway alone has none
            ctx.store.close()
    return sorted(paths)


def credential_routes(routes: list[str]) -> list[str]:
    return [r for r in routes if any(name in CREDENTIAL_PATH_PARAMS for name in PARAM.findall(r))]


def sample_of(route: str, ticket: str) -> str:
    return PARAM.sub(lambda m: ticket if m.group(1) in CREDENTIAL_PATH_PARAMS else "x1", route)


class CredentialPaths(unittest.TestCase):
    def test_every_path_parameter_is_classified_and_every_credential_path_is_masked(self):
        routes = every_route()
        for route in routes:
            for name in PARAM.findall(route):
                self.assertIn(name, CREDENTIAL_PATH_PARAMS | PLAIN_PATH_PARAMS,
                              f"{route}: classify {{{name}}} as a credential (observability.CREDENTIAL_PATH_PARAMS) "
                              f"or as a plain id (PLAIN_PATH_PARAMS in this test)")
        with_credentials = credential_routes(routes)
        for known in ("/account/delete/{ticket}", "/connect/{ticket}"):
            self.assertIn(known, with_credentials, "the walk reaches the one-time routes")
        for route in with_credentials:
            ticket = secrets.token_urlsafe(32)
            sample = sample_of(route, ticket)
            self.assertNotEqual(route_label(sample), "other",
                                f"{route} is not in observability.ROUTE_TEMPLATES, so its ticket is not derived")
            kept = sample.split(ticket)[0]
            for text in (sample, f"GET /identity-service{sample} -> 302 (3.1ms)", f"http://localhost:8787{sample}?x=1",
                         f'{{"path": "{sample}"}}'):
                out = redact(text)
                self.assertNotIn(ticket, out, text)
                self.assertIn(kept + "***", out, text)

    def test_the_ticket_is_masked_in_a_message_a_field_and_a_traceback_in_both_formats(self):
        for route in credential_routes(every_route()):
            ticket = secrets.token_urlsafe(32)
            path = sample_of(route, ticket)
            for formatter in (JsonFormatter("identity-service"), TextFormatter("%(levelname)s %(message)s")):
                stream = io.StringIO()
                handler = logging.StreamHandler(stream)
                handler.setFormatter(formatter)
                handler.addFilter(Redactor())
                log = logging.getLogger(f"inkcast.test.credential_paths.{id(formatter)}")
                log.addHandler(handler)
                log.propagate = False
                try:
                    try:
                        raise RuntimeError(f"GET http://localhost:8787{path} failed")
                    except RuntimeError:
                        log.exception("Request %s refused", path,
                                      extra={"event": "http.request.rejected", "method": "GET", "path": path})
                finally:
                    log.removeHandler(handler)
                out = stream.getvalue()
                self.assertNotIn(ticket, out, f"{type(formatter).__name__} wrote the ticket of {route}")
                self.assertIn("RuntimeError", out, "the traceback is still there, masked")
                self.assertIn(path.split(ticket)[0] + "***", out)

    def test_a_new_one_time_route_is_masked_the_day_its_template_is_listed(self):
        """The derivation, on templates that do not exist: a ticket after an id, a ticket mid-path, and an id alone."""
        ticket = secrets.token_urlsafe(32)
        patterns = credential_path_patterns(("/api/v1/posts/{post_id}/share/{ticket}", "/invite/{ticket}/accept",
                                             "/api/v1/posts/{post_id}"))
        self.assertEqual(len(patterns), 2, "one pattern per credential-bearing parameter; an id alone gives none")
        text = f"GET /api/v1/posts/pst_1/share/{ticket} /invite/{ticket}/accept /api/v1/posts/pst_1 /share/{ticket}"
        for pattern in patterns:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        self.assertEqual(text, f"GET /api/v1/posts/pst_1/share/*** /invite/***/accept /api/v1/posts/pst_1 /share/{ticket}",
                         "the ticket goes, the id stays, and a path that is not a listed route is left alone")


if __name__ == "__main__":
    unittest.main()
