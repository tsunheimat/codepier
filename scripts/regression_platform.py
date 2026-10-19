"""Exact native-platform applicability, never arbitrary skip-as-pass.

All cases remain collected and accounted for on POSIX. These ten Windows-native
cases are labelled not_applicable only on Linux/macOS, with the exact native
setup guard and clean teardown. Complete aggregation additionally requires their
actual passing Windows evidence for the identical source inventory.
"""
from __future__ import annotations

import json
from pathlib import Path
import stat
import xml.etree.ElementTree as ET

WINDOWS_MODULE = 'tests/test_execution_windows.py'
WINDOWS_SKIP = 'Skipped: Native Windows acceptance requires Windows'
WINDOWS_CASES = frozenset(
    f'{name}[{shell}]'
    for name, shells in (
        ('test_windows_persistent_shell_keeps_variables_functions_env_cwd', ('powershell', 'pwsh')),
        ('test_windows_persistent_output_is_framed_and_failures_do_not_replay', ('powershell', 'pwsh')),
        ('test_windows_persistent_timeout_and_connection_ownership', ('powershell', 'pwsh')),
        ('test_real_conpty_read_write_resize_and_original_close', ('cmd', 'powershell')),
        ('test_windows_close_terminates_owned_child_tree', ('powershell', 'pwsh')),
    )
    for shell in shells
)
WINDOWS_NODES = frozenset(WINDOWS_MODULE + '::' + name for name in WINDOWS_CASES)


def not_applicable(nodeid, events, platform):
    if platform not in {'linux', 'darwin'} or nodeid not in WINDOWS_NODES or len(events) != 2:
        return False
    phases = {event.get('when'): event for event in events}
    return (set(phases) == {'setup', 'teardown'}
        and phases['setup'].get('outcome') == 'skipped'
        and phases['setup'].get('skip_reason') == WINDOWS_SKIP
        and phases['teardown'].get('outcome') == 'passed'
        and not any(event.get('xfail') for event in events))


def read_native_proof(path):
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 128 * 1024:
        raise ValueError('Native proof must be a bounded regular non-symlink file')
    return json.loads(path.read_text(encoding='utf-8'))


def validate_native_windows(report, source_inventory, required):
    if (set(required) != WINDOWS_NODES or not isinstance(report, dict)
            or report.get('platform') != 'win32' or report.get('verified') is not True
            or report.get('source_inventory_sha256') != source_inventory
            or report.get('source_changed_during_run') != []
            or report.get('outcomes') != {node: 'passed' for node in WINDOWS_NODES}
            or report.get('collected') != len(WINDOWS_NODES)):
        raise ValueError('Actual complete Windows-native evidence for these exact sources is required')


def native_xml_outcomes(path):
    document = ET.parse(path).getroot()
    outcomes = {}
    for case in document.iter('testcase'):
        if case.get('classname') != 'tests.test_execution_windows':
            raise ValueError('Unexpected test in Windows-native acceptance evidence')
        nodeid = WINDOWS_MODULE + '::' + case.get('name', '')
        if nodeid not in WINDOWS_NODES or nodeid in outcomes:
            raise ValueError('Unknown or duplicate Windows-native acceptance test')
        if any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')):
            raise ValueError('Windows-native acceptance cannot fail, skip or xfail')
        outcomes[nodeid] = 'passed'
    if set(outcomes) != WINDOWS_NODES:
        raise ValueError('Windows-native acceptance cases are missing')
    return outcomes
