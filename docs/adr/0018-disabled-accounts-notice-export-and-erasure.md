# ADR-0018: A cut-off account is told, can export, and is erased after N days

**Status:** accepted, 2026-10-28 (the owner accepted every recommendation in "For the owner") · **Epic:** E9 (Identity on Keycloak), with E3 and E7 · **Issue:** #324 · **Changes:** F9.5.S6's rule that a disabled-but-present person is never erased, and F9.8.S3's "cut off but never erased"

## Context

**The owner's decision on #320, 2026-10-28.** A federated person who leaves the directory group is disabled in Keycloak, not deleted (`removeInvalidUsersEnabled: false`). The owner then said: "we cannot keep data for ever. Give them an option to export out their data and download their data because we are deleting after 30 days. So we need to have cleanup process job that we can parameterize."

**What Inkcast does today** (`main` at `19a7390`):
- **The cut-off.** The user sync cuts off a person Keycloak reports gone (404) or `enabled: false`. It deletes their OAuth tokens, codes and pending consents, revokes their personal access tokens, and sets `users.disabled_at` (`src/inkcast/libs/db/identity.py:142-161`). Only a sign-in clears the mark (`:115`).
- **Who is erased.** A person who is gone is erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` (30) after the reconcile first found them gone (`keycloak_sync.py:426-435`). A person who is disabled but still present is "never erased" (`keycloak_sync.py:31-32`, `:607`; F9.5.S6).
- **The breaker.** A mass 404 or a mass `enabled: false` is held for an operator (F9.5.S7, F9.5.S8).
- **Email.** Inkcast sends none.
- **Export.** None exists yet (F3.3.S7, #61, is planned: a signed-in person's one-time link, 24 h).
- **Scheduled posts.** A cut-off person's scheduled posts are still claimed and sent. The scheduler checks only that the `users` row exists (`src/inkcast/libs/db/posts.py:203`).

**What Keycloak 26.6.7 does** (read from source; [research §4](../research/account-lifecycle-2026-10.md#4-keycloak-2667-read-at-the-tag)):
- **The disabled copy.** With `removeInvalidUsersEnabled: false`, a federated person who no longer validates is kept as a read-only, disabled copy, and nothing in Keycloak removes it later.
- **Returning.** A person who returns while the copy exists keeps their Keycloak id, so they keep their Inkcast account (`oidc:{issuer}#{sub}`).
- **Deleting the copy.** This needs `manage-users`. The sync holds `view-users` and `view-events` only. In a read-only LDAP realm, the delete removes only Keycloak's copy.
- **Workflows.** They cannot do this job: no condition tests "disabled", and no event fires when a person becomes invalid.

