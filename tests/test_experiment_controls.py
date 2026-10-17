import json
from pathlib import Path
import pytest
from cyber_agent_flow_orchestrator.experiment_controls import validate, apply, catalogue
from cyber_agent_flow_orchestrator.evaluation_tasks import validate_tasks
from cyber_agent_flow_orchestrator.samples import SampleRequestError
from cyber_agent_flow_orchestrator import trial_reset

RUBRIC = {
    "version": 1,
    "criteria": [
        dict(
            id="read",
            requirement="Read the configuration.",
            evidence="Successful tool output showing its contents.",
        )
    ],
}


def test_judge_task_needs_no_flag_or_exact_answer():
    task = dict(
        id="read-config",
        family="configuration",
        prompt="Find and inspect the configuration.",
        required_checks=["services"],
        verification_mode="judge",
        rubric=RUBRIC,
    )
    assert validate_tasks([task])[0] == task
    with pytest.raises(SampleRequestError, match="without an exact"):
        validate_tasks([dict(task, verifier={"type": "json_equals", "expected": {}})])


def test_conditions_select_configured_catalogs_and_freeze_guidance(tmp_path):
    runtime = {"conditions": []}
    cfg = {}
    value = dict(
        order_seed=19,
        reset_each_trial=True,
        conditions=[
            dict(
                id="baseline",
                source="baseline",
                tools=["curl"],
            ),
            dict(
                id="guided",
                source="http-helper",
                tools=["curl", "http_flag_walk"],
                guidance="Inspect the service.",
            ),
        ],
    )
    apply(value, runtime, cfg, tmp_path, {"conditions": []})
    assert cfg["reset_each_trial"] and runtime["order_seed"] == 19
    assert all("provide_progressive_hints" not in row for row in runtime["conditions"])
    assert "helper" not in runtime["conditions"][1]
    assert (
        Path(runtime["conditions"][1]["guidance_files"][0]).read_text()
        == "Inspect the service."
    )
    value["conditions"][0]["source"] = "../../private"
    with pytest.raises(SampleRequestError, match="configured"):
        validate(value, {"conditions": []})


def test_helper_catalog_metadata_is_separate_from_runtime_condition():
    helper = catalogue({"conditions": []})["http-helper"]
    assert helper["helper"]["name"] == "HTTP flag walker"
    assert helper["helper"]["kind"] == "tool bundle"
    assert helper["tools"][-1] == "http_flag_walk"


def test_dynamic_participant_catalog_controls_tools_and_dangerous_approval(tmp_path):
    catalog = tmp_path / 'participant-tools.json'
    catalog.write_text(json.dumps({'tools': [
        {'name': 'nmap', 'command': 'nmap'},
        {'name': 'shell_dangerous', 'command': 'bash'},
    ]}))
    extra = {'participant-vm': {
        'catalog': str(catalog),
        'tools': ['nmap', 'shell_dangerous'],
        'definitions': [{'name': 'nmap'}, {'name': 'shell_dangerous', 'risk': 'dangerous'}],
        'guidance_files': [],
    }}
    value = {
        'order_seed': 2,
        'reset_each_trial': True,
        'auto_approve_dangerous': True,
        'conditions': [{
            'id': 'all-tools', 'source': 'participant-vm',
            'tools': ['nmap', 'shell_dangerous'],
        }],
    }
    runtime, cfg = {'conditions': [], 'execution': {}}, {}
    apply(value, runtime, cfg, tmp_path / 'inputs', {'conditions': [], 'execution': {}},
          extra_catalogs=extra)
    assert runtime['execution']['auto_approve_dangerous'] is True
    assert runtime['conditions'][0]['tools'] == ['nmap', 'shell_dangerous']
    assert 'provide_progressive_hints' not in runtime['conditions'][0]
    assert 'definitions' not in runtime['conditions'][0]
    with pytest.raises(SampleRequestError, match='available'):
        validate({**value, 'conditions': [{**value['conditions'][0], 'tools': ['missing']}]},
                 {'conditions': []}, extra)


def test_selected_caf_guidance_assets_are_frozen_into_condition(tmp_path):
    runtime, cfg = {'conditions': [], 'execution': {}}, {}
    assets = {
        'playbook:triage': {
            'id': 'playbook:triage', 'kind': 'playbook', 'name': 'triage',
            'content': '# CAF playbook: triage\n\nInspect the service.\n',
        },
        'skill:verify': {
            'id': 'skill:verify', 'kind': 'skill', 'name': 'verify',
            'content': '# CAF skill: verify\n\nVerify the result.\n',
        },
    }
    value = {
        'conditions': [{
            'id': 'guided', 'source': 'baseline', 'tools': ['curl'],
            'guidance_assets': ['playbook:triage', 'skill:verify'],
        }],
    }
    apply(value, runtime, cfg, tmp_path / 'inputs', {'conditions': [], 'execution': {}},
          guidance_assets=assets)
    files = runtime['conditions'][0]['guidance_files']
    assert len(files) == 2
    assert 'Inspect the service.' in Path(files[0]).read_text()
    assert 'Verify the result.' in Path(files[1]).read_text()
    with pytest.raises(SampleRequestError, match='available in the participant snapshot'):
        validate({**value, 'conditions': [{**value['conditions'][0],
                 'guidance_assets': ['playbook:missing']}]}, {'conditions': []},
                 guidance_assets=assets)


