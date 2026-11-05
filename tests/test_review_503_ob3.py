# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of #503: a plug-in that changes the ``Prepared`` it was handed must not make every sync a PUT.

#503 reads a confirmed post whose stored payload doesn't hash as the file as a change. The payload a send's success
stores (``Publisher._deliver``'s ``sent``) and the one an update stores (``Publisher.update``) were taken from
``prepared`` after the plug-in's call. A third-party plug-in (the ``inkcast.platforms`` entry point) that normalizes
the payload in place, as nothing in ``docs/writing-a-platform-plugin.md`` forbids, then left a row whose payload never
hashes as the file: every sync after the first sent the same PUT again, forever. On 81009c4f the next sync was
``unchanged``; on d313438f it is ``update``, every time. The same read also gave the first send a false note that the
post was changed while it was out (#392's ``holds``), on 81009c4f already. Both are now read before the call."""

import unittest

from inkcast.libs.platforms import PublishResult

from test_sync_edit_during_send_392 import Editable
from test_sync_error_after_post import SyncCase


class Normalizing(Editable):
    """A platform that edits posts and normalizes the body in the ``Prepared`` it was handed, on publish and update."""

    id, name, default_api_url = "normalizing", "Normalizing", "https://normalizing.invalid"

    async def publish(self, creds, account, prepared, destination):
        prepared.payload["body"] = prepared.payload["body"].strip()
        return await super().publish(creds, account, prepared, destination)

    async def update(self, creds, account, remote_id, prepared, destination):
        prepared.payload["body"] = prepared.payload["body"].strip()
        self.updated.append((remote_id, prepared.payload["body"]))
        return PublishResult(url=f"https://{self.id}/{remote_id}", remote_id=remote_id, status="published")


class FindingAPlugInThatNormalizesIsNotSentAgain(SyncCase):
    platform_class = Normalizing

    def test_an_unchanged_file_is_unchanged_after_its_publish(self):
        first = self.sync()
        self.assertEqual((first.action, first.status), ("create", "published"))
        self.assertIsNone(self.store.get_post(first.post_id)["error"], "a send nobody edited noted an edit")
        for _ in range(3):
            again = self.sync()
            self.assertEqual(again.action, "unchanged", "an unchanged file was sent again")
        self.assertEqual(self.platform.updated, [], "a PUT for a file nobody changed")

    def test_an_edit_is_sent_once(self):
        self.sync()
        self.assertEqual(self.sync(body="Edited.").action, "update")
        for _ in range(3):
            self.assertEqual(self.sync(body="Edited.").action, "unchanged", "the edit was sent again")
        self.assertEqual(self.platform.updated, [("1", "Edited.")])


if __name__ == "__main__":
    unittest.main()