**What comparable systems do** ([research §1-2](../research/account-lifecycle-2026-10.md#1-the-comparison)):
- **Disabling and deleting are separate steps.** Deletion runs on a stated clock: Google Workspace 20 days, Microsoft Entra 30, Atlassian 14, Notion 30. After it, coming back is a new account.
- **Export links last 7 days** wherever a vendor states it.
- **Accounts are keyed on an internal id, not on email.**
- **Atlassian** emails the person when the account is deactivated and when deletion is scheduled, and reminds the administrator before it.
- **The GDPR**: storage limitation (Art. 5(1)(e)), with the period stated when data is collected (Art. 13(2)(a)), and a hold "for the establishment, exercise or defence of legal claims" (Art. 17(3)(e)).

**What makes this harder.**
- A disabled person cannot sign in, so an export can reach them only by email.
- Inkcast has no email.
- Deleting a federated person's Keycloak copy can bring back a person an administrator meant to block (research §4.2).

## Options considered

| Option | Why not |
|---|---|
| **1. Keep F9.5.S6: a disabled-but-present person is never erased** | The owner ruled it out: their data would be kept for ever. |
| **2. The reconcile erases a disabled person N days after the cut-off, as it erases a gone one** | Simple, but nobody is told and nothing can be exported. The owner asked for both. |
| **3. A Keycloak Workflow (`notify-user`, then `delete-user` after 30 days)** | No condition tests "disabled", and no event fires on becoming invalid (research §4.3). It would delete only Keycloak's copy, and send Keycloak's mail, not Inkcast's export. |
| **4. A daily cleanup job: it tells the person, offers the export, reminds them, then erases Inkcast's data and removes Keycloak's copy** (chosen) | |

**Where the clock starts:**
- **At the cut-off** (as F9.5.S6 does for a gone person). Rejected: a person could be erased with less than N days' warning if the notice is late, and everyone already disabled at roll-out would be erased at once.
- **At the notice** (chosen). The person always has the full N days from the day they are told.

## Decision

### 1. The states

| State | The `users` row | Sign-in | Data | What moves it on |
|---|---|---|---|---|
| **Active** | `disabled_at` NULL | yes | kept | the sync's cut-off (404 or `enabled: false`, past the breaker), or a confirmed breaker pass |
| **Disabled** | `disabled_at` set, `erase_at` NULL | no | kept | the job's next run: it reads them from Keycloak, sends the notice and schedules them. A legal hold keeps them here |
| **Scheduled for deletion** | `erase_at` set | no | kept; export by an emailed link | `erase_at` passes: the job erases them. They sign in, or Keycloak reports them enabled again: back to active, or disabled until they sign in |
| **Erased** | no row; a `user_erasures` audit row | a new account | gone | — |

- **A self-deletion is not part of this.** A person who deletes their own account (`DELETE_ACCOUNT`, F9.5.S4) is erased at once, as today.
- **A person the breaker holds** (`held_at`, #135) is in none of these states. They are neither cut off nor scheduled until an operator confirms the pass.
- **A lapsed subscription** (the story's third case) does not disable anyone: the organization drops to Free and keeps its data 90 days ([E10 design §2.6](../epics/E10-tiers-and-billing/design.md)). F5.7.S4 owns that clock and reuses the notice and export built here.

### 2. The rule that replaces F9.5.S6's

**Before:** "A disabled-but-present Keycloak user is never erased, in any mode." A person gone from Keycloak was erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` after the reconcile first found them gone. `0` meant at once, `off` meant never.

**After:** **every person Inkcast has cut off is erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` days after the account-cleanup job told them**, whether Keycloak has them disabled or no longer has them. The exceptions:
- Keycloak reports them enabled again before then.
- They sign in before then.
- They are on legal hold.

**Who erases.** The reconcile no longer erases anyone. The job does. A self-deletion still erases at once.

**Which docs change** when F9.5.S9 ships, in the same PR:
- the README;
- the E9 design;
- the alerts runbook;
- the posture check's text.

### 3. Settings

| Setting | Default | Bounds (refused at start-up, naming the variable) | Set explicitly in |
|---|---|---|---|
| `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` (kept; it now covers the disabled too) | `30` | whole days, 14 to 365. `0` and `off` are refused (owner decision 5) | staging, production, `deploy/dev/compose.yaml` (all already `30`) |
| `INKCAST_ACCOUNT_REMINDER_DAYS` (new) | `7` | 1 to 13, and less than the erase-after days | staging, production |
| `INKCAST_EXPORT_LINK_DAYS` (new) | `7` | 1 to 14 | staging, production |

- **The variable keeps its name**, so no deployment has to rename it.
- **The floor of 14** is the shortest window a vendor states (Atlassian). It leaves room for the notice and a reminder a week apart.
- **The ceiling of 365** stops the setting from being a way to keep data for ever. Today's posture check already treats more than ten years as "effectively off".

**Security constants that stay in code:**
- 5 downloads per export link;
- an address must have been the account's for 7 days before it may receive an export link (§6);
- one re-sent link per person per day.

### 4. The account-cleanup job

**How it runs.**
- `inkcast accounts cleanup` runs as the CronJob `inkcast-account-cleanup`: daily, `concurrencyPolicy: Forbid`, on the same image.
- It has the same pod affinity as `inkcast-backup` while the database is SQLite on a ReadWriteOnce volume (`deploy/kustomize/components/backup/cronjob.yaml`).
- `--dry-run` prints what it would do, as counts and ids.
- One run at a time: a lease row in `sync_state`, taken by compare-and-set.

**Each run, in order:**
1. **Stand down while the breaker holds a pass** (`pending_breaker_trip`): do nothing, log it, exit 0. The open pass already alerts.
2. **Schedule.** For each disabled person (no `erase_at`, no legal hold):
   - Read them from Keycloak with the sync's read-only client.
   - Enabled: leave them alone.
   - Disabled or 404: send the notice (§5) with an export link (§6), then set `erase_at = now + days` and `notified_at`.
   - If sending fails, they stay disabled and the next run tries again.
   - A person with no email address is scheduled without a notice, and counted as such.
3. **Remind.** When `erase_at` is less than `INKCAST_ACCOUNT_REMINDER_DAYS` away and `reminded_at` is NULL:
   - Read them from Keycloak first. Enabled: unschedule them, as at step 5.
   - Otherwise send the reminder with a fresh export link.
   - A failed reminder is retried and does not move `erase_at`. The person was told the date at step 2.
4. **The mass guard.** If the people due at step 5 number at least `INKCAST_KEYCLOAK_BREAKER_MIN` and more than `INKCAST_KEYCLOAK_BREAKER_PERCENT` of the people in Inkcast:
   - nobody is erased;
   - the run records a breaker pass of kind `erase`, which `inkcast identity confirm-deletions` or `dismiss-deletions` resolves as today.

   Many people erased at once means a mass cut-off happened N days earlier. Erasing them waits for an operator a second time.
5. **Erase.** For each person whose `erase_at` has passed and who is not on legal hold:
   - **Read them from Keycloak again.** Enabled: clear `erase_at`, `notified_at` and `reminded_at` (they are back). Disabled or 404: go on.
   - **Remove the Keycloak user if owner decision 1 allows it** (§9). Then call `Store.erase_user` with the row as read (`STANDING` gains `erase_at` and the legal hold).
   - **If the sync changed the row in between,** the erasure is refused and the next run erases them.

**It fails closed.**
- Without the sync's client, or when Keycloak cannot be read, the job exits 1 and erases nobody.
- It never erases a person it has not read from Keycloak in the same run.

**It is idempotent.**
- Every step is a compare-and-set on the row.
- A run that dies between sending a notice and recording it sends it again next time: a duplicate, never a skipped notice.

**The reconcile.**
- It stops erasing.
- It does not count a person whose erasure is due among the newly gone. So the job's own Keycloak delete (§9) never trips the breaker.

**The audit.**
- `user_erasures` gains `via` (`cleanup`, `return`, `event`) and `notified_at`, so the lasting record shows the person was told.
- The outbox events `identity.account.scheduled`, `.reminded`, `.erased` and `.keycloak_removed` carry ids and counts only. They are erased with the person.

**Metrics.** These gauges are read from the database by the server, as `inkcast_identity_sync_breaker_open` is:
- `inkcast_identity_accounts{state="disabled|scheduled|legal_hold"}`;
- `inkcast_identity_erasure_overdue`: people more than a day past `erase_at`;
- `inkcast_identity_notice_wait_oldest_seconds`: the oldest cut-off person not yet told.

**Alerts:**
- `InkcastAccountCleanupMissing`: `kube_cronjob_status_last_successful_time` older than 26 h, as `InkcastBackupMissing` does.
- `InkcastAccountErasureOverdue`: above 0 for an hour.
- `InkcastAccountNoticeFailing`: a person has waited more than 2 days for a notice.

Each alert has a runbook section.

**The privacy terms.** The period must be stated where people are told what Inkcast keeps (GDPR Art. 13(2)(a)): the terms at sign-up, and a privacy notice when one exists. The repository has none today.

### 5. Notices

- **The channel.** Email, through the notifier's email channel (F7.4.S4). It goes to the address Keycloak holds when the job reads the person, or, for a person Keycloak no longer has, the last address they signed in with.
- **The first notice is sent when the job schedules them.** It says:
  - the account is disabled or deleted in the sign-in service (never why: Inkcast does not know);
  - the date their data is erased;
  - what is erased, and what stays: posts already published stay on the platforms; backups age out within `INKCAST_BACKUP_KEEP` days;
  - the export link and its lifetime;
  - how to come back before the date: ask whoever manages their sign-in to restore access, then sign in;
  - that after the date a sign-up is a new, empty account.
- **The reminder** is sent `INKCAST_ACCOUNT_REMINDER_DAYS` (7) before the date, with a fresh link. Seven days is Microsoft's stated lead time.
- **No mail after erasure.** Nothing is kept to send it with.
- **No grace period after the reminder.** Erasure does not wait for delivery. The notices are best effort: Inkcast cannot know whether a former employee still reads that mailbox.
- **Without an email channel** (a self-hosted install), people are scheduled without notices. The posture check warns in production and staging.

### 6. Export for a cut-off person

This builds on F3.3.S7 (#61). It is the same schema-versioned JSON bundle, never with credentials or tokens.

- **The link.** Each notice carries a link to `/account/export/<token>`.
  - The token is random (256 bits) and stored only as its hash.
  - It lives `INKCAST_EXPORT_LINK_DAYS` (7), and never past `erase_at`.
  - It allows at most 5 downloads.
- **The download.** Opening the link shows a page. The download is a POST from that page, so a mail scanner that follows links does not use up the downloads.
- **An expired link** offers "send a new link to the address on file", once a day. It never lets the visitor change the address.
- **The email-age rule.** An address that changed within the 7 days before the cut-off gets the notice but no link. The notice says to contact support instead. A taken-over account often has its address changed first.
  - `users.email_since` records when the current address arrived.
  - An operator can also turn the link off for one person: `inkcast accounts export off --user <id> --by <who>`, audited.
- **The audit.** Each download is an outbox event (`identity.account.exported`, ids only). The links are erased with the person.

### 7. Coming back

- **Within the window it is the same account, with nothing to import.**
  - Keycloak keeps the same id while the copy exists: §4.1 of the research for a federated person; a local person is only disabled.
  - Their sign-in clears every mark (`upsert_user` also clears `erase_at`, `notified_at` and `reminded_at`).
  - The job also unschedules anyone Keycloak reports enabled again.
  - Their tokens were revoked at the cut-off, so they make new ones.
- **After erasure it is a new, empty account.** Nothing links it to the old one:
  - the audit row holds a salted hash of the old user id only;
  - a new directory entry, or a removed Keycloak copy, means a new Keycloak id.

  The person's export is their copy. Importing it into a new account is owner decision 7.

### 8. Email and identity changes

- **Inkcast keys on Keycloak's user id** (`oidc:{issuer}#{sub}`), never on email.
  - The email is contact data. It is refreshed at each sign-in.
  - A changed address changes nothing about the account or the clock.
- **Inkcast never links or merges accounts by email.** The rules for a social sign-in that finds an existing account belong to Keycloak's first-broker-login flow (F9.3.S2, refined in the backlog):
  - link only after the person proves the existing account, by an email link or by signing in again;
  - never the AutoLink authenticator, which Keycloak's own guide calls "dangerous in a generic environment".
- **The email stays reserved during the window.** A disabled Keycloak user keeps their email reserved: with `duplicateEmailsAllowed: false`, Keycloak treats another account with that email as a duplicate (research §4.4, read from source for a brokered sign-in; not measured). Once Keycloak's copy is removed, the address is free and a sign-up with it is a new account.
- **A new directory entry is a new person**, even under the same username (a new `entryUUID`, a new Keycloak id).
- **The export and email-age rules of §6 are the email-change rules for a cut-off account.**

### 9. Removing the Keycloak user (owner decisions 1 to 3)

If the owner grants it:
- **The client.** The job deletes the Keycloak user just before Inkcast's erasure, by id, with a separate confidential client `inkcast-cleanup`. It holds realm-management `view-users` and `manage-users`, and its secret is mounted only into the CronJob, never into a long-running pod.
- **Only a person just read as disabled.** The job deletes only a person it has just read as `enabled: false`. Never an enabled user, and never by username.
- **A local (self-sign-up) person is deleted.** Deletion is final, and their email becomes free.
- **A federated person is deleted only when Keycloak no longer lists them in the environment's directory group.**
  - Deleting a member that an administrator disabled in Keycloak would let them sign in again as a new user (research §4.2).
  - Read from source, `GET /admin/realms/{realm}/users/{id}/groups` tells the two apart. F9.5.S10 measures it on OpenShift Local before relying on it:
    - left the group;
    - deleted in LDAP;
    - a member an administrator disabled;
    - LDAP scaled to 0.
  - If the measurement refutes it, the job keeps federated copies. It counts them (`inkcast_identity_keycloak_copies_kept`) for an operator, and the runbook says how to check the directory and delete them in the admin console.
- **Every delete is a Keycloak admin event.** The sync's admin-events pass already reads them. It alerts on any user admin event the cleanup client makes other than a DELETE (`authDetails.clientId`, `AuthDetailsRepresentation.java:27`).
- **The order: Keycloak first, then Inkcast.** A failure between the two leaves an Inkcast row that the next run erases. The other order would leave a Keycloak copy that nothing remembers.

Without the grant:
- the job erases Inkcast's data only;
- it counts the Keycloak copies left (`inkcast_identity_keycloak_copies_kept`);
- a posture check warns in production and staging that Keycloak keeps them.

### 10. Legal hold and backups

- **Legal hold** (F3.3.S9): `inkcast accounts hold --user <id> --ref <matter> --by <who>` and `release`. It sets `users.legal_hold_at` and `legal_hold_ref`.
  - While held, nobody is notified of deletion, scheduled or erased, by any path (`Store.erase_user` refuses a held person, so a self-deletion's erasure waits too).
  - A hold placed on a scheduled person clears `erase_at`.
  - On release the person is disabled again, so the next run notifies them and gives them a full window.
  - Holds are audited, listed (`inkcast accounts holds`) and counted (`state="legal_hold"`).
  - The basis is GDPR Art. 17(3)(e).
- **Backups.**
  - An erased person stays in the daily copies for up to `INKCAST_BACKUP_KEEP` (14) days. A restore re-erases them before anything serves (`reapply_erasures`, #94). This is the ICO's "beyond use".
  - Copies outside the rotation are issue #112.
  - On PostgreSQL (ADR-0014), the point-in-time window takes the place of `INKCAST_BACKUP_KEEP`, and the re-erasure on restore must still hold.
  - A legal hold does not lengthen backup retention.

## For the owner

Each decision has a recommendation. **Answered 2026-10-28: the owner accepted all seven recommendations as written.**

1. **May the cleanup job hold `manage-users`?**
   - **Recommended: yes**, through the separate `inkcast-cleanup` client of §9. Its secret lives only in the CronJob, and every use is an admin event the sync checks.
   - This reverses, for this one job, the E9 design's "Inkcast holds no admin right over users": `manage` also allows changing an email and sending action emails, so the secret could take over accounts.
   - Without it, Keycloak keeps every cut-off person's name and email for ever, unless an operator deletes them by hand.
2. **A federated person's Keycloak copy.** **Recommended:** delete it only when Keycloak no longer lists them in the directory group, after F9.5.S10 measures that this works. Otherwise keep it and report it. Never delete a member who is still in the group: that undoes an administrator's block.
3. **A local (self-sign-up) person's Keycloak copy.** **Recommended: delete it too.** It is the same personal data, and nothing would bring the person back.
4. **A cut-off person's scheduled posts** still go out today, through their connected accounts, until erasure.
   - **Recommended: pause them.** The scheduler does not claim a post whose person has `disabled_at` set. A person the breaker holds keeps publishing, because Keycloak may be wrong there.
   - Nothing paused is sent on the person's return without their action. F9.5.S11 decides what a paused post becomes, within ADR-0015's rules.
5. **The bounds.** **Recommended:** 14 to 365 days, with `0` and `off` refused. A legal hold is the way to keep one person's data.
   - `off` today is a posture warning, not an error.
   - A self-hosted install that wants to keep data would have to set 365, or the owner keeps `off` for installs outside production and staging.
6. **A directory-managed person's export.** **Recommended: send it to the person.** Today every Inkcast account is personal, and the data is their own writing and accounts.
   - Vendors send a managed account's export to its administrator (Atlassian, Microsoft).
   - When E3's organizations exist, an organization's workspace content follows the organization's own clock (F5.7.S4).
7. **Import into a new account.** **Recommended: yes, later**, after F3.3.S7: posts and schedules become drafts, nothing is sent, and no credentials are imported.
   - People who publish from Git already keep their markdown in their repository, so this matters most for posts written through MCP or the API.
   - Only Ghost and Slack state an import of their own export. Notion says it cannot.

## Consequences

**For people:**
- Being disabled is no longer indefinite. A cut-off person is told, can download their data, and is erased N days after they were told.
- A person disabled by mistake has N days for an administrator to restore them. Their account comes back whole.

**For operators and owners:**
- When F9.5.S9 ships, everyone already cut off gets a notice at the first run and a full N days. Nobody is erased by the roll-out itself.
- A person an administrator deleted from Keycloak now gets a notice and an export link too, where today they were erased silently 30 days later.

**For the code:**
- Inkcast gains outbound email (F7.4.S4), which needs an SMTP relay and its network policy in each environment.
- The reconcile is simpler: it cuts off, and the job erases.
- Tests that used `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS=0` move the clock instead.

**Known limit: an outage in a small realm.**
- An LDAP outage makes federated people read `enabled: false`.
- In a realm smaller than `INKCAST_KEYCLOAK_BREAKER_MIN`, they are cut off below the breaker (F9.8.S3).
- If the outage still lasts when their erasure falls due, the job's re-read cannot tell it apart from a real disable, and erases them.
- The notices, the overdue gauges and the runbook are the defence there.

### Build stories

| Story | Issue | Needs |
|---|---|---|
| F7.4.S4 Email to a person: the notifier's SMTP channel | #327 | — |
| F3.3.S8 Export by an emailed link for a cut-off person | #328 | F3.3.S7 (#61), F7.4.S4 |
| F3.3.S9 Legal hold on one person's erasure | #329 | — |
| F9.5.S9 The account-cleanup job, and F9.5.S6's rule replaced | #330 | F7.4.S4, F3.3.S8, F3.3.S9 |
| F9.5.S10 Remove the Keycloak user at erasure | #331 | F9.5.S9, owner decisions 1 to 3, F9.8.S3's measurements |
| F9.5.S11 Pause a cut-off person's scheduled posts | #332 | owner decision 4 |
| F3.3.S10 Import an export bundle into a new account, as drafts | #333 | F3.3.S7, owner decision 7 |

F9.3.S2 (first-broker-login linking) and F9.8.S3 are refined in the backlog to match §8 and §2.

---
<!-- inkcast-nav -->
[Docs](../README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