def test_condition_progressive_hints_are_rejected_as_ambiguous():
    with pytest.raises(SampleRequestError, match='Unknown condition fields'):
        validate({'conditions': [{
            'id': 'baseline', 'source': 'baseline', 'tools': ['curl'],
            'provide_progressive_hints': False,
        }]}, {'conditions': []})


def test_default_conditions_cannot_override_evaluation_hint_policy(tmp_path):
    runtime = {
        'conditions': [{
            'id': 'baseline', 'catalog': 'catalog.json', 'tools': ['curl'],
            'guidance_files': [], 'provide_progressive_hints': False,
        }],
        'execution': {'provide_progressive_hints': True},
    }
    apply({'conditions': []}, runtime, {}, tmp_path, runtime)
    assert runtime['execution']['provide_progressive_hints'] is True
    assert 'provide_progressive_hints' not in runtime['conditions'][0]


@pytest.mark.parametrize("changed", [False, True])
def test_lab_reset_checks_frozen_objectives_and_tracks_owned_session(
    tmp_path, monkeypatch, changed
):
    from cyber_agent_flow_orchestrator import scenarios
    from types import SimpleNamespace
    import hashlib

    original = b'<Scenarios><Scenario name="Lab"/></Scenarios>'
    package = tmp_path / "suite"
    (package / "evaluator").mkdir(parents=True)
    (package / "evaluator/scenario.xml").write_bytes(original)
    tasks = [
        {
            "id": "read",
            "prompt": "Inspect service",
            "verifier": {"type": "contains_all", "expected": ["observed"]},
        }
    ]
    baseline = {
        "package_hash": "original",
        "scenario": {
            "xml_sha256": hashlib.sha256(original).hexdigest(),
            "core_host": "core",
            "core_session_id": 1,
        },
    }

    def load(path):
        if Path(path) == package:
            return tasks, baseline
        newer = dict(
            baseline,
            scenario=dict(baseline["scenario"], core_session_id=2),
            readiness={"ok": True},
        )
        if changed:
            newer["scenario"]["xml_sha256"] = "changed"
        return tasks, newer

    monkeypatch.setattr(trial_reset.ev, "load_suite", load)
    monkeypatch.setattr(trial_reset.ev, "require_ready", lambda snapshot, age: None)
    monkeypatch.setattr(
        trial_reset.ev,
        "ProxmoxBackend",
        lambda *a, **k: SimpleNamespace(fetch_suite=lambda archive, path: path.mkdir()),
    )
    calls = []

    class Remote:
        def call(self, vmid, op, **data):
            calls.append(op)
            return {"path": "/opt/sf/outputs/source/.caf-reset-test.xml"}

    monkeypatch.setattr(scenarios, "guest", lambda backend: Remote())

    class Workflow:
        journal = {"token": "run"}
        agent = SimpleNamespace(
            put=lambda vmid, path, data: calls.append(("put", path, data))
        )

        def save(self):
            pass

        def notify(self, message):
            pass

        def command(self, stage, command, retry):
            calls.append(command["argv"])
            return "EVALUATION_PACKAGE_JSON: " + json.dumps(
                dict(
                    state="complete", readiness_passed=True, archive="/opt/sf/reset.zip"
                )
            )

    wf = Workflow()
    cfg = {
        "scenarioforge": dict(
            mode="execute",
            repo="/opt/sf",
            xml="/opt/sf/outputs/source/scenario.xml",
            user="sf",
            python="/opt/sf/python",
            scenario="Lab",
            output_root="/opt/sf/eval",
            timeout_seconds=300,
            split="test",
        ),
        "max_readiness_age_seconds": 3600,
    }
    runtime = {"backend": {"app_vmid": 9402}, "engine": {}}
    reset = trial_reset.callback(wf, cfg, runtime, package)
    directory = tmp_path / "attempt-1"
    directory.mkdir()
    if changed:
        from cyber_agent_flow_eval.runner import PreparationError

        with pytest.raises(PreparationError, match="changed"):
            reset(directory, {"trial_id": "trial-1"})
    else:
        assert reset(directory, {"trial_id": "trial-1"})["passed"]
    assert calls[0][-2:] == ["--session", "1"]
    assert wf.journal["active_reset_session_id"] == 2
    assert "reset-ready" in calls


