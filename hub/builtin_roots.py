"""Hub-owned root settings, separate from the permanent built-in identity.

Saved paths are canonical names, not symlinks to resolve afresh at startup.
Missing or redirected mounts stay visible in settings and fail closed in the
Agent's access checks; they are never silently removed or granted a new target.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.filesystem import protected, within
from shared.util import DevError

META_KEY = "builtin_agent_roots"
MAX_ROOTS = 32
MAX_PATH = 2048
MAX_REVISION = 2**53 - 1


def _path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_PATH
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid path")
    value.encode("utf-8")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("relative path")
    return path


def _private_paths(store):
    directory = store.directory
    # Include both names and targets, including a separately mounted/symlinked
    # Agent state directory. Do not block the application directory itself.
    paths = (directory, directory / "builtin-agent", directory / "builtin-agent" / "state")
    return {path for item in paths for path in (item, item.resolve())}


def _check_private(path, private_paths):
    if protected(path.as_posix()) or any(within(path, private) or within(private, path)
                                         for private in private_paths):
        raise DevError("PROTECTED_ROOT", "不能授权 Hub 数据、Agent 配置/状态、凭据目录或其上级目录", 403)


def canonical_roots(store, roots):
    """Validate requested roots without creating directories or project maps."""
    if not isinstance(roots, list) or len(roots) > MAX_ROOTS:
        raise DevError("INVALID_ROOTS", "根目录必须是最多 32 项的数组")
    result, seen = [], set()
    try:
        private_paths = _private_paths(store)
        for root in roots:
            if (not isinstance(root, dict) or set(root) != {"path", "writable"}
                    or type(root.get("writable")) is not bool):
                raise ValueError("invalid root")
            path = _path(root["path"]).resolve(strict=True)
            _path(str(path))  # Symlink targets must meet the same text bounds.
            if not path.is_dir():
                raise ValueError("not a directory")
            _check_private(path, private_paths)
            if path in seen:
                raise DevError("DUPLICATE_ROOT", "不能重复授权同一个规范目录")
            seen.add(path)
            result.append({"path": str(path), "writable": root["writable"], "allow_tasks": False})
    except (ValueError, OSError, RuntimeError) as exc:
        raise DevError("INVALID_ROOT", "根目录必须是 Hub 容器内已存在、可解析的绝对目录路径") from exc
    return sorted(result, key=lambda root: root["path"])


def load_settings(store):
    """Return version/revision/roots, or fail closed on a corrupt meta record."""
    row = store.one("SELECT value FROM meta WHERE key=?", (META_KEY,))
    if row is None:
        return {"version": 1, "revision": 0, "roots": []}
    try:
        # Bound decoding too: one persisted record must not consume unbounded
        # work in every runtime admission check.
        if not isinstance(row["value"], str) or len(row["value"]) > 512 * 1024:
            raise ValueError("invalid record")
        settings = json.loads(row["value"])
        if (not isinstance(settings, dict) or set(settings) != {"version", "revision", "roots"}
                or type(settings["version"]) is not int or settings["version"] != 1
                or type(settings["revision"]) is not int or not 0 <= settings["revision"] <= MAX_REVISION
                or not isinstance(settings["roots"], list) or len(settings["roots"]) > MAX_ROOTS):
            raise ValueError("invalid settings")
        seen = set()
        for root in settings["roots"]:
            if (not isinstance(root, dict) or set(root) != {"path", "writable", "allow_tasks"}
                    or type(root["writable"]) is not bool or root["allow_tasks"] is not False):
                raise ValueError("invalid root")
            path = _path(root["path"])
            if str(path) != root["path"] or ".." in path.parts or path in seen:
                raise ValueError("noncanonical or duplicate root")
            _check_private(path, {store.directory})
            seen.add(path)
        return settings
    except (ValueError, TypeError, KeyError, RecursionError, DevError):
        raise DevError("BUILTIN_ROOTS_STATE", "Hub 内置设备根目录设置无效；请恢复原始记录", 409) from None


def save_settings(store, roots, revision, *, actor, device_id):
    """Atomically compare, save and audit. Caller owns device authorization.

    The caller may join this transaction to schedule after-commit reconciliation.
    Canonical no-ops return the existing revision and do not emit an audit event.
    """
    with store.transaction():
        current = load_settings(store)
        if type(revision) is not int or revision != current["revision"]:
            raise DevError("BUILTIN_ROOTS_CONFLICT", "根目录设置已变化，请刷新后重新保存", 409)
        normalized = canonical_roots(store, roots)
        if normalized == sorted(current["roots"], key=lambda root: root["path"]):
            return current
        if revision >= MAX_REVISION:
            raise DevError("BUILTIN_ROOTS_CONFLICT", "根目录设置版本达到上限", 409)
        settings = {"version": 1, "revision": revision + 1, "roots": normalized}
        store.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                      (META_KEY, json.dumps(settings, ensure_ascii=False)))
        store.audit(actor, "device.builtin_roots_updated", device_id,
                    detail={"revision": settings["revision"], "roots": normalized}, target_kind="device")
        return settings
