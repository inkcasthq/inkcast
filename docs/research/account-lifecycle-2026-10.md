# Inkcast: disabled accounts, export and deletion — what comparable systems do

*Research date: 2026-10-28, for issue #324 (the owner's decision on #320, and #61 "Export my data"). Sources:*
- *each vendor's own help pages, privacy policy or terms, read on that date with a web-fetch tool. That tool cuts a quote at
  about 125 characters, so a cut quote ends with "…";*
- *the GDPR (Regulation (EU) 2016/679), as adopted;*
- *Keycloak 26.6.7, the version F9.8's stories cite: its source and its server administration guide, read over git at
  the tag (commit `3db007a`). The repository's own Keycloak manifest pins 26.6.1 (`deploy/keycloak/keycloak.yaml:32`);
  whether these lines differ in 26.6.1 was not checked;*
- *Inkcast `main` at `19a7390`, re-checked at `0f2e332` (#325 added the staging and production realms).*

*"Not stated" means the vendor's own pages that were checked say nothing on the point. It never means "no". A claim that
could not be read on the vendor's own page is flagged **[UNVERIFIED]**. Disagreements between a vendor's own pages are
flagged **[CONFLICT]**. Section 8 lists what could not be read.*

The design that follows from this research is [ADR-0018](../adr/0018-disabled-accounts-notice-export-and-erasure.md).

---

## TL;DR

1. **Disabling an account and deleting it are separate steps everywhere.** Google Workspace, Slack and Atlassian keep a
   suspended or deactivated account's data with no time limit, because it can be restored. A clock starts only at
   deletion:
   - Google Workspace: "up to 20 days";
   - Microsoft Entra ID: "30 days";
   - Atlassian (managed account): "14 days", then personal data "within 30 days";
   - Notion: "30 days", restored through support.

   Inkcast's new rule (a disabled account is erased after 30 days) puts a deletion clock on the disabled state itself.
   That is stricter than these vendors. It is what storage limitation asks for (GDPR Art. 5(1)(e)), as long as the
   period is stated to the person in advance (Art. 13(2)(a)).
2. **An export link lasts 7 days wherever a vendor states it.** GitHub ("seven days"), Notion ("7 days"), Ghost(Pro)
   ("7 days") and Google Takeout ("about 7 days", downloadable "5 times"). The formats are archives of JSON, HTML,
   Markdown or CSV. Microsoft, Slack and Atlassian offer an export to administrators, or to the person on request.
3. **Coming back after deletion is a new account at every vendor.** None of them states a way to restore after the
   window ends. Only Ghost and Slack state an import that accepts their own export. Ghost tells people to keep an archive
   "to reimport your site data if you ever decide to sign back up". Notion says "You can't instantly recreate your
   workspace by reuploading your exported workspace content."
4. **Accounts are keyed on an internal id, not on email.**
   - Microsoft: "use the user objectID as the immutable identifier, rather than UPN or email addresses".
   - Atlassian: content is "associated with the Atlassian account ID, not the email address".
   - Inkcast already does this: `oidc:{issuer}#{sub}`, where `sub` is the Keycloak user id. An email change changes
     nothing about the account.
5. **Notices vary.**
   - Atlassian emails the person at deactivation and again when deletion is scheduled, and reminds the administrator.
   - Microsoft emails the deleted person's manager, with a reminder "Seven days before" OneDrive is deleted.
   - Slack: "People aren't notified when their accounts are deactivated".
   - Google states no notice to the person.
   - The GDPR requires the storage period to be stated when data is collected (Art. 13(2)(a)). It requires no notice
     before deletion at the end of that period.
6. **Backups and legal holds.**
   - Vendors bound how long backups keep deleted data: Slack "up to 14 days", Microsoft "at most 30 days" for customer
     content, Google "up to 6 months".
   - Every vendor with a hold feature lets a hold stop deletion: Google Vault, Microsoft holds, Slack legal holds. The
     GDPR allows it "for the establishment, exercise or defence of legal claims" (Art. 17(3)(e)).
7. **Keycloak 26.6.7 keeps a disabled federated user forever when `removeInvalidUsersEnabled` is false, and nothing in
   it removes them on a clock.**
   - Deleting such a user through the admin API needs `manage-users`, which Inkcast does not hold today.
   - In a read-only LDAP realm, that deletion removes only Keycloak's copy, not the directory entry.
   - Keycloak's Workflows feature has no "disabled" condition or event.

---

## 1. The comparison

Every cell is quoted from the vendor's own page (links in §9, all read on 2026-10-28) or says "not stated". "Re-import"
means an import the vendor states accepts its own export.

| Vendor | Window, and what happens in it | Export: format, link lifetime | Re-import of its own export | Coming back after deletion | Identity key, email change |
|---|---|---|---|---|---|
| **Google Workspace** ([restore][g-restore], [delete][g-delete], [suspend][g-suspend], [Takeout][g-takeout], [org export][g-export], [email change][g-email]) | Suspended: "Email, documents, calendars, and other data aren't deleted"; "you can restore the account at any time". Deleted: "You can restore a user account … up to 20 days after deleting it"; "After 20 days, the data is gone and you can't restore it." | Takeout: Zip or Tgz; "Your archive expires in about 7 days"; "each archive to be downloaded 5 times". Admin: "You can let users export any personal data they want to take with them, before you delete their account." Organization export "automatically deleted 60 days" after it starts. | Not stated | "A deleted user's data is unrecoverable once it's deleted." The address is removed after 20 days; for a personal Google Account "you must wait 30 days". | "You can change a user or guest's work profile name or email address without losing any of their data"; the old address becomes an alias. Internal key: not stated. |
| **Microsoft Entra ID / 365** ([restore][m-restore], [delete a user][m-delete], [OneDrive][m-onedrive], [DSR][m-dsr], [UPN][m-upn]) | "After you delete a user, the account remains in a suspended state for 30 days. During that 30-day window, the user account can be restored, along with all its properties." Then "the permanent deletion process automatically starts and can't be stopped." | Tenant administrator: "export the data in the native JSON format"; "can take up to one month". Link lifetime: not stated. | Not stated | "After a user is permanently deleted, neither you nor Microsoft Support can restore them." "If you permanently delete a user by mistake, you have to create a new user and manually enter all the previous information." | "We recommend developers use the user objectID as the immutable identifier, rather than UPN or email addresses." A UPN change can make a just-in-time app "create a new user profile". |
| **Atlassian Cloud** ([deactivate][a-deactivate], [delete managed][a-delete], [delete own][a-delete-own], [email][a-email]) | Deactivated: "This won't delete personal data … since you can reactivate the account at any time." Deleted: "After 14 days, the account will be permanently deleted"; personal data deleted "within 30 days". The person's own deletion: "a 14-day grace period". | The person: "submit a Subject Access Request (SAR)"; a managed account asks its administrator. Format and link: not stated. | Not stated | "Once the grace period ends, your account will be permanently deleted and you won't be able to restore it." If the person stays in the identity provider, "their Atlassian account will get created again". | "User-created content, product access, and permissions are associated with the Atlassian account ID, not the email address." |
| **Slack** ([deactivate][s-deactivate], [delete profile][s-profile], [export][s-export], [import][s-import], [data requests][s-requests]) | Deactivated: "unable to sign back in"; "does not delete profile information"; reactivation "at any time". Profile deletion is a separate step after deactivation. No clock is stated. | Owners and admins: JSON (Enterprise: "JSON or TXT"), "links to files, but not the files themselves". By the member: not stated. Link lifetime: not stated. | Yes: "import member, message, and channel data from one Slack workspace to another". | "Once profile information is removed, it can't be recovered." | An alias or a typo at sign-in "will create a new account". Internal key: not stated. |
| **GitHub** ([delete][gh-delete], [reference][gh-ref], [archive][gh-archive], [account management][gh-mgmt]) | No disabled state; deletion is one step: "Once your personal account has been deleted, GitHub cannot restore your content." | `tar.gz` emailed as a link; "By default, the download link expires after seven days." Any time, from settings. | Not stated | "Your username will be available for anyone to use after 90 days." "The email address associated with the account becomes available for use with a different account." | "Since an email address can only be associated with a single GitHub account, …" An email can be changed "at any time". Linking a social login by email: not stated. |
| **Notion** ([delete][n-delete], [dormant][n-dormant], [export][n-export]) | "The action is permanent." "We may be able to restore your content within 30 days of deletion." "After 30 days from the date of deletion, your account and content will be permanently lost." **[CONFLICT]**: "may be able to" on one page, "you have 30 days … to contact support and restore" on the other. | HTML, Markdown or CSV in a zip, "at any time"; "This link will expire after 7 days." | No: "You can't instantly recreate your workspace by reuploading your exported workspace content." | Within 30 days, through support. A new sign-up: not stated. | "Each email address can only be associated with one Notion account." Google sign-in works "as long as your Notion account email is the same as your Google email." |
| **Ghost** ([exports][gh-exports], [staff][go-staff], [pause][go-pause], [migration][go-migrate], [DPA][go-dpa]) | Ghost(Pro) cancelled: "your site and account will be deleted at the end of your billing cycle". Deleting a staff user "is irreversible". DPA: personal data deleted "within 90 business days". | JSON (content), CSV (members), zips (themes); Ghost(Pro) emails a link: "The link is valid for 7 days." | Yes: "select export.json, and click Import"; members by CSV. | "Full site archives cannot be provided for Ghost(Pro) accounts that have already been cancelled." Ghost advises an archive "to reimport your site data if you ever decide to sign back up". | A staff user's email "can be altered at any time by the staff user". Linking by email: not stated. |
| **Substack** ([terms][ss-tos], [privacy][ss-privacy]) | "When you delete your account, any Posts associated with that account will also be deleted." Restore window: not stated. | Posts and subscribers as a zip from Settings → Exports **[UNVERIFIED]**. Link lifetime: not stated. | Not stated (its importer's list of sources does not include Substack **[UNVERIFIED]**) | "contact us immediately … but unfortunately, we can't promise that we can recover or restore anything." | Confirmation emails go to both the old and the new address **[UNVERIFIED]**. |
| **Medium** (§8: every help page answered HTTP 403) | Deactivated accounts "can be restored at any time simply by signing in" **[UNVERIFIED]**; deleted: "within 14 days" **[UNVERIFIED]**. | HTML in a zip, link by email **[UNVERIFIED]**. Link lifetime: not stated. | Not stated | "This deletion is permanent" **[UNVERIFIED]** | Sign in with Apple connects "automatically" when the emails match **[UNVERIFIED]**. |
| **DEV (Forem)** ([delete][dev-delete], [account settings][dev-forem], [suspend][dev-suspend]) | Self-deletion "will remove your DEV profile, and all articles, comments, Connect messages, etc." Window: not stated. | "Currently we only support the export of posts and comments." The docs state no format or link. Forem's source sends a zip of `articles.json` and `comments.json` as an email attachment (§2.2). | Not stated | The username becomes "available to anyone". Recovery: not stated. | "you must have one OAuth method connected to your account at all times." |
| **Hashnode** ([terms][hn-terms], [privacy][hn-privacy], [Pro][hn-pro]) | "Upon account deletion, your content will be removed from public view". Window: not stated. | "Every post exports as Markdown"; GitHub backup "as Markdown" (Pro). Link lifetime: not stated. | Not stated (Pro imports "a zip of Markdown files"; whether that accepts its own export is not stated) | Not stated | Not stated |

---

## 2. Findings by question

### 2.1 Deactivation and retention windows

- **Suspension keeps everything, with no clock.**
  - Google: "Email, documents, calendars, and other data aren't deleted", and the account can be restored "at any time"
    ([suspend][g-suspend]).
  - Slack: deactivation "does not delete profile information", and reactivation is possible "at any time"
    ([deactivate][s-deactivate], [reactivate][s-reactivate]).
  - Atlassian: deactivation "won't delete personal data … since you can reactivate the account at any time"
    ([deactivate][a-deactivate]).
- **Deletion has a fixed, stated window, then is final.**
  - Google Workspace: 20 days ([restore][g-restore]).
  - Microsoft Entra ID: 30 days, then deletion "can't be stopped" ([restore][m-restore]).
  - Atlassian: 14 days, and "The grace period can't be expedited" ([delete managed][a-delete]).
  - Notion: 30 days through support ([dormant][n-dormant]).
- **Personal data then takes longer to leave every system.**
  - Atlassian: "within 30 days".
  - Microsoft: "expunged within an additional 30 days" after a permanent delete ([DSR][m-dsr]).
  - Google: "This process generally takes around 2 months from the time of deletion" ([retention][g-retention]).
  - Ghost: "within 90 business days" ([DPA][go-dpa]).
- **[CONFLICT] Microsoft.**
  - The DSR page gives 30 days.
  - Its assurance table allows End User Identifiable Information "at most 180 days" ([assurance][m-assurance]).
  - OneDrive keeps a deleted user's files under its own setting ("30 through 3650" days) plus "93 days" in the recycle
    bin ([OneDrive][m-onedrive], [set retention][m-set-retention]).
- **The closest analogue to Inkcast's new rule is an inactivity policy, not a deactivation.**
  - Notion's dormant-account policy deletes an account after warning "the dormant account will be terminated and deleted
    in thirty days unless there is activity" ([dormant][n-dormant]).
  - Google deletes a consumer account unused "within a 2-year period", after emails ([inactive][g-inactive]).

### 2.2 Export

| Vendor | Who exports | Format | Delivery and lifetime |
|---|---|---|---|
| GitHub | the person, any time | `tar.gz` | emailed link; "By default, the download link expires after seven days"; the person can disable it earlier ([archive][gh-archive]) |
| Notion | the person, any time | HTML, Markdown, CSV, in a zip | emailed link; "This link will expire after 7 days"; "can take up to 30 hours" ([export][n-export]) |
| Ghost(Pro) | owner or administrator, any time | JSON, CSV, zips | emailed link; "The link is valid for 7 days"; self-hosted downloads at once ([exports][gh-exports]) |
| Google (Takeout) | the person; administrators can restrict it | Zip or Tgz | "Your archive expires in about 7 days"; "5 times" ([Takeout][g-takeout]) |
| Google Workspace (organization) | super administrator | zip | "automatically deleted 60 days from the beginning of the export" ([org export][g-export]) |
| Microsoft (Azure DSR) | tenant administrator; the person only in an unmanaged tenant | JSON | to a storage container; "up to one month" ([DSR][m-dsr]) |
| Slack | owners and admins | JSON (Enterprise: JSON or TXT) | emailed when ready; lifetime not stated ([export][s-export]) |
| Atlassian | the person, by a request to support; a managed account asks its administrator | not stated | not stated ([access][a-access]) |
| DEV | the person, from Settings | posts and comments | the docs state nothing more. Forem's source code (github.com/forem/forem at `7762fe2`, not documentation, and possibly not what dev.to runs) sends a zip of `articles.json` and `comments.json` as an email attachment, so no link expires |
| Hashnode | the person | Markdown | not stated ([homepage][hn-home]) |

- **When it is offered.**
  - Google lets administrators offer it "before you delete their account" ([delete][g-delete]).
  - Ghost offers "a full publication archive" before cancelling. None can be provided after: "Full site archives cannot
    be provided for Ghost(Pro) accounts that have already been cancelled" ([pause][go-pause], [backups][go-backups]).
  - Slack's deletion page recommends "exporting your workspace data first" ([delete workspace][s-delete-ws]).
  - No vendor states an export offered to a person whose access an administrator has already removed. At Atlassian and
    Microsoft that person goes through the administrator.

### 2.3 Coming back

- **After the window, it is a new account** at every vendor that states it:
  - Microsoft: "create a new user and manually enter all the previous information" ([restore][m-restore]).
  - Atlassian: "you won't be able to restore it" ([delete own][a-delete-own]).
  - GitHub: "cannot restore your content" ([delete][gh-delete]).
- **Within the window, through the identity provider, it is the same account.**
  - Microsoft, for a deleted user who still exists on-premises: "the sync engine may restore the user during the next
    synchronization cycle" ([restore][m-restore]).
  - Atlassian: "If you don't remove the user from your identity provider, their Atlassian account will get created
    again" ([delete managed][a-delete]).
- **Re-import is rare.**
  - Ghost: `export.json` through "Universal import" ([migration][go-migrate]).
  - Slack: workspace to workspace, "Users with a matching email address in the destination workspace" merged, and
    "Modifying your export file in any way before uploading it may cause issues with your import" ([import][s-import]).
  - Notion says the opposite (§1). The others: not stated.
- **Names and addresses come free after deletion.**
  - GitHub: a username after "90 days"; the email at once ([reference][gh-ref], [account management][gh-mgmt]).
  - Google Workspace: an address after 20 days, and 30 days for a personal account ([delete][g-delete]).

### 2.4 Email and identity changes

- **Keyed on an internal id.**
  - Microsoft says so for developers ([UPN][m-upn]).
  - Atlassian says so for content ([email][a-email]).
  - Google changes an address "without losing any of their data" and keeps the old one as an alias ([email
    change][g-email]).
  - "If an app saves data based on the domain name or the user's email address, the data is lost after the email address
    change" ([impact][g-email-impact]). That is the failure an email key causes.
- **The identity provider is the source of truth for federated people.**
  - Atlassian: "If the user is synced from an identity provider, updates to the account can only be made from the
    identity provider" ([managed changes][a-managed-changes]).
  - Slack updates a SAML user's email "the next time the member signs in" ([email][s-email]).
- **Linking by email is either explicit or limited to a trusted provider.**
  - Notion links Google sign-in when "your Notion account email is the same as your Google email" ([log in][n-login]).
  - GitHub lets the person add a social login's email to an existing account ([authentication][gh-auth]).
  - Keycloak's own guide warns against automatic linking (§4.4).

### 2.5 Notices

| Vendor | At deactivation | Before deletion | At deletion |
|---|---|---|---|
| Atlassian | "we email the user to inform them that their account has been deactivated" | the user: "scheduled for deletion"; the administrator: "We'll email you again in 10 days to remind you" | not stated ([deactivate][a-deactivate], [delete managed][a-delete]) |
| Microsoft (OneDrive) | not stated to the person | the manager: "Seven days before the OneDrive retention period expires, a second email is sent" | not stated ([OneDrive][m-onedrive]) |
| Slack | "People aren't notified when their accounts are deactivated" | not stated | not stated ([deactivate][s-deactivate]) |
| Google Workspace | not stated | not stated | an email to "the user who's receiving the transferred data" ([delete][g-delete]) |
| Notion (dormant accounts) | — | "the account owner is sent an email stating that the dormant account … will be terminated and deleted in thirty days" | not stated ([dormant][n-dormant]) |
| DEV | not stated in the docs | not stated | Forem's source code sends a deleted-account email (§2.2's caveat applies) |

Atlassian notifies at deactivation and when deletion is scheduled, and reminds before deletion: the shape #324 asks for.
Microsoft's 7-day reminder is the only stated reminder lead time.

### 2.6 Backups and legal hold

- **Backups.**
  - Slack: "up to 14 days, but often fewer" ([data requests][s-requests]).
  - Microsoft: customer content "at most 30 days" after deletion ([assurance][m-assurance]).
  - Atlassian: RDS snapshots "retained for 30 days" ([security practices][a-security]).
  - Google: backups "can remain on these systems for up to 6 months" ([retention][g-retention]).
  - Ghost: archived personal data is isolated "from any further processing" ([DPA][go-dpa]).
  - The UK regulator's guidance is to put backup data "beyond use", even if it cannot be overwritten at once
    ([ICO][ico-erasure]).
- **Legal hold.**
  - Google: "You can delete a Google Workspace account only if it's not on hold" ([Vault][g-vault]).
  - Microsoft: "If there is any type of hold on the user mailbox the mailbox can't be removed from Exchange Online"
    ([mailboxes][m-mailbox]).
  - Slack: a legal hold keeps content "regardless of retention settings" ([legal holds][s-holds]).
  - Atlassian keeps "Data that's part of an active lawsuit" ([delete managed][a-delete]).

---

## 3. The GDPR text

The canonical text is on EUR-Lex ([CELEX 32016R0679][eurlex]). From this workspace EUR-Lex answered every request with
an AWS WAF JavaScript challenge (HTTP 202, 2026-10-28), so the wording below was read from the same regulation as adopted
in the Official Journal, republished by The National Archives' legislation.gov.uk ("Original (As adopted by EU)")
([Art. 5][uk5], [Art. 13][uk13], [Art. 17][uk17], [Art. 20][uk20]).

- **Art. 5(1)(e), storage limitation:** personal data shall be "kept in a form which permits identification of data
  subjects for no longer than is necessary for the purposes for which the personal data are processed".
- **Art. 13(2)(a):** at collection the controller provides "the period for which the personal data will be stored, or if
  that is not possible, the criteria used to determine that period".
- **Art. 17(1)(a):** erasure "without undue delay" where "the personal data are no longer necessary in relation to the
  purposes for which they were collected or otherwise processed".
- **Art. 17(3)(e):** paragraphs 1 and 2 "shall not apply to the extent that processing is necessary … for the
  establishment, exercise or defence of legal claims".
- **Art. 20(1):** "the right to receive the personal data concerning him or her, which he or she has provided to a
  controller, in a structured, commonly used and machine-readable format", where the processing is based on consent or
  "on a contract pursuant to point (b) of Article 6(1)" and "carried out by automated means".
- **Art. 20(3):** "The exercise of the right referred to in paragraph 1 of this Article shall be without prejudice to
  Article 17."
- **Art. 12(3):** a request under Articles 15 to 22 is answered "without undue delay and in any event within one month
  of receipt of the request" ([Art. 12][uk12]).

**What follows for Inkcast:**
- A person whose account is disabled no longer uses the service, so their data is "no longer necessary" once the window
  for restoring the account has passed (Art. 5(1)(e), Art. 17(1)(a)).
- The 30-day period must be stated in the privacy notice before it applies (Art. 13(2)(a)). No privacy notice is in the
  repository (`legal/` holds the licence and the CLAs only).
- A JSON bundle is a "structured, commonly used and machine-readable format" (Art. 20(1)).
- A legal hold that stops one person's erasure has its basis in Art. 17(3)(e).
- Nothing in these articles requires a notice before deletion at the end of a stated period. The notices are the
  owner's choice, made for fairness.

---

## 4. Keycloak 26.6.7, read at the tag

### 4.1 A federated person who leaves the group

- **`removeInvalidUsersEnabled` defaults to true.** `UserStorageProviderModel.java:125-133` reads it as `true` when it
  is not set. The owner chose `false` (#320).
- **With it false, an invalid person is kept, read-only and disabled.** `UserStorageManager.deleteFederatedUser`
  (`model/storage-private/.../UserStorageManager.java:177-196`) returns `new ReadOnlyUserModelDelegate(user, false)`
  instead of deleting the local copy. The second argument is `enabled` (`ReadOnlyUserModelDelegate.java:40-42`), so the
  admin API reads `enabled: false`. Nothing in Keycloak removes that copy later.
- **The person comes back to the same Keycloak id while the copy exists.** On import, `LDAPStorageProvider.java:728-731`
  first looks for a local user with the same `LDAP_ID` (the `entryUUID`). Only when there is none does it add a new one,
  with a new id (`:746`). So a person who rejoins the group before the copy is removed keeps their id, and with it their
  Inkcast account (`oidc:{issuer}#{sub}`). A person who rejoins after it is removed gets a new id and a new account.
- **A new directory entry under the same username is a different person to Keycloak.**
  - `loadAndValidateUser` (`LDAPStorageProvider.java:647-669`) loads the directory entry by the stored `LDAP_ID`, not by
    username.
  - `getUserByAttribute` (`UserStorageManager.java:1172-1191`) returns the local copy whenever validation returns
    anything at all. With `removeInvalidUsersEnabled: false`, the old copy validates to the disabled delegate, and that is
    returned for the username.
  - So, read from source and not measured: the recreated person cannot sign in until the old copy is removed. They then
    arrive as a new Keycloak user.
- **An administrator can still disable a federated person locally.** `ReadonlyLDAPUserModelDelegate` overrides the
  setters for username, names, email and attributes, and not `setEnabled` (0 matches). So `enabled: false` on a
  federated person means one of four things:
  - they left the group;
  - an administrator disabled them in Keycloak;
  - the provider is switched off;
  - LDAP is unreachable.

  The admin API answer does not say which.

### 4.2 Removing the Keycloak user

- **The permission.** `DELETE /admin/realms/{realm}/users/{id}` calls `auth.users().requireManage(user)`
  (`services/.../admin/UserResource.java:706-707`). The sync's service account has `view-users` and `view-events` only
  (`deploy/keycloak/realm-inkcast.json` and the staging and production realms generated from it,
  `service-account-inkcast-sync`). The E9 design records that no delete-only grant
  exists, and that `manage` also allows changing the email and sending action emails
  (`docs/epics/E9-identity-keycloak/design.md`, F9.5.S4).
- **What the delete does in a read-only LDAP realm.** `LDAPStorageProvider.removeUser` (`:364-368`) logs that the user
  "can't be deleted in LDAP as editMode is '%s'", deletes "just from Keycloak DB", and returns true. The directory
  entry is untouched. A person who still matches the group filter is imported again at their next sign-in, under a new id.
- **The delete is an admin event.** A successful delete records an `OperationType.DELETE` admin event
  (`UserResource.java:715`), which Inkcast's sync already reads.
- **Who would come back.** A federated person reads `enabled: false` in four cases (§4.1).
  - Deleting the copy of one who left the group, or was deleted in the directory, is final: the directory no longer
    matches them.
  - Deleting the copy of one an administrator disabled in Keycloak, while they are still in the group, lets them sign
    in again as a new, enabled user. The delete undoes the administrator's block.
- **What could tell the two apart (read from source, not measured).**
  - With the group mapper in `READ_ONLY` mode (F9.8.S1), a person Keycloak loads from the directory is wrapped, so
    their groups include the directory's groups (`GroupLDAPStorageMapper.java:646-654`, `:711-719`).
  - A person who no longer validates is returned as the disabled copy (§4.1), without that wrapper. Their groups are
    then the database's only, and `READ_ONLY` mode stores none of the directory's (only `IMPORT` mode does, `:666`).
  - So `GET /admin/realms/{realm}/users/{id}/groups` (`view-users`) should list the environment's group for a member
    an administrator disabled, and not for a person who left.
  - Keycloak skips validation for a cached user (`UserStorageManager.java:152-155`), so the answer can lag the
    directory.

### 4.3 Workflows cannot do it on their own

- Workflows are enabled by default in 26.6.7: `Profile.java:154`, `WORKFLOWS("Workflows", Type.DEFAULT)`.
- They offer `notify-user`, `disable-user` and `delete-user` steps, each with an `after` delay
  (`docs/documentation/server_admin/topics/workflows/defining-steps.adoc`).
- Their conditions are `has-user-attribute`, `has-role`, `has-identity-provider-link` and `is-member-of`
  (`defining-conditions.adoc:39-42`). None tests whether a user is enabled.
- Their events are `user-created`, `user-authenticated`, `user-federated-identity-added` and `-removed`,
  `user-role-granted` and `-revoked`, `user-group-membership-added` and `-removed`, `client-created` and
  `client-authenticated` (`listening-workflow-events.adoc:47-56`). None is a user being disabled or becoming invalid.
- A read-only federated user's attributes cannot be written: `ReadonlyLDAPUserModelDelegate.setAttribute` throws for
  every attribute but `locale` (`ReadonlyLDAPUserModelDelegate.java:77-84`; `INTERNAL_ATTRIBUTES`,
  `LDAPStorageProvider.java:126`). So no "disabled since" mark can be set for a workflow to test.
- A scheduled "inactive users" workflow is a different policy. It would delete people who use Inkcast only through MCP
  tokens or personal access tokens, which never reach Keycloak's login.

### 4.4 Linking by email at first broker login (F9.3.S2)

- **The guide's warning.** `identity-broker/first-login-flow.adoc:9`: "Automatically linking the existing local account
  to the external identity provider is a potential security hole. You cannot always trust the information you get from
  the external identity provider."
- **The AutoLink authenticator.** `:57-59`: "The AutoLink authenticator is dangerous in a generic environment where users
  can register themselves using arbitrary usernames or email addresses."
- **The default flow links only after the person proves the existing account**, by an email link ("Verify Existing
  Account By Email") or by signing in again ("Verify Existing Account By Re-authentication") (`:40-51`).
- **A matching email is a duplicate.** With `duplicateEmailsAllowed: false` (the realm's setting), a brokered sign-in
  whose email matches an existing user is treated as a duplicate, not a new account
  (`IdpCreateUserIfUniqueAuthenticator.java:150-160`).
  - So a disabled user keeps their email reserved. A social sign-in with that address (the realm has no identity
    providers yet; F9.3.S1 adds them) is sent to the linking steps for the disabled account instead of creating a new
    one. That it then fails because the account is disabled is not
    measured.
  - After the Keycloak user is removed, the address is free.
- **An email change does not change the account.** The access token's `sub` is the user id: the `oidc-sub-mapper` sets
  `token.subject(userSession.getUser().getId())` (`SubMapper.java:80`), and the ID token is built the same way
  (`TokenManager.java:1388`). With `UPDATE_EMAIL`, a changed address is applied "Only after following the link and
  confirming the email" when email verification is on (`login-settings/update-email-workflow.adoc`).

---

## 5. Inkcast today

| What | Where | What it does |
|---|---|---|
| Identity key | `src/inkcast/services/identity/login.py:153` | `oidc:{issuer}#{sub}`. Email, display name and name are refreshed at every sign-in, which also clears every cut-off mark (`src/inkcast/libs/db/identity.py:108-128`, line 115). |
| Cut-off | `src/inkcast/libs/db/identity.py:142` `disable_user` | OAuth tokens, codes and pending consents deleted; PATs revoked; `disabled_at` set (`:152-160`). Cleared only by a sign-in. |
| A disabled-but-present person | `src/inkcast/services/identity/keycloak_sync.py:31-32` and `:607` | "never erased"; E9 backlog F9.5.S6: "A disabled-but-present Keycloak user is never erased, in any mode". |
| A person gone from Keycloak | `keycloak_sync.py:426-435` `_grace_over` | erased `INKCAST_KEYCLOAK_ERASE_AFTER_DAYS` (30) after `deleted_at` (`src/inkcast/libs/config.py:641`). |
| The breaker | `keycloak_sync.py:650` `_breaker` | holds a mass 404 or mass `enabled: false` (`INKCAST_KEYCLOAK_BREAKER_MIN` 5, `_PERCENT` 10) for an operator. |
| Erasure | `src/inkcast/libs/db/erasure.py:149` `erase_user` | every table in `USER_TABLES` (`:68-83`) in one transaction, re-checked against the row as read (`STANDING`, `:87`); audit row in `user_erasures` (salted hash, time, counts). |
| Backups | `src/inkcast/libs/config.py:732`, `src/inkcast/libs/db/erasure.py:214` | 14 daily copies (`INKCAST_BACKUP_KEEP`); a restore re-erases anyone in `user_erasures` (`reapply_erasures`). Copies outside the rotation: issue #112. |
| Export | — | none yet: F3.3.S7 (#61) is planned. |
| Email to people | — | none: Inkcast sends no email. F7.4.S2 (a notifier with an SMTP channel) is planned. Keycloak sends its own mail (verification, password reset): `realm-inkcast.json` takes its SMTP settings as placeholders, and the lab's staging and production realms send to a Mailpit catcher in the cluster (`deploy/kustomize/shared/keycloak-realms/mailpit.yaml`, #325). |
| A cut-off person's scheduled posts | `src/inkcast/libs/db/posts.py:203-213` | still claimed and sent. `_OWNED` checks only that the `users` row exists. Outside the tests, `disabled_at` is read only by token lookups (`_ACTIVE_USER`, `src/inkcast/libs/db/identity.py:99`), the erasure's re-check (`STANDING`) and the sync. Git sync stops, because it needs the person's token. |

---

## 6. The owner's questions, answered

The owner, on 2026-10-28: "Does this means they can import again if they resign up. So it means what happens if they
change email. Obviously we cannot control."

### Can they import again if they sign up again?

- **Within the 30 days, nothing is lost and nothing needs importing.** An administrator re-enables them, or they rejoin
  the directory group. Keycloak keeps the same id (§4.1), so the same Inkcast account comes back. They sign in, which
  clears the cut-off, and make new personal access tokens: the old ones were revoked.
- **After the 30 days, a sign-up is a new, empty account.** Inkcast keeps only the erasure audit: a salted hash of the
  old user id, the time and the row counts (`user_erasures`, and `user_erasure_aliases` for a hash under an earlier
  key). It holds no name, email or Keycloak id. This is the same at every vendor (§2.3). The export is the person's
  copy. Importing it into the new account is a feature Inkcast would have to build: only Ghost and Slack state one
  (§2.3).
- **What an import can sensibly restore.**
  - Posts' markdown, as drafts or as history. Never sent again: a post already published would be published twice.
  - Schedules, as drafts to confirm.
  - Not connected platforms or personal access tokens: the export never holds credentials (#61), so platforms are
    connected again.
- **An old export needs no link to the new account.** The person uploads their own file into their own account. Inkcast
  cannot prove the file came from the same person (the old identity is gone), and need not: the file's contents are
  what the person could type in themselves.

### What happens if they change their email?

- **Inside Inkcast, nothing.** The account is keyed on Keycloak's user id. The email Inkcast holds is refreshed at the
  next sign-in (§5), whether the change came from the person, an administrator, or the directory's `mail` attribute.
- **For the notices, Inkcast uses the email Keycloak holds when it reads the person.** For a person Keycloak no longer
  has, it uses the last one they signed in with.
- **What Inkcast cannot control.** A person who left an organisation often loses that mailbox. A notice sent there is
  delivered to a closed mailbox, or to whoever the organisation lets read it. Inkcast can only send to the address it
  has. Deletion therefore does not wait for delivery: the notice is best effort (ADR-0018).
- **A new directory entry is a new identity.** A new LDAP entry under the same username has a new `entryUUID`, so it is
  a new person to Keycloak (§4.1). Their old Inkcast account cannot follow them except through export and import.

### What can we control, and what can't we?

| We control | We cannot control |
|---|---|
| How long Inkcast keeps a disabled person's data, and that it is erased | Whether the person still reads the mailbox the notice goes to |
| When they are told, what the notice says, and the export it offers | Whether the directory or an administrator re-enables them in time |
| Removing Keycloak's copy (if the owner grants the permission, ADR-0018) | The directory's own copy (LDAP is read-only to Keycloak and Inkcast) |
| How long backups keep erased data, and re-erasing on restore | Copies the person or others made: published articles on the platforms, downloaded exports |
| Holding one person's erasure for a legal matter | Whether a returning person brings their export back |

---

## 7. What this research does not settle

These are the owner's decisions. ADR-0018 recommends an answer for each:
- whether Inkcast may hold `manage-users` to remove Keycloak's copy;
- whether a cut-off person's scheduled posts keep going out;
- whether an import is built;
- whether a directory-managed (organisation) account's export should go to the person or to the organisation. No vendor
  that states it sends a managed account's export to the departed person directly (§2.2).

---

## 8. What could not be read

- **EUR-Lex**: an AWS WAF JavaScript challenge (HTTP 202) on every request; the text was read from legislation.gov.uk's
  as-adopted copy (§3).
- **Medium** (help.medium.com, policy.medium.com): HTTP 403 on every page. Every Medium cell is **[UNVERIFIED]**: its
  wording comes from search-result excerpts, not the page.
- **Substack support** (support.substack.com): HTTP 403. The terms and privacy policy were read directly; support-page
  claims are **[UNVERIFIED]**.
- **Hashnode docs and support** (docs.hashnode.com, support.hashnode.com): DNS failure (NXDOMAIN) on 2026-10-28.
- **DEV**: the export and deletion texts sit behind sign-in; the docs state neither the export's format nor a notice.
  What §2.2 says about delivery is read from Forem's source, which may differ from what dev.to runs.
- **Atlassian**: a "15-day" figure for billing contacts appeared only in a search summary; every page read says 14 days.
- **Google Workspace Admin Help** moved to knowledge.workspace.google.com; links below are the pages read after the
  redirect.
- **Keycloak**: §4 is read from source and the guide, not measured. In particular these are not measured on OpenShift
  Local:
  - the admin API's answer for an invalid federated user;
  - a delete of one;
  - a social sign-in against a disabled account's email.

  F9.8.S3 (#320) measures the first. ADR-0018's stories measure the others.

---

## 9. Sources (all read 2026-10-28)

**Regulation and guidance**
- `eurlex`: [EUR-Lex, Regulation (EU) 2016/679](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679) (the canonical text; not readable from here)
- `uk5`: [Art. 5, as adopted](https://www.legislation.gov.uk/eur/2016/679/article/5/adopted)
- `uk12`: [Art. 12, as adopted](https://www.legislation.gov.uk/eur/2016/679/article/12/adopted)
- `uk13`: [Art. 13, as adopted](https://www.legislation.gov.uk/eur/2016/679/article/13/adopted)
- `uk17`: [Art. 17, as adopted](https://www.legislation.gov.uk/eur/2016/679/article/17/adopted)
- `uk20`: [Art. 20, as adopted](https://www.legislation.gov.uk/eur/2016/679/article/20/adopted)
- `ico-erasure`: [Information Commissioner's Office, Right to erasure](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-erasure/)

**Keycloak 26.6.7** (git tag `26.6.7`, commit `3db007abffdcfe01b66e2979de917ef43a9c27c8`, https://github.com/keycloak/keycloak):
- `model/storage/src/main/java/org/keycloak/storage/UserStorageProviderModel.java`
- `model/storage-private/src/main/java/org/keycloak/storage/UserStorageManager.java`
- `federation/ldap/src/main/java/org/keycloak/storage/ldap/LDAPStorageProvider.java` and `ReadonlyLDAPUserModelDelegate.java`
- `federation/ldap/src/main/java/org/keycloak/storage/ldap/mappers/membership/group/GroupLDAPStorageMapper.java`
- `server-spi-private/src/main/java/org/keycloak/models/utils/ReadOnlyUserModelDelegate.java`
- `services/src/main/java/org/keycloak/services/resources/admin/UserResource.java`
- `services/src/main/java/org/keycloak/protocol/oidc/TokenManager.java` and `mappers/SubMapper.java`
- `services/src/main/java/org/keycloak/authentication/authenticators/broker/IdpCreateUserIfUniqueAuthenticator.java`
- `common/src/main/java/org/keycloak/common/Profile.java`
- `docs/documentation/server_admin/topics/identity-broker/first-login-flow.adoc`
- `docs/documentation/server_admin/topics/workflows/defining-steps.adoc`, `defining-conditions.adoc` and `listening-workflow-events.adoc`
- `docs/documentation/server_admin/topics/login-settings/update-email-workflow.adoc`

**Google**
- `g-restore`: [Restore a recently deleted user](https://knowledge.workspace.google.com/admin/users/restore-a-recently-deleted-user?hl=en)
- `g-delete`: [Delete or remove a user from your organization](https://knowledge.workspace.google.com/admin/users/delete-or-remove-a-user-from-your-organization?hl=en)
- `g-suspend`: [Suspend a user temporarily](https://knowledge.workspace.google.com/admin/users/suspend-a-user-temporarily?hl=en)
- `g-takeout`: [How to download your Google data](https://support.google.com/accounts/answer/3024190?hl=en)
- `g-export`: [Export all your organization's data](https://knowledge.workspace.google.com/admin/migrate/export-all-your-organizations-data?hl=en)
- `g-email`: [Changing a Directory user's name or email address](https://knowledge.workspace.google.com/admin/users/overview-changing-a-directory-users-name-or-email-address)
- `g-email-impact`: [Impact on Google apps when changing a user's email address](https://knowledge.workspace.google.com/admin/users/impact-on-google-apps-when-changing-a-users-email-address)
- `g-retention`: [How Google retains data we collect](https://policies.google.com/technologies/retention?hl=en)
- `g-inactive`: [Inactive Google Account Policy](https://support.google.com/accounts/answer/12418290?hl=en)
- `g-vault`: [Delete a user with data on hold](https://knowledge.workspace.google.com/vault/holds/delete-a-user-with-data-on-hold?hl=en)

**Microsoft**
- `m-restore`: [Restore or permanently remove recently deleted user](https://learn.microsoft.com/en-us/entra/fundamentals/users-restore)
- `m-delete`: [Delete a user from your organization](https://learn.microsoft.com/en-us/microsoft-365/admin/add-users/delete-a-user?view=o365-worldwide)
- `m-onedrive`: [OneDrive retention and deletion](https://learn.microsoft.com/en-us/sharepoint/retention-and-deletion)
- `m-set-retention`: [Set the OneDrive retention for deleted users](https://learn.microsoft.com/en-us/sharepoint/set-retention)
- `m-dsr`: [Azure Data Subject Requests for the GDPR and CCPA](https://learn.microsoft.com/en-us/compliance/regulatory/gdpr-dsr-azure)
- `m-upn`: [Plan and troubleshoot User Principal Name changes](https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/howto-troubleshoot-upn-changes)
- `m-assurance`: [Data retention, deletion, and destruction in Microsoft 365](https://learn.microsoft.com/en-us/compliance/assurance/assurance-data-retention-deletion-and-destruction-overview)
- `m-mailbox`: [Delete or restore user mailboxes in Exchange Online](https://learn.microsoft.com/en-us/exchange/recipients-in-exchange-online/delete-or-restore-mailboxes)

**Atlassian**
- `a-deactivate`: [Deactivate a managed account](https://support.atlassian.com/user-management/docs/deactivate-a-managed-account/)
- `a-delete`: [Delete a managed account](https://support.atlassian.com/user-management/docs/delete-a-managed-account/)
- `a-delete-own`: [Delete your Atlassian account](https://support.atlassian.com/atlassian-account/docs/delete-your-atlassian-account/)
- `a-access`: [Access your personal data](https://support.atlassian.com/atlassian-account/docs/access-your-personal-data/)
- `a-email`: [Update your Atlassian account email addresses](https://support.atlassian.com/atlassian-cloud/kb/change-atlassian-account-email-addresses/)
- `a-managed-changes`: [Make changes to a managed account](https://support.atlassian.com/user-management/docs/make-changes-to-a-managed-user-account/)
- `a-security`: [Security Practices](https://www.atlassian.com/trust/security/security-practices)

**Slack**
- `s-deactivate`: [Deactivate a member's account](https://slack.com/help/articles/204475027-Deactivate-a-members-account)
- `s-reactivate`: [Reactivate a member's account](https://slack.com/help/articles/360002061747-Reactivate-a-members-account)
- `s-profile`: [Delete profile information from Slack](https://slack.com/help/articles/360000360443-Delete-profile-information-from-Slack)
- `s-export`: [Export your workspace data](https://slack.com/help/articles/201658943-Export-your-workspace-data)
- `s-import`: [Import data from one Slack workspace to another](https://slack.com/help/articles/217872578-Import-data-from-one-Slack-workspace-to-another)
- `s-delete-ws`: [Delete a workspace](https://slack.com/help/articles/204067366-Delete-a-workspace)
- `s-email`: [Change a member's email address](https://slack.com/help/articles/225531168-Change-a-members-email-address)
- `s-requests`: [Data Request Overview](https://slack.com/trust/data-requests)
- `s-holds`: [Create and manage legal holds](https://slack.com/help/articles/4401830811795-Create-and-manage-legal-holds)

**GitHub**
- `gh-delete`: [Deleting your personal account](https://docs.github.com/en/account-and-profile/how-tos/account-management/deleting-your-personal-account)
- `gh-ref`: [Personal account reference](https://docs.github.com/en/account-and-profile/reference/personal-account-reference)
- `gh-mgmt`: [Personal account management](https://docs.github.com/en/account-and-profile/concepts/account-management)
- `gh-archive`: [Requesting an archive of your personal account's data](https://docs.github.com/en/get-started/archiving-your-github-personal-account-and-public-repositories/requesting-an-archive-of-your-personal-accounts-data)
- `gh-auth`: [About authentication to GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)

**Notion**
- `n-delete`: [Delete your Notion account](https://www.notion.com/help/delete-your-account)
- `n-dormant`: [Notion dormant account policy](https://www.notion.com/help/dormant-account-policy)
- `n-export`: [Export your Notion content](https://www.notion.com/help/export-your-content)
- `n-login`: [Log in & out of Notion](https://www.notion.com/help/log-in-and-out)

**Ghost**
- `gh-exports`: [Exporting content and data](https://ghost.org/help/exports/)
- `go-staff`: [Managing staff user profiles](https://ghost.org/help/managing-staff-user-profiles/)
- `go-pause`: [Can I pause my Ghost(Pro) subscription and publication?](https://ghost.org/help/pause-ghost-pro-subscription/)
- `go-backups`: [Does Ghost automatically back up my data?](https://ghost.org/help/automatic-back-ups/)
- `go-migrate`: [Migrating from Ghost To Ghost](https://docs.ghost.org/migration/ghost)
- `go-dpa`: [GDPR Data Processing Agreement](https://ghost.org/dpa/)

**Substack, DEV, Hashnode**
- `ss-tos`: [Substack Terms of Use](https://substack.com/tos)
- `ss-privacy`: [Substack Privacy Policy](https://substack.com/privacy)
- `dev-delete`: [Delete your DEV Account](https://dev.to/help/delete-account)
- `dev-forem`: [Forem account settings](https://admin.forem.com/docs/forem-basics/user-settings/account-settings)
- `dev-suspend`: [Forem, Suspending Users](https://admin.forem.com/docs/managing-your-community/suspending-users)
- `hn-terms`: [Hashnode Terms of Use](https://hashnode.com/terms)
- `hn-privacy`: [Hashnode Privacy Policy](https://hashnode.com/privacy-policy)
- `hn-pro`: [Hashnode Pro](https://hashnode.com/pro)
- `hn-home`: [Hashnode](https://hashnode.com/)

[eurlex]: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679
[uk5]: https://www.legislation.gov.uk/eur/2016/679/article/5/adopted
[uk12]: https://www.legislation.gov.uk/eur/2016/679/article/12/adopted
[uk13]: https://www.legislation.gov.uk/eur/2016/679/article/13/adopted
[uk17]: https://www.legislation.gov.uk/eur/2016/679/article/17/adopted
[uk20]: https://www.legislation.gov.uk/eur/2016/679/article/20/adopted
[ico-erasure]: https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-erasure/
[g-restore]: https://knowledge.workspace.google.com/admin/users/restore-a-recently-deleted-user?hl=en
[g-delete]: https://knowledge.workspace.google.com/admin/users/delete-or-remove-a-user-from-your-organization?hl=en
[g-suspend]: https://knowledge.workspace.google.com/admin/users/suspend-a-user-temporarily?hl=en
[g-takeout]: https://support.google.com/accounts/answer/3024190?hl=en
[g-export]: https://knowledge.workspace.google.com/admin/migrate/export-all-your-organizations-data?hl=en
[g-email]: https://knowledge.workspace.google.com/admin/users/overview-changing-a-directory-users-name-or-email-address
[g-email-impact]: https://knowledge.workspace.google.com/admin/users/impact-on-google-apps-when-changing-a-users-email-address
[g-retention]: https://policies.google.com/technologies/retention?hl=en
[g-inactive]: https://support.google.com/accounts/answer/12418290?hl=en
[g-vault]: https://knowledge.workspace.google.com/vault/holds/delete-a-user-with-data-on-hold?hl=en
[m-restore]: https://learn.microsoft.com/en-us/entra/fundamentals/users-restore
[m-delete]: https://learn.microsoft.com/en-us/microsoft-365/admin/add-users/delete-a-user?view=o365-worldwide
[m-onedrive]: https://learn.microsoft.com/en-us/sharepoint/retention-and-deletion
[m-set-retention]: https://learn.microsoft.com/en-us/sharepoint/set-retention
[m-dsr]: https://learn.microsoft.com/en-us/compliance/regulatory/gdpr-dsr-azure
[m-upn]: https://learn.microsoft.com/en-us/entra/identity/hybrid/connect/howto-troubleshoot-upn-changes
[m-assurance]: https://learn.microsoft.com/en-us/compliance/assurance/assurance-data-retention-deletion-and-destruction-overview
[m-mailbox]: https://learn.microsoft.com/en-us/exchange/recipients-in-exchange-online/delete-or-restore-mailboxes
[a-deactivate]: https://support.atlassian.com/user-management/docs/deactivate-a-managed-account/
[a-delete]: https://support.atlassian.com/user-management/docs/delete-a-managed-account/
[a-delete-own]: https://support.atlassian.com/atlassian-account/docs/delete-your-atlassian-account/
[a-access]: https://support.atlassian.com/atlassian-account/docs/access-your-personal-data/
[a-email]: https://support.atlassian.com/atlassian-cloud/kb/change-atlassian-account-email-addresses/
[a-managed-changes]: https://support.atlassian.com/user-management/docs/make-changes-to-a-managed-user-account/
[a-security]: https://www.atlassian.com/trust/security/security-practices
[s-deactivate]: https://slack.com/help/articles/204475027-Deactivate-a-members-account
[s-reactivate]: https://slack.com/help/articles/360002061747-Reactivate-a-members-account
[s-profile]: https://slack.com/help/articles/360000360443-Delete-profile-information-from-Slack
[s-export]: https://slack.com/help/articles/201658943-Export-your-workspace-data
[s-import]: https://slack.com/help/articles/217872578-Import-data-from-one-Slack-workspace-to-another
[s-delete-ws]: https://slack.com/help/articles/204067366-Delete-a-workspace
[s-email]: https://slack.com/help/articles/225531168-Change-a-members-email-address
[s-requests]: https://slack.com/trust/data-requests
[s-holds]: https://slack.com/help/articles/4401830811795-Create-and-manage-legal-holds
[gh-delete]: https://docs.github.com/en/account-and-profile/how-tos/account-management/deleting-your-personal-account
[gh-ref]: https://docs.github.com/en/account-and-profile/reference/personal-account-reference
[gh-mgmt]: https://docs.github.com/en/account-and-profile/concepts/account-management
[gh-archive]: https://docs.github.com/en/get-started/archiving-your-github-personal-account-and-public-repositories/requesting-an-archive-of-your-personal-accounts-data
[gh-auth]: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github
[n-delete]: https://www.notion.com/help/delete-your-account
[n-dormant]: https://www.notion.com/help/dormant-account-policy
[n-export]: https://www.notion.com/help/export-your-content
[n-login]: https://www.notion.com/help/log-in-and-out
[gh-exports]: https://ghost.org/help/exports/
[go-staff]: https://ghost.org/help/managing-staff-user-profiles/
[go-pause]: https://ghost.org/help/pause-ghost-pro-subscription/
[go-backups]: https://ghost.org/help/automatic-back-ups/
[go-migrate]: https://docs.ghost.org/migration/ghost
[go-dpa]: https://ghost.org/dpa/
[ss-tos]: https://substack.com/tos
[ss-privacy]: https://substack.com/privacy
[dev-delete]: https://dev.to/help/delete-account
[dev-forem]: https://admin.forem.com/docs/forem-basics/user-settings/account-settings
[dev-suspend]: https://admin.forem.com/docs/managing-your-community/suspending-users
[hn-terms]: https://hashnode.com/terms
[hn-privacy]: https://hashnode.com/privacy-policy
[hn-pro]: https://hashnode.com/pro
[hn-home]: https://hashnode.com/

---
<!-- inkcast-nav -->
[Docs](../../docs/README.md) · [Licensing](../../LICENSING.md) · [Legal](../../legal/README.md) · [Contributing](../../CONTRIBUTING.md) · [Security](../../SECURITY.md) · [Inkcast README](../../README.md)
