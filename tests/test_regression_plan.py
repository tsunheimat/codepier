"""Exhaustiveness and failure semantics are executable contracts, not source greps."""
from __future__ import annotations
import copy
from pathlib import Path
import pytest
from scripts.regression_plan import available_workers, classify_events, fingerprint, merge_summaries, plan_jobs, pytest_command
from scripts.regression_platform import WINDOWS_NODES, WINDOWS_SKIP

NODES = ['tests/test_a.py::test_one', 'tests/test_a.py::test_matrix[light]',
         'tests/test_a.py::test_matrix[dark]', 'tests/test_b.py::test_two']
MARKERS = {node: ['isolated_case'] for node in NODES if 'matrix' in node}


def summaries(count=3):
    result = []
    for index in range(count):
        jobs = plan_jobs(NODES, MARKERS, shard_index=index, shard_count=count)
        result.append({'platform': 'linux', 'shard': {'index': index, 'count': count}, 'full_collection': NODES,
                       'collection_sha256': fingerprint(NODES), 'source_inventory_sha256': 'source-fixture',
                       'verified': True, 'outcomes': {n: 'passed' for job in jobs for n in job['nodeids']}})
    return result


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_shards_partition_every_case_exactly_once(count):
    jobs = [job for index in range(count) for job in plan_jobs(NODES, MARKERS, shard_index=index, shard_count=count)]
    actual = [node for job in jobs for node in job['nodeids']]
    assert sorted(actual) == sorted(NODES)
    assert len(actual) == len(set(actual))
    assert len({job['name'] for job in jobs}) == len(jobs)
    leftover = next(job for job in jobs if job['nodeids'] == [NODES[0]])
    assert leftover['selectors'] == [NODES[0]]  # Must not rerun both isolated matrix cases.
    assert merge_summaries(summaries(count))['collected'] == len(NODES)


@pytest.mark.parametrize('problem', ['missing_shard', 'duplicate_shard', 'source', 'collection', 'unverified', 'skipped', 'missing_case', 'duplicate_case', 'unexpected', 'phases', 'process', 'stale'])
def test_merge_rejects_incomplete_or_misleading_evidence(problem):
    reports = copy.deepcopy(summaries())
    first, last = reports[0], reports[-1]
    if problem == 'missing_shard': reports.pop()
    elif problem == 'duplicate_shard': last['shard']['index'] = 0
    elif problem == 'source': last['source_inventory_sha256'] = 'other-source'
    elif problem == 'collection': last['collection_sha256'] = 'bad'
    elif problem == 'unverified': last['verified'] = False
    elif problem == 'skipped': last['outcomes'][next(iter(last['outcomes']))] = 'skipped'
    elif problem == 'missing_case': first['outcomes'].pop(next(iter(first['outcomes'])))
    elif problem == 'duplicate_case': last['outcomes'].update(first['outcomes'])
    elif problem == 'unexpected': last['unexpected_tests'] = ['tests/extra.py::case']
    elif problem == 'phases': last['duplicate_phases'] = ['tests/test_a.py::test_one']
    elif problem == 'process': last['module_failures'] = [{'exit_code': 1}]
    elif problem == 'stale': last['source_changed_during_run'] = ['hub/store.py']
    with pytest.raises(ValueError): merge_summaries(reports)


@pytest.mark.parametrize('count,index', [(0, 0), (65, 0), (1, -1), (2, 2)])
def test_invalid_shard_selection_is_not_silently_empty(count, index):
    with pytest.raises(ValueError): plan_jobs(NODES, shard_index=index, shard_count=count)


def test_bad_collection_and_duplicate_cases_are_rejected():
    with pytest.raises(ValueError): plan_jobs(NODES + [NODES[0]])
    with pytest.raises(ValueError): plan_jobs(['tests/../outside.py::case'])
    assert available_workers() >= 1


