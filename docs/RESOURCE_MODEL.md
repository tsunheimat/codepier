# Resources and workspace bindings

> VPS model update (2.1): Runner-approved SSH aliases automatically create global
> VPS resources. Spaces use inline VPS/Device selection lists; VPS permissions are
> Shell selections with an explicit all/future selector. Known workspace-2 VPS
> layouts are converted/reset automatically at startup; the pre-workspace model
> is still refused. The current VPS rules and cutoff are defined in [VPS](VPS.md).

Resources register technical inventory. Workspace bindings determine what is
provided to members and Connections, including paths, permitted devices and VPS
entry placement. Projects reference exactly one workspace source (Folder or Storage Pool); they cannot
select unrelated global inventory. Direct members replace Role assignments.

Navigate directly to `/#spaces/<id>` or a resource detail. No login-level current
workspace or selector header exists. Root resource lists enumerate authorized
inventory; Project and audit lists combine independently authorized workspace views.
Private operation/session records remain connection- or owner-private.

Folder and Storage Pool are peer inventory resources. Import either directly;
a Pool does not need a wrapper Folder. Pool mappings and VPS launchers do not
require or grant independent workspace Device access. Pool file/shell defaults
are configured with the resource and can be inherited or narrowed on import.
Independent Device rules currently expose `devices.read`; allocating a Device
does not create a whole-Device execution target without a Project. Top-level VPS
execution is a separate, explicitly authorized resource operation.

See [Workspace setup and contracts](WORKSPACES.md) for the complete development model.
