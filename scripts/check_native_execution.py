#!/usr/bin/env python3
"""Execute all native Windows Session cases and bind real results to the source.

Runs only on Windows; a POSIX invocation cannot mint this evidence. No skip,
xfail, duplicate, omitted case or changing source can produce a verified record.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.check_full_regression import snapshot
from scripts.regression_plan import fingerprint
from scripts.regression_platform import WINDOWS_MODULE, native_xml_outcomes, validate_native_windows, WINDOWS_NODES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if sys.platform != 'win32' or os.name != 'nt':
        parser.error('Actual Windows execution is required; no cross-platform substitute is accepted')
    if output.exists():
        parser.error('Use a fresh Windows-native evidence directory')
    output.mkdir(parents=True)
    before = snapshot()
    result = {'verified': False, 'platform': sys.platform,
              'source_inventory_sha256': fingerprint(before), 'source_changed_during_run': [],
              'collected': 0, 'outcomes': {}}
    try:
        xml = output/'native-sessions.xml'
        command = [sys.executable, '-m', 'pytest', '-q', WINDOWS_MODULE, '--strict-markers',
            '--tb=short', '--junitxml='+str(xml), '--basetemp='+str(output/'tmp'),
            '-o', 'cache_dir='+str(output/'cache')]
        completed = subprocess.run(command, cwd=ROOT,
            env={**os.environ, 'PYTEST_ADDOPTS': ''}, timeout=900)
        if completed.returncode:
            raise ValueError('Native Windows Session acceptance failed')
        outcomes = native_xml_outcomes(xml)
        after = snapshot()
        changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        result.update(outcomes=outcomes, collected=len(outcomes), source_changed_during_run=changed,
                      verified=not changed)
        validate_native_windows(result, fingerprint(before), WINDOWS_NODES)
    finally:
        (output/'native-proof.json').write_text(json.dumps(result, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
