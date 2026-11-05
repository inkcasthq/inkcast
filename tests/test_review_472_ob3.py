# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""OB3's review of #472 (head dd95bed5): a resend that can't be made must cancel nothing.

#472 refuses a resend whose credentials can't be decrypted before anything is cancelled. Its runbook names the other
cause of the same alert, a payload this version can't read (a newer version wrote it, and this one was rolled back), and
says what happens to such a post on a platform that can't be asked (Medium): the next stuck pass marks it unconfirmed,
and its owner resends it. That resend read the payload only after ``resend_post`` had cancelled the old post and made
the new one: the person got a 500, the old post was cancelled, and a new 'publishing' post holding the same payload was
left for recovery to make unconfirmed again. The payload is read before anything is cancelled now.

Failed on dd95bed5."""

import asyncio
import unittest

from test_person_decides import Harness


class FindingAResendOfAPayloadThisVersionCannotReadCancelsNothing(Harness):
    def unreadable(self) -> str:
        """An unconfirmed post whose payload names a field this version's ``Prepared`` doesn't have (a rollback)."""
        payload = {**self.platform.prepare({"title": "t"}, "Body.").to_dict(), "written_by_a_newer_version": 1}
        return self.store.create_post(group_id="g", user_id=self.user, platform=self.platform.id, title="t",
                                      status="unconfirmed", run_at=0.0, payload=payload)

    def test_the_resend_fails_before_the_old_post_is_cancelled_or_a_new_one_made(self):
        old_id = self.unreadable()
        with self.assertRaises(TypeError):
            asyncio.run(self.service.resend(self.user, old_id))
        self.assertEqual((self.status(old_id), len(self.posts()), self.platform.sent), ("unconfirmed", 1, []),
                         "the old post was cancelled, or a new one made, for a send that could not begin")
        self.assertEqual([t for t, _, _ in self.events(old_id)], [], "the decision was recorded")


if __name__ == "__main__":
    unittest.main()