def test_remote_guidance_and_authored_guidance_are_frozen_together(tmp_path):
    from types import SimpleNamespace
    from cyber_agent_flow_orchestrator.workflow import Workflow

    defaults = {
        "conditions": [
            {
                "id": "baseline",
                "catalog": "placeholder.json",
                "tools": ["remote_tool"],
                "guidance_files": ["placeholder.md"],
            }
        ]
    }
    source = {
        "collect": {
            "baseline": {
                "catalog": "/generated/catalog.json",
                "guidance_files": ["/generated/guide.md"],
            }
        },
        "artifacts": [],
    }
    runtime = {"conditions": [], "backend": {"participant_vmid": 101}}
    cfg = {}
    value = {
        "conditions": [
            dict(
                id="guided",
                source="configured-baseline",
                tools=["remote_tool"],
                guidance="Author guidance",
            )
        ],
        "reset_each_trial": True,
    }
    apply(value, runtime, cfg, tmp_path / "author", defaults, source)
    authored = runtime["conditions"][0]["guidance_files"][0]
    assert "placeholder.md" not in runtime["conditions"][0]["guidance_files"]
    agent = SimpleNamespace(
        get=lambda vm, path: (
            b'{"tools":[]}' if path.endswith(".json") else b"Generated guidance"
        )
    )
    output = tmp_path / "run"
    output.mkdir()
    journal = {"stages": {}}
    wf = Workflow(output, journal, agent=agent, progress=None)
    wf.freeze_runtime(
        runtime, {authored: "Author guidance"}, dict(cfg, id="frozen"), "a" * 64
    )
    import yaml

    frozen = yaml.safe_load((output / "runtime.yaml").read_text())
    assert [Path(p).read_text() for p in frozen["conditions"][0]["guidance_files"]] == [
        "Generated guidance",
        "Author guidance",
    ]


@pytest.mark.parametrize('explicit_conditions', [False, True])
def test_enabled_helpers_extend_existing_conditions_without_extra_trials(tmp_path, explicit_conditions):
    import hashlib
    catalog = tmp_path / 'native.json'
    native = {'name': 'curl', 'command': 'curl', 'description': 'Participant configured tool'}
    catalog.write_text(json.dumps({'tools': [native]}))
    runtime = {'conditions': [dict(id='existing', catalog=str(catalog), tools=['curl'], guidance_files=[])], 'execution': {}}
    defaults = {'conditions': [], 'execution': {}}
    extra = {'participant-vm': dict(catalog=str(catalog), tools=['curl'], guidance_files=[])}
    content = 'Inspect the response, then preserve the successful tool output as evidence.'
    helper_id = 'helper-' + '1' * 24
    helpers = {helper_id: dict(name='Evidence helper', content=content,
                              helper_sha256=hashlib.sha256(content.encode()).hexdigest())}
    controls = dict(conditions=[dict(id='selected', source='participant-vm', tools=['curl'])] if explicit_conditions else [],
                    helpers=['http-helper'], generated_helpers=[helper_id])
    apply(controls, runtime, {}, tmp_path / 'frozen', defaults, extra_catalogs=extra, generated_helpers=helpers)
    assert len(runtime['conditions']) == 1
    condition = runtime['conditions'][0]
    assert condition['id'] == ('selected' if explicit_conditions else 'existing')
    assert condition['tools'] == ['curl', 'http_flag_walk']
    frozen = json.loads(Path(condition['catalog']).read_text())
    assert frozen['tools'][0] == native
    assert [tool['name'] for tool in frozen['tools']] == condition['tools']
    assert 'Source: ' + helper_id in Path(condition['guidance_files'][0]).read_text()
    assert content in Path(condition['guidance_files'][0]).read_text()
    catalog.write_text('{}')
    assert json.loads(Path(condition['catalog']).read_text())['tools'][0] == native


@pytest.mark.parametrize('controls', [
    {'helpers': ['baseline']}, {'helpers': ['http-helper', 'http-helper']},
    {'generated_helpers': ['helper-' + 'a' * 24]}, {'helpers': [dict(id='http-helper')]},
])
def test_helper_selection_rejects_unavailable_or_invalid_ids(controls):
    with pytest.raises(SampleRequestError, match='Select available'):
        validate(controls, {'conditions': []})


def test_conflicting_helper_tool_is_rejected(tmp_path):
    catalog = tmp_path / 'conflict.json'
    catalog.write_text(json.dumps({'tools': [dict(name='http_flag_walk', command='different')]}))
    runtime = {'conditions': [dict(id='existing', catalog=str(catalog), tools=['http_flag_walk'], guidance_files=[])]}
    with pytest.raises(SampleRequestError, match='Conflicting helper tool'):
        apply({'helpers': ['http-helper']}, runtime, {}, tmp_path / 'frozen', {'conditions': []})


def test_configuration_names_allow_display_text_without_changing_runtime_ids(tmp_path):
    value = {'conditions': [{'id': 'configuration-1', 'name': 'Guided baseline', 'source': 'baseline', 'tools': ['curl']}]}
    runtime = {'conditions': [], 'execution': {}}
    apply(value, runtime, {}, tmp_path, {'conditions': [], 'execution': {}})
    assert runtime['conditions'][0]['id'] == 'configuration-1'
    assert 'name' not in runtime['conditions'][0]
    for name in ['', '  ', 'x' * 81, 'invalid\nname', None]:
        with pytest.raises(SampleRequestError, match='configuration names'):
            validate({'conditions': [dict(value['conditions'][0], name=name)]}, {'conditions': []})
    with pytest.raises(SampleRequestError, match='configuration names'):
        validate({'conditions': value['conditions'] + [dict(value['conditions'][0], id='configuration-2', name='GUIDED BASELINE')]}, {'conditions': []})
