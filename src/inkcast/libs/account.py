# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Delete my account (E9.F9.5.S4, issue #41): the start of it, shared by the MCP tool and the REST endpoint.

Inkcast leads, Keycloak deletes. The assistant or the API only ever hands the person a one-time link to
Inkcast's own page; there, after signing in again, they read what goes and what stays and continue to Keycloak,
which asks them to confirm and deletes the account on their own authority (its ``delete_account``
application-initiated action). Inkcast erases their data (``Store.erase_user``) once it knows the account is
gone: on the visit back from Keycloak's "deleted" page, checked through the user sync's read-only ``view-users``,
and in any case from the ``DELETE_ACCOUNT`` user event the sync reads. Without the sync Inkcast could never
learn that the account is gone, so the link is refused until ``INKCAST_KEYCLOAK_SYNC_CLIENT_ID`` is set.
"""

from __future__ import annotations

from typing import Any

from .contracts.models import DeletionLink
from .http.rest import ApiError

# The one-time link lasts Config.one_time_link_seconds (INKCAST_ONE_TIME_LINK_MINUTES, 15 by default, at most 60; F1.10.S4,
# issue #92), the same setting as the connect link's. The return window below is a protocol window, deliberately not
# a setting: it covers Keycloak's own pages (sign in again, confirm) and the visit back, however the link was sized.
RETURN_TTL = 1800     # from "Continue" to the visit back from Keycloak: 30 minutes
COOKIE = "inkcast_deletion"


def deletion_link(config: Any, store: Any, user_id: str) -> DeletionLink:
    """The one-time link, good for ``config.one_time_link_seconds`` (the reply states it), or the reason there is none
    (``ApiError`` 409, in words the person can act on)."""
    if config.login != "oidc":
        raise ApiError(409, "not_available", "Deleting your account is not available on this Inkcast: it needs Keycloak "
                       "sign-in (INKCAST_LOGIN=oidc), and this one uses another sign-in. Ask the operator to remove your data.")
    if not config.keycloak_sync_client_id:
        raise ApiError(409, "not_available", "Deleting your account is not available on this Inkcast yet: the Keycloak "
                       "user sync (INKCAST_KEYCLOAK_SYNC_CLIENT_ID) is not configured, so Inkcast could not learn that "
                       "your account is gone. Ask the operator.")
    ttl = config.one_time_link_seconds
    ticket = store.create_deletion_request(user_id, ttl=ttl)
    return DeletionLink(url=f"{config.public_url}/account/delete/{ticket}", expires_in=ttl)


__all__ = ["COOKIE", "RETURN_TTL", "deletion_link"]
