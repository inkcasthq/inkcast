# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""Review of PR #461 (issue #444, option B): an old unconfirmed entry with no post id is skipped until the file is
edited, and every push tells the writer so.

The acceptance criteria: ``GitSync._one`` skips such an entry "with a message saying the file must be edited". A push
whose lookup the platform rate-limits (``Publisher.confirm``: ``unavailable``) skipped it with "The next sync will ask
again" and nothing else: the writer, failing the run under ``--fail-on-decision``, was not told that the edit is the
only way out when the platform keeps saying it isn't there, nor that it would be a new post."""

import unittest

from test_sync_old_unconfirmed_entry_444 import OldEntry


class FindingARateLimitedLookupStillNamesTheEdit(OldEntry):
    def test_an_unedited_file_whose_lookup_is_refused_for_now_is_told_to_edit_it(self):
        self.old_entry()
        self.platform.refuse = True                                           # a 429 on the lookup: unavailable
        result = self.sync()
        self.assert_waits_for_an_edit(result)
        self.assertIn("Findable couldn't be asked just now (Findable rate limit hit", result.message)
        self.assertEqual(self.platform.lookups, 1)
        self.platform.refuse = False
        self.assert_waits_for_an_edit(self.sync())                            # the next push: told "isn't there", as before
        self.assert_the_edit_is_a_new_post()


if __name__ == "__main__":
    unittest.main()
