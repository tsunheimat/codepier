# Authorization continuity

A CodePier Grant is a durable delegation, not a network connection or a copy of
Project permissions. Adding a Project, refreshing a credential, reconnecting a
transport, or synchronizing an identity must not replace that Grant. Operations,
worktrees, leases and consent continue to belong to the original Grant.

## Lifetimes

These are Hub environment variables. Zero disables the corresponding server-side
expiry; it does not bypass revocation, account checks, or live resource policy.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEPIER_SESSION_IDLE_SECONDS` | `0` | Optional sliding server-side browser-session idle deadline. |
| `CODEPIER_SESSION_MAX_SECONDS` | `0` | Optional maximum duration since authentication, including post-await checks. |
| `CODEPIER_OAUTH_ACCESS_SECONDS` | `3600` | Short-lived MCP access credential; permitted range 300–86400 seconds. |
| `CODEPIER_OAUTH_REFRESH_SECONDS` | `0` | Refresh credential expiry; zero means valid until revoked. A positive value starts a new deadline at each rotation. |
| `CODEPIER_OAUTH_REFRESH_RETRY_SECONDS` | `30` | Fixed duplicate-refresh response-recovery window, 0–120 seconds. Zero selects strict immediate reuse revocation. |

A positive session/refresh duration must be at least 60 seconds and no more than
3650 days. Indefinite refresh lifetime is an explicit deployment tradeoff rather
than the RFC 9700 recommendation to expire inactive refresh credentials; set a
positive refresh duration when that stricter policy is required. Browser credentials remain HttpOnly, SameSite=Lax, Secure on HTTPS.
Cookie retention is capped at 400 days, or a shorter configured absolute session
maximum. Browsers can impose shorter limits or clear cookies. Consequently this
is **not** a promise that a browser can retain login forever.

Ordinary successful same-origin API requests maintain server-side session state;
they do not re-set the credential cookie. Re-setting it on a delayed response can
overwrite a newer account's login in another tab. Only explicit login and SSO
callback responses set the cookie. A browser-cookie retention limit is distinct
from a server-side idle deadline.

Existing valid sessions adopt the configured server policy on use. Deleted,
expired, disabled, or epoch-revoked sessions are never revived. Existing expired
refresh tokens and revoked Grants are not revived either. PAT expiry choices
are unchanged: a PAT with a selected expiry remains finite and has no OAuth
refresh flow.

## OAuth rotation and response loss

A valid refresh rotates exactly once in a database transaction. For the next
30 seconds by default, an identical authorized retry returns the same token pair
from an **encrypted**, persistent receipt, rather than minting another branch or
revoking the Grant. `expires_in` reports the remaining access-token lifetime;
replaying a receipt never restarts its expiry clock. The deadline never slides. Client, resource, scope, account,
identity, profile and role bindings are checked before a cached response is used.
A consumed successor is never returned: a delayed predecessor then receives
`invalid_grant` without revoking a newer valid successor during its retry window.

This is a deliberate availability/security tradeoff: possession of a stolen
bearer refresh token cannot be distinguished from a legitimate retry during this
short window. Configure `CODEPIER_OAUTH_REFRESH_RETRY_SECONDS=0` when strict
immediate replay detection is required. Outside the window, reuse revokes the
rotation family and emits `oauth.refresh_reuse`. Rotated token hashes are kept
for 30 days for replay detection; after cleanup, an unknown old token is denied.
Receipts expire promptly, never contain plaintext tokens on disk, and participate
in the existing encryption-key rotation/backup mechanism.

`oauth.refresh`, `oauth.refresh_retry` and `oauth.refresh_reuse` audits record
outcomes without token values. Revocation is still checked on every request.
Renewing a credential does not authorize resource use: each use rechecks live
membership, identity freshness, roles and resource policy. A paused role continues
to renew credentials but denies its actions until explicitly resumed, using the
same Grant; deleting or rebinding the role still rejects renewal.

## SSO renewal is a separate relationship

There are two independent refresh flows:

1. MCP client → CodePier: CodePier issues and renews the MCP credentials.
2. CodePier → identity provider: the provider issues credentials for verified
   UserInfo and group reconciliation.

New provider configurations request `offline_access`, with explicit consent in
OIDC authorization requests. Explicit administrator-configured scopes are not
silently expanded. Existing panel-managed providers retain their saved scopes.
For Authentik, enable the offline-access scope mapping and allow refresh-token
issuance on the provider. Other providers may use different settings or policies.
A successful login is not proof that a provider supplied a refresh token.

The identity page and `/api/iam/me` expose non-secret renewal diagnostics:
`has_refresh_token`, `sync_code`, `next_attempt`, `checked_at`,
`permissions_fresh`, and `reauthentication_required`. No raw tokens or claims are
returned. A missing refresh token requires a one-time scope/configuration update
and reauthentication; CodePier cannot manufacture an upstream refresh token.

Identity synchronization is serialized per identity, snapshots tokens after
acquiring the lock, and rechecks account/provider/credential generations after
network waits. An unexpectedly rejected access token gets at most one refresh
attempt before reauthentication is required. Minimum-freshness providers are
scheduled before their verified window expires; transient-error backoff is
bounded by the remaining window and never extends that window.

A temporary provider failure leaves the login and Grant intact. Once verified
permissions expire, protected operations **pause fail-closed**, using
`ENTITLEMENTS_UNAVAILABLE` / HTTP 503 and `Retry-After`, not a new CodePier OAuth
consent challenge. The panel retries only context-fenced, read-only workspace
revalidation; it never replays submitted writes or commands. Provider recovery
restores access through the same Grant.

Unsent durable operations retain their payload and operation ID while identity
freshness is unavailable, subject to their original queue deadline. Recovery
rechecks all permissions and mappings before dispatch. Previously attempted work
is only probed during the pause; it is neither replayed nor automatically killed.
Explicit cancellation, deadline expiry and Grant revocation still take effect.

An upstream `invalid_grant` / `invalid_token` means that upstream credential is
unusable. CodePier immediately suspends its derived authority, but retains the
CodePier identity, session and Grant for same-subject reauthentication. This does
not grant continued access while authority is unknown. Actual account disablement,
unlinking, required-group removal, identity mismatch, explicit Grant revocation
and signed logout retain their existing security effects. Updating a provider
label, requested scopes or client secret alone does not delete verified access;
admission, identity-boundary, endpoint, or freshness-policy changes still
invalidate snapshots immediately.

## Deployment and migration

No production deployment or identity-provider setting is changed by this branch.
Merge and build a new image before deploying. Keep `/app/data`, the encryption
key and the public OAuth resource URL stable. The migration adds only an OAuth
receipt table/index; it does not replace user, session, profile or Grant IDs.

After the upgrade, log into the panel once to replace an old 12-hour browser
cookie. For existing SSO configurations without offline access, enable it at both
ends and reauthenticate the same linked identity once. A still-valid MCP Grant
should not need a new consent flow. A Grant already revoked by an older release
cannot be safely resurrected; authorize a new connection in that case.

Browser-cookie retention, unavailable/revoking identity providers, network
interruptions, and clients that do not refresh credentials cannot be eliminated
by making a Grant persistent. Preserve and query existing operation IDs after an
interruption instead of resubmitting a mutation with a new idempotency key.

Rolling back code does not shorten deadlines already extended in the database.
Use an explicit finite policy and/or deliberate session/Grant revocation when
restoring a stricter deployment; do not assume a rollback resets credential TTLs.

## Protocol references

- [OpenID Connect Core: offline access](https://openid.net/specs/openid-connect-core-1_0.html#OfflineAccess)
- [OAuth Security BCP: refresh-token protection](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14)
- [Authentik OAuth provider: refresh-token scopes](https://docs.goauthentik.io/add-secure-apps/providers/oauth2/)