def test_serial_regression_marker_marks_only_its_job_exclusive():
    markers = {NODES[3]: ['serial_regression']}
    jobs = plan_jobs(NODES, markers)
    flagged = {node: job['exclusive'] for job in jobs for node in job['nodeids']}
    assert flagged[NODES[3]] is True
    assert all(flagged[node] is False for node in NODES[:3])


def test_browser_and_integration_markers_use_limited_pool_without_exclusive_mode():
    markers = {NODES[0]: ['browser'], NODES[3]: ['integration']}
    jobs = plan_jobs(NODES, markers)
    by_node = {node: job for job in jobs for node in job['nodeids']}
    assert by_node[NODES[0]]['limited'] and not by_node[NODES[0]]['exclusive']
    assert by_node[NODES[3]]['limited'] and not by_node[NODES[3]]['exclusive']


def test_each_job_owns_tmp_cache_xml_and_optional_coverage():
    directory = Path('isolated evidence') / 'case'
    command = pytest_command(['python', '-m', 'pytest'], directory, [NODES[0]], coverage=True)
    assert '--basetemp=' + str(directory / 'tmp') in command
    assert '--junitxml=' + str(directory / 'results.xml') in command
    assert 'cache_dir=' + str(directory / 'cache') in command
    assert '--cov=hub' in command and command[-1] == NODES[0]


@pytest.mark.parametrize('mode,expected', [('passed','passed'),('teardown_failed','failed'),('setup_skipped','skipped'),('xfail','skipped'),('missing','missing')])
def test_no_teardown_skip_or_missing_phase_can_make_a_green_receipt(mode, expected):
    events = [{'when': phase, 'outcome': 'passed'} for phase in ('setup', 'call', 'teardown')]
    if mode == 'teardown_failed': events[-1]['outcome'] = 'failed'
    elif mode == 'setup_skipped': events = [{'when': 'setup', 'outcome': 'skipped'}]
    elif mode == 'xfail': events[1]['xfail'] = True
    elif mode == 'missing': events.pop()
    outcome, duplicates = classify_events([NODES[0]], {NODES[0]: events})
    assert outcome[NODES[0]] == expected and not duplicates
    _, duplicates = classify_events([NODES[0]], {NODES[0]: events + [events[0]]})
    assert duplicates == [NODES[0]]


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32', None, 'unknown'])
def test_native_setup_guard_is_not_a_pass_on_a_different_platform(platform):
    node = sorted(WINDOWS_NODES)[0]
    events = [{'when':'setup','outcome':'skipped','skip_reason':WINDOWS_SKIP},
              {'when':'teardown','outcome':'passed'}]
    outcomes, duplicates = classify_events([node], {node:events}, platform=platform)
    assert outcomes[node] == ('not_applicable' if platform in {'linux','darwin'} else 'skipped')
    assert not duplicates and 'passed' not in outcomes.values()


@pytest.mark.parametrize('fault', ['other_node','other_reason','no_reason','xfail','teardown_failed','missing_teardown','duplicate_setup','skipped_call'])
def test_native_platform_exception_cannot_hide_any_other_skip_or_failure(fault):
    node = sorted(WINDOWS_NODES)[0]
    events = [{'when':'setup','outcome':'skipped','skip_reason':WINDOWS_SKIP},
              {'when':'teardown','outcome':'passed'}]
    if fault=='other_node':node='tests/test_other.py::unrelated'
    elif fault=='other_reason':events[0]['skip_reason']='Skipped: missing runtime library'
    elif fault=='no_reason':events[0].pop('skip_reason')
    elif fault=='xfail':events[0]['xfail']=True
    elif fault=='teardown_failed':events[1]['outcome']='failed'
    elif fault=='missing_teardown':events.pop()
    elif fault=='duplicate_setup':events.append(events[0])
    else:events.insert(1,{'when':'call','outcome':'skipped'})
    outcomes, duplicates = classify_events([node],{node:events},platform='linux')
    assert outcomes[node] not in {'passed','not_applicable'}
    assert bool(duplicates) == (fault=='duplicate_setup')


