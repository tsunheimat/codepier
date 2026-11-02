# Workspace Profiles and Connection identities

Profiles belong to one workspace. A Connection binds that workspace and an enabled
same-workspace Profile immutably. Two Connections using the same Profile retain
distinct identities and private receipts. Token renewal preserves the Connection
ID. OAuth requires workspace selection, Profile selection and current-policy
confirmation; changing workspace invalidates the previous selection.

Member policy is always an upper bound. A Profile cannot select resources outside
its Space, grant local Runner capabilities or acquire administrative rights. No
Role grants, account fallback or Profile-less credentials exist.

## Projects: paired resource/action permissions

In **Space → Profiles**, each Project has read/write/execute controls and a
**read + write + execute** shortcut. The listed-Project shortcut affects only the
currently listed Projects. Neither shortcut selects Computer Use or changes VPS
permissions. Existing explicit Computer selections are retained.

Workspace administrators may select **全部当前及未来 Projects（仅此工作空间）** and
optional exclusions. The stored `all_projects` selector is not a display-list
snapshot. Future Projects are usable only when the member permits them and the
Connection explicitly consents to live Profile policy. Fixed Project consent
still remains bounded to its original resources and target/mapping identities.

## VPS: selected means Shell

VPS rules have **no read/execute classification**. Select individual workspace VPS
or **全部当前及未来 VPS（仅此工作空间）**. A selected VPS authorizes its SSH Shell,
subject to enabled state, Runner-local approval and all other current policy
ceilings. Empty selection grants none. Profiles never select global VPS inventory
directly; the VPS must first be assigned to their Space.

The explicit all/future VPS selector is preserved through policy intersection
only when every applicable ceiling also permits all VPS. It also remains part of
fixed Connection consent; it is not expanded into today's IDs. This deliberately
fixes Connections failing to see newly assigned VPS despite an all-VPS Profile.
Finite fixed selections remain finite and keep their original target fingerprints.
A later Profile edit from finite to all does not broaden an existing fixed
Connection without new consent. This VPS exception does not change fixed Project,
connector or plugin rules.

Only workspace administrators may author future-resource selectors. Delegated
Profile managers cannot use the visual editor or raw JSON to exceed their own
member policy. Revocation and suspended/expired membership still apply immediately.

Simple all-Project and all-VPS selections reopen in the visual editor. Mixed
Project rules or references not represented in the current list stay in explicit
JSON mode rather than being flattened into broader rights. Unrelated Device,
source, connector and plugin rules are preserved when editing either section.

## Cutoff

2.1 rejects old VPS `actions` inputs. Known stored policies are converted
transactionally at Hub startup: execute-capable VPS rules become selections,
read-only VPS rules are dropped rather than elevated. Unchanged all-VPS Profile
consent is repaired only with an eligible current member ceiling. See
[VPS data cutoff and resource model](VPS.md) for scope and restart behavior.
