"""Deterministic, exhaustive pytest grouping and evidence aggregation.

A shard is not a full-suite verification. Only a merge with every shard,
identical source/collection fingerprints, and exactly-once passing outcomes is.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from scripts.regression_platform import WINDOWS_NODES, not_applicable, validate_native_windows


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def available_workers() -> int:
    return max(1, (getattr(os, "process_cpu_count", os.cpu_count)() or 1))


def plan_jobs(nodeids, markers=None, *, shard_index=0, shard_count=1):
    """Keep module fixtures together, except explicitly independent matrix cases."""
    if not 1 <= shard_count <= 64 or not 0 <= shard_index < shard_count:
        raise ValueError("Require 1 <= shard_count <= 64 and 0 <= shard_index < shard_count")
    if len(set(nodeids)) != len(nodeids):
        raise ValueError("Collection contains duplicate node IDs")
    markers = markers or {}
    grouped: dict[str, list[str]] = {}
    by_module: dict[str, list[str]] = {}
    for nodeid in nodeids:
        module = nodeid.split("::", 1)[0]
        if not module.startswith("tests/") or ".." in Path(module).parts:
            raise ValueError("Unexpected test path")
        by_module.setdefault(module, []).append(nodeid)
        key = nodeid if "isolated_case" in markers.get(nodeid, []) else module
        grouped.setdefault(key, []).append(nodeid)
    jobs = []
    for ordinal, (key, selected) in enumerate(sorted(grouped.items())):
        if ordinal % shard_count != shard_index:
            continue
        module = selected[0].split("::", 1)[0]
        name = Path(module).with_suffix("").as_posix().removeprefix("tests/").replace("/", "__")
        if key != module:
            name += "--" + fingerprint(key)[:16]
        # A module containing isolated cases must select the remaining node IDs,
        # not run the complete module again and duplicate those cases.
        full_module = by_module[module]
        selectors = [module] if selected == full_module else selected
        marker_names = {marker for nodeid in selected for marker in markers.get(nodeid, [])}
        exclusive = "serial_regression" in marker_names
        limited = not exclusive and bool(marker_names & {"browser", "integration"})
        jobs.append({"name": name, "module": module, "nodeids": selected, "selectors": selectors,
                     "exclusive": exclusive, "limited": limited})
    return jobs


def pytest_command(prefix, directory, selectors, *, coverage=False):
    directory = Path(directory)
    command = [*prefix, "-v", "--tb=short", "-o", "cache_dir=" + str(directory / "cache"),
               "--junitxml=" + str(directory / "results.xml"), "--basetemp=" + str(directory / "tmp")]
    if coverage:
        command += ["--cov=agent", "--cov=hub", "--cov=shared", "--cov=scripts", "--cov-report="]
    return command + list(selectors)


def classify_events(expected, records, *, platform=None):
    outcomes, duplicate_phases = {}, []
    for nodeid in expected:
        events = records.get(nodeid, [])
        phases = Counter(event["when"] for event in events)
        if any(count != 1 for count in phases.values()):
            duplicate_phases.append(nodeid)
        if any(event["outcome"] == "failed" for event in events):
            state = "failed"
        elif not_applicable(nodeid, events, platform):
            state = 'not_applicable'
        elif any(event["outcome"] == "skipped" or event.get("xfail") for event in events):
            state = "skipped"
        elif all(any(event["when"] == phase and event["outcome"] == "passed" for event in events)
                 for phase in ("setup", "call", "teardown")):
            state = "passed"
        else:
            state = "missing"
        outcomes[nodeid] = state
    return outcomes, duplicate_phases


def merge_summaries(summaries, *, native_windows=None):
    """Reject missing/duplicate shards, stale sources, omissions and extra tests."""
    if not summaries:
        raise ValueError("No shard reports supplied")
    first = summaries[0]
    count = first["shard"]["count"]
    if len(summaries) != count or {s["shard"]["index"] for s in summaries} != set(range(count)):
        raise ValueError("Missing or duplicate shard reports")
    platform = first.get('platform')
    if platform not in {'linux', 'darwin', 'win32'}:
        raise ValueError('Explicit regression platform is required')
    expected = first["full_collection"]
    if len(expected) != len(set(expected)) or not expected:
        raise ValueError("Invalid full collection")
    outcomes: dict[str, str] = {}
    for summary in summaries:
        if (summary.get('platform') != platform or summary["shard"]["count"] != count or summary["full_collection"] != expected or
                summary["collection_sha256"] != fingerprint(expected) or
                summary["source_inventory_sha256"] != first["source_inventory_sha256"]):
            raise ValueError("Shard source or collection fingerprints differ")
        if not summary.get("verified") or summary.get("source_changed_during_run"):
            raise ValueError("A shard is failed, skipped, incomplete or stale")
        if summary.get("unexpected_tests") or summary.get("duplicate_phases") or summary.get("module_failures"):
            raise ValueError("A shard has unexpected execution evidence")
        if set(outcomes) & summary["outcomes"].keys():
            raise ValueError("A test was executed in more than one shard")
        outcomes.update(summary["outcomes"])
    deferred = {node for node, state in outcomes.items() if state == 'not_applicable'}
    native_cases = set(expected) & WINDOWS_NODES
    if (set(outcomes) != set(expected) or any(value not in {'passed', 'not_applicable'} for value in outcomes.values())
            or deferred and (platform not in {'linux', 'darwin'} or deferred != WINDOWS_NODES)
            or platform in {'linux', 'darwin'} and native_cases and
                (native_cases != WINDOWS_NODES or deferred != WINDOWS_NODES)):
        raise ValueError("Merged test outcomes are missing, unexpected or not passing")
    if deferred:
        # A Linux/macOS skip is not a Windows pass. The full proof needs that
        # actual platform's exact cases and identical source inventory too.
        validate_native_windows(native_windows, first['source_inventory_sha256'], deferred)
    return {"verified": True, "scope": "complete", "platform": platform, "shards": count, "collected": len(expected),
            "counts": {"passed": len(expected)-len(deferred), "not_applicable": len(deferred),
                       "failed": 0, "skipped": 0, "missing": 0},
            "native_windows_verified": bool(deferred),
            "native_windows_tests": len(deferred),
            "collection_sha256": fingerprint(expected),
            "source_inventory_sha256": first["source_inventory_sha256"]}
