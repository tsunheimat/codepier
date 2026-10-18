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
