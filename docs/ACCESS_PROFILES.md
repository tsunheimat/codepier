# Workspace Profiles and Connection identities

Profiles belong to one workspace and hold paired resource/action rules.
A Connection requires that workspace and an enabled same-workspace Profile.
Creating two Connections with the same Profile creates two distinct identities.
Token renewal preserves the original Connection ID, not a new grant or shared
Profile identity. `get_profile` returns the Connection ID.

OAuth is workspace selection → Profile selection → current-policy confirmation.
Changing the workspace invalidates the old selection. A create-Profile detour
returns to pending authorization and never automatically grants consent.
Fixed connections cannot expand beyond their original consent; live connections
follow Profile changes only after explicit consent, within current member rights.
No Role grants, account fallback or Profile-less credentials exist.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.

## Visual policy editor

In Workspace → Profiles, create or edit a Profile without writing JSON for ordinary
Project rules. Each Project has a **read + write + execute** shortcut; the separate
listed-project shortcut applies those three actions only to the Projects currently
shown. Neither shortcut selects `computer`, changes VPS permissions or grants
workspace administration. Existing deliberate `computer` selections are retained.

Workspace administrators can select **全部当前及未来 Projects（仅此工作空间）**, choose
its actions and optionally exclude individual Projects. This saves the real
`all_projects` selector, not a snapshot of the visible Project IDs. An empty action
selection is not an allow-all rule. Member permissions, source mappings and Runner
restrictions remain enforced. Future Projects are available only where the member
policy also allows them and the Connection explicitly consented to **live** policy;
existing **fixed** Connections never silently expand.

Simple all-Project rules and exclusions reopen in the visual editor. Mixed rules,
all-VPS selectors and references not represented in the current list remain in
explicit JSON mode, preserving the original policy rather than flattening it into
a broader grant. Switching modes retains the draft; unrelated source, Device,
connector and plugin rules are preserved. This is not an unrestricted
"inherit every member permission" mode.