def reports_with_native():
    nodes=NODES+sorted(WINDOWS_NODES)
    report={'platform':'linux','shard':{'index':0,'count':1},'full_collection':nodes,
        'collection_sha256':fingerprint(nodes),'source_inventory_sha256':'same-source',
        'verified':True,'outcomes':{**{n:'passed' for n in NODES},**{n:'not_applicable' for n in WINDOWS_NODES}}}
    native={'verified':True,'platform':'win32','collected':len(WINDOWS_NODES),
        'source_inventory_sha256':'same-source','source_changed_during_run':[],
        'outcomes':{n:'passed' for n in WINDOWS_NODES}}
    return report,native


def test_complete_proof_needs_real_native_companion_and_counts_it_separately():
    report,native=reports_with_native()
    with pytest.raises(ValueError,match='Windows-native evidence'):merge_summaries([report])
    merged=merge_summaries([report],native_windows=native)
    assert merged['verified'] and merged['native_windows_verified']
    assert merged['counts']=={'passed':len(NODES),'not_applicable':10,'failed':0,'skipped':0,'missing':0}
    assert merged['collected']==len(NODES)+10 and merged['native_windows_tests']==10


@pytest.mark.parametrize('fault', ['wrong_platform','wrong_source','unverified','source_changed','missing','extra','skipped','duplicate_count'])
def test_native_companion_cannot_be_mocked_omitted_or_from_another_build(fault):
    report,native=reports_with_native()
    if fault=='wrong_platform':native['platform']='linux'
    elif fault=='wrong_source':native['source_inventory_sha256']='another-build'
    elif fault=='unverified':native['verified']=False
    elif fault=='source_changed':native['source_changed_during_run']=['agent/execution.py']
    elif fault=='missing':native['outcomes'].pop(next(iter(WINDOWS_NODES)))
    elif fault=='extra':native['outcomes']['tests/test_extra.py::case']='passed'
    elif fault=='skipped':native['outcomes'][next(iter(WINDOWS_NODES))]='skipped'
    else:native['collected']=11
    with pytest.raises(ValueError,match='Windows-native evidence'):
        merge_summaries([report],native_windows=native)


@pytest.mark.parametrize('fault',['other_exception','native_on_windows','mixed_platform','missing_platform','partial_native','native_pass_on_posix'])
def test_aggregation_revalidates_platform_exception_instead_of_trusting_verified_flag(fault):
    report,native=reports_with_native()
    if fault=='other_exception':report['outcomes'][NODES[0]]='not_applicable'
    elif fault=='native_on_windows':report['platform']='win32'
    elif fault=='missing_platform':report.pop('platform')
    elif fault=='partial_native':report['outcomes'][next(iter(WINDOWS_NODES))]='passed'
    elif fault=='native_pass_on_posix':report['outcomes'].update({node:'passed' for node in WINDOWS_NODES})
    else:
        reports=summaries();reports[-1]['platform']='darwin'
        with pytest.raises(ValueError):merge_summaries(reports)
        return
    with pytest.raises(ValueError):merge_summaries([report],native_windows=native)


def test_large_collection_has_linear_node_parsing_budget():
    class CountedNode(str):
        splits = 0

        def split(self, *args, **kwargs):
            type(self).splits += 1
            return super().split(*args, **kwargs)

    nodes = [CountedNode(f'tests/test_module_{module:04d}.py::test_case_{case}')
             for module in range(1000) for case in range(10)]
    jobs = plan_jobs(nodes)
    assert len(jobs) == 1000
    assert sum(len(job['nodeids']) for job in jobs) == len(nodes)
    assert all(job['selectors'] == [job['module']] for job in jobs)
    assert CountedNode.splits <= 3 * len(nodes)
