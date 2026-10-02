"""Execution choices are explicit in initial plans and future-plan revisions."""

import pytest
from pydantic import ValidationError
from vikingbot.compile.plan import DEFAULT_PLAN, PlanProposal, ReviewDecision, parse_plan
from vikingbot.compile.schemas import result_schema


@pytest.mark.parametrize("schema", [PlanProposal, ReviewDecision])
def test_plan_execution_is_required(schema):
    """Accept either mode and reject missing or unknown modes in every work configuration."""
    transform_schema = result_schema(schema, {})["$defs"]["Transform"]
    assert "execution" in transform_schema["required"]
    assert "default" not in transform_schema["properties"]["execution"]

    for execution in ("direct", "agent"):
        contract = {
            "extract": {"instructions": "Extract facts", "execution": execution},
            "reduce": {"instructions": "Combine facts", "execution": execution},
            "synthesize": {"instructions": "Organize facts", "execution": execution},
            "routing": {"mode": "all"},
        }
        data = {"contract": contract, "plan": DEFAULT_PLAN}
        if schema is ReviewDecision:
            data["action"] = "revise"
        result = schema.model_validate(data)
        assert len(parse_plan(result.plan, result.contract)) == 4
        restored = schema.model_validate(result.model_dump())
        for name in ("extract", "reduce", "synthesize"):
            assert getattr(restored.contract, name).execution == execution
            for invalid in ({}, {"execution": "automatic"}):
                bad_contract = {
                    **contract,
                    name: {"instructions": contract[name]["instructions"], **invalid},
                }
                with pytest.raises(ValidationError) as rejected:
                    schema.model_validate({**data, "contract": bad_contract})
                assert any(
                    error["loc"] == ("contract", name, "execution")
                    for error in rejected.value.errors()
                )

    if schema is ReviewDecision:
        assert schema.model_validate({"action": "continue"}).contract is None
