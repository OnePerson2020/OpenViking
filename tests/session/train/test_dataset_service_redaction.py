from openviking.session.train import Case, CriterionResult, Rollout, Rubric, RubricEvaluation
from openviking.session.train.components.dataset_service import (
    redact_sensitive,
    rollout_from_dict,
    rollout_to_dict,
)


def test_redact_sensitive_recursively_masks_openviking_api_key():
    payload = {
        "policy_set": {
            "metadata": {
                "openviking_api_key": "secret-key",
                "openviking_url": "http://127.0.0.1:1933",
                "nested": [{"api_key": "other-secret"}],
            }
        },
        "case": "keep-me",
    }

    redacted = redact_sensitive(payload)

    assert redacted["policy_set"]["metadata"]["openviking_api_key"] == "<redacted>"
    assert redacted["policy_set"]["metadata"]["nested"][0]["api_key"] == "<redacted>"
    assert redacted["policy_set"]["metadata"]["openviking_url"] == "http://127.0.0.1:1933"
    assert payload["policy_set"]["metadata"]["openviking_api_key"] == "secret-key"


def test_redact_sensitive_masks_key_value_pairs_in_strings():
    text = "failed with openviking_api_key='secret-key', api_key=other-secret token: bearer"

    redacted = redact_sensitive(text)

    assert "secret-key" not in redacted
    assert "other-secret" not in redacted
    assert "bearer" not in redacted
    assert "openviking_api_key='<redacted>'" in redacted
    assert "api_key=<redacted>" in redacted
    assert "token: <redacted>" in redacted


def test_rollout_round_trip_keeps_nested_dag_runtime_events():
    rollout = Rollout(
        case=Case(
            name="case",
            task_signature="task",
            input={},
            rubric=Rubric(name="rubric", description="", criteria=[]),
        ),
        messages=[],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(
            passed=True,
            score=1.0,
            criterion_results=[
                CriterionResult(
                    criterion_name="done", passed=True, score=1.0, feedback=[], evidence=[]
                )
            ],
            feedback=[],
        ),
        metadata={
            "dag_runtime": {
                "session_id": "tau2_dag_1",
                "events": [{"revision": 2, "actions": [{"node_id": 3}]}],
            }
        },
    )

    restored = rollout_from_dict(rollout_to_dict(rollout))

    assert restored.metadata == rollout.metadata
