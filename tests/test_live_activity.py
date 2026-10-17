"""Live highlights require recorded actions, current execution and fresh observation."""

from datetime import datetime, timedelta, timezone
import json
import pytest
from cyber_agent_flow_eval.network_targets import extract
from cyber_agent_flow_orchestrator.live_activity import observe
from test_progress_visualization import save

NOW = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)
BASE = "evaluation/trials/trial-1/attempt-0001/"
ROW = dict(
    trial_id="trial-1",
    status="running",
    started_at=(NOW - timedelta(seconds=100)).isoformat(),
)
NODES = [
    dict(id="first", targets=["10.0.0.2"]),
    dict(id="second", targets=["10.0.0.3"]),
    dict(id="ipv6", targets=["2001:db8::2"]),
]


def activity(
    root, events, *, active=True, phase="executing", age=0, monitor=None, lagging=False
):
    save(
        root,
        BASE + "transport.json",
        dict(
            phase=phase,
            updated_at=(NOW - timedelta(seconds=age)).isoformat(),
            execution_started_at=ROW["started_at"],
            transcript_offset=0,
            transcript_size=1 if lagging else 0,
            service_status=dict(SubState="running"),
        ),
    )
    path = root / (BASE + "live-events.jsonl")
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return observe(root, BASE, ROW, monitor or {}, NODES, active=active, now=NOW)


def event(kind, age=1, **fields):
    return dict(
        type=kind, _eval_at=(NOW - timedelta(seconds=age)).isoformat(), **fields
    )


def test_in_flight_scan_range_maps_challenges_and_renews_from_guest_heartbeat(tmp_path):
    value = activity(
        tmp_path,
        [
            event(
                "tool_call",
                age=100,
                tool="shell_dangerous",
                args=dict(command="nmap -sV 10.0.0.0/24"),
            )
        ],
    )
    assert value["live"] and value["phase"] == "tool" and value["kind"] == "scan"
    assert (
        value["targets"][0]["address"] == "10.0.0.0/24" and value["targets"][0]["range"]
    )
    assert value["step_ids"] == ["first", "second"]
    assert datetime.fromisoformat(value["expires_at"]) == NOW + timedelta(seconds=45)
    assert value["source"]["file"].endswith("live-events.jsonl")


def test_finished_tool_gets_only_a_short_recent_highlight(tmp_path):
    events = [
        event("tool_call", age=20, tool="curl", args=dict(url="http://10.0.0.3/")),
        event(
            "tool_result",
            age=2,
            tool="curl",
            args=dict(url="http://10.0.0.3/"),
            result="hello",
        ),
    ]
    value = activity(tmp_path, events)
    assert (
        value["live"] and value["phase"] == "recent" and value["step_ids"] == ["second"]
    )
    events[-1]["_eval_at"] = (NOW - timedelta(seconds=13)).isoformat()
    assert not activity(tmp_path, events)["live"]


@pytest.mark.parametrize(
    "change",
    [
        dict(active=False),
        dict(phase="collecting"),
        dict(age=46),
        dict(lagging=True),
        dict(monitor=dict(checks=[dict(status="running")])),
    ],
)
def test_inactive_stale_lagging_and_reviewing_never_blink(tmp_path, change):
    value = activity(
        tmp_path,
        [event("tool_call", tool="curl", args=dict(url="http://10.0.0.3/"))],
        **change,
    )
    assert not value["live"] and value["step_ids"] == [] and value["expires_at"] is None


def test_reasoning_private_answers_and_guide_ips_are_not_live_targets(tmp_path):
    monitor = dict(
        updated_at=NOW.isoformat(),
        targeted_steps=["first"],
        checks=[dict(status="completed")],
    )
    value = activity(
        tmp_path,
        [event("reasoning", text="I should scan 10.0.0.3. Guide says 10.0.0.2.")],
        monitor=monitor,
    )
    assert value["targets"] == []
    assert value["live"] and value["context_step_ids"] == ["first"]
    # A current action wins over a previous monitor focus at another host.
    value = activity(
        tmp_path,
        [event("tool_call", tool="curl", args=dict(url="http://10.0.0.3/"))],
        monitor=monitor,
    )
    assert value["step_ids"] == ["second"] and value["context_step_ids"] == []


def test_ipv6_unknown_targets_and_secret_argument_fields(tmp_path):
    value = activity(
        tmp_path,
        [
            event(
                "tool_call",
                tool="curl",
                args=dict(
                    url="http://[2001:db8::2]/", token="10.0.0.2", password="10.0.0.3"
                ),
            )
        ],
    )
    assert value["targets"] == [
        dict(address="2001:db8::2", step_ids=["ipv6"], range=False)
    ]
    value = activity(
        tmp_path, [event("tool_call", tool="curl", args=dict(url="http://192.0.2.8/"))]
    )
    assert (
        value["live"]
        and value["targets"][0]["step_ids"] == []
        and value["step_ids"] == []
    )


def test_legacy_elapsed_time_and_terminal_events(tmp_path):
    value = activity(
        tmp_path,
        [
            dict(
                type="tool_result",
                tool="curl",
                args=dict(url="http://10.0.0.2"),
                _eval_elapsed_seconds=99,
            )
        ],
    )
    assert value["live"] and value["phase"] == "recent"
    value = activity(
        tmp_path,
        [
            event("tool_call", tool="curl", args=dict(url="http://10.0.0.2")),
            event("chat_done"),
        ],
    )
    assert not value["live"] and not value["targets"]


def test_linked_or_unbounded_transcripts_are_not_followed(tmp_path):
    path = tmp_path / (BASE + "live-events.jsonl")
    path.parent.mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.write_text(
        json.dumps(event("tool_call", tool="curl", args="http://10.0.0.2/"))
    )
    path.symlink_to(outside)
    assert (
        observe(tmp_path, BASE, ROW, {}, NODES, active=True, now=NOW)["targets"] == []
    )


def test_tool_output_only_mentions_do_not_become_contacted_targets(tmp_path):
    value = activity(tmp_path, [event("tool_result", tool="shell", args=dict(command="cat report.txt"), result="Found 10.0.0.2 and 2001:db8::2")])
    assert value["live"] and value["targets"] == [] and value["step_ids"] == []


@pytest.mark.parametrize("kind,fields,state", [
    ("status",dict(message="Calling openai (turn 2)"),"waiting_model"),
    ("reasoning",dict(text="I should try 10.0.0.2 next"),"reasoning"),
    ("response",dict(text="Preparing the next action"),"responding"),
])
def test_new_model_activity_replaces_recent_tool_without_inventing_targets(tmp_path, kind, fields, state):
    value=activity(tmp_path,[event("tool_result",age=3,tool="curl",args=dict(url="http://10.0.0.3/")),event(kind,**fields)])
    assert value["live"] and value["phase"]=="thinking" and value["agent_state"]==state
    assert value["tool"] is None and value["targets"]==[]


def test_generic_tool_action_is_live_without_an_ip(tmp_path):
    value=activity(tmp_path,[event("tool_call",tool="shell_dangerous",args=dict(command="ls -la /tmp"))])
    assert value["live"] and value["agent_state"]=="using_tool"
    assert value["tool"]=="shell_dangerous" and value["targets"]==[]
