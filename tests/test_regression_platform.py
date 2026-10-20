"""Native proof rejects missing, skipped, duplicated and foreign-platform cases."""
from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts import check_native_execution
from scripts.regression_platform import (
    WINDOWS_CASES, WINDOWS_MODULE, WINDOWS_NODES, native_xml_outcomes, read_native_proof,
)


def xml_document():
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite')
    for name in sorted(WINDOWS_CASES):
        ET.SubElement(suite, 'testcase', classname='tests.test_execution_windows', name=name, time='0.5')
    return root, suite


def test_native_pass_evidence_lists_exactly_the_required_actual_cases(tmp_path):
    root, _ = xml_document()
    path = tmp_path/'result.xml'
    ET.ElementTree(root).write(path)
    assert native_xml_outcomes(path) == {node: 'passed' for node in WINDOWS_NODES}


@pytest.mark.parametrize('fault', ['skipped','failed','error','xfail','missing','duplicate','foreign','unrecognized'])
def test_native_xml_has_no_missing_or_skipped_as_success(tmp_path, fault):
    root, suite = xml_document()
    case = suite[0]
    if fault in {'skipped','failed','error','xfail'}:
        name = {'failed':'failure','xfail':'skipped'}.get(fault, fault)
        ET.SubElement(case, name, message='fixture denial', type='pytest.xfail' if fault=='xfail' else 'fixture')
    elif fault=='missing':suite.remove(case)
    elif fault=='duplicate':ET.SubElement(suite, 'testcase', **case.attrib)
    elif fault=='foreign':case.set('classname','tests.test_portable_contracts')
    else:case.set('name','test_not_in_reviewed_native_contract')
    path = tmp_path/'result.xml';ET.ElementTree(root).write(path)
    with pytest.raises(ValueError):native_xml_outcomes(path)


def test_posix_process_cannot_publish_a_native_windows_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(check_native_execution.sys, 'platform', 'linux')
    output = tmp_path/'must-not-exist'
    with pytest.raises(SystemExit) as failure:
        check_native_execution.main(['--output',str(output)])
    assert failure.value.code == 2 and not output.exists()


@pytest.mark.parametrize('kind', ['directory','symlink','large','invalid_json'])
def test_native_proof_requires_bounded_regular_json(tmp_path, kind):
    path=tmp_path/'proof.json'
    if kind=='directory':path.mkdir()
    elif kind=='symlink':
        target=tmp_path/'target.json';target.write_text('{}');path.symlink_to(target)
    elif kind=='large':path.write_bytes(b' '* (129*1024))
    else:path.write_text('not-json')
    with pytest.raises(ValueError):read_native_proof(path)


def test_native_proof_optional_input_is_not_automatically_created(tmp_path):
    assert read_native_proof(None) is None
    path=tmp_path/'proof.json';path.write_text(json.dumps({'verified':False}))
    assert read_native_proof(path)=={'verified':False}


def test_reviewed_native_cases_match_real_collection_without_exclusions(tmp_path):
    import os
    import subprocess
    import sys
    # Collect the real test file, not hand-written acceptance selectors. New or
    # renamed cases require the exact applicability contract to be reviewed.
    script = r'''import json,sys,pytest
class Capture:
 def pytest_collection_finish(self,session):
  print('NATIVE_COLLECTION='+json.dumps([item.nodeid for item in session.items]))
raise SystemExit(pytest.main(['--collect-only','-q','-o','cache_dir='+sys.argv[1],sys.argv[2]],plugins=[Capture()]))
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'cache'),WINDOWS_MODULE],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ,'PYTEST_ADDOPTS':''},
        text=True,capture_output=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    marker=next(line for line in result.stdout.splitlines() if line.startswith('NATIVE_COLLECTION='))
    nodeids=json.loads(marker.split('=',1)[1])
    assert set(nodeids)==WINDOWS_NODES
    assert len(nodeids)==len(WINDOWS_NODES)
