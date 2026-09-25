from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.experience_dag import DEFAULT_BRANCH_CHOICE, Dag, DagInstance
from openviking.session.memory.experience_dag_compiler import compile_dag
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

SOURCE = """dag = workflow("Check an order")
order_id = ask("What is the order ID?")
order = call("query_order", "Use order_id from context")
order_id.then(order)"""


def dag(source: str = SOURCE) -> Dag:
    return Dag.model_validate_json(compile_dag(source))


def instance(source: str = SOURCE) -> DagInstance:
    return DagInstance(
        experience_uri="viking://user/u/memories/experiences/order.md", dag=dag(source)
    )


def operation(source: str, *, page_id: int = 1, name: str = "order") -> ResolvedOperation:
    return ResolvedOperation(
        memory_type="experiences",
        uris=[f"viking://user/u/memories/experiences/{name}.md"],
        page_id=page_id,
        memory_fields={"content": source, "experience_name": name},
        old_memory_file_content=None,
    )


def test_source_builds_internal_ids_without_handwritten_ids():
    built = dag()

    assert list(built.nodes) == [1, 2]
    assert built.nodes[1].slot_name == "order_id"
    assert built.nodes[2].slot_name == "order"
    assert built.nodes[2].predecessors == [1]


def test_fanout_and_and_join_are_explicit():
    built = dag("""dag = workflow("verify cancellation")
reservation = call("get_reservation_details")
payment = call("get_payment_details")
insurance = call("get_insurance_details")
verified = check("Payment and insurance details are both available")
cancel = call("cancel_reservation")
reservation.then(payment, insurance)
payment.then(verified)
insurance.then(verified)
verified.then(cancel)""")

    assert built.successors(1) == [2, 3]
    assert built.nodes[4].predecessors == [2, 3]
    assert built.nodes[5].predecessors == [4]

    run = instance(
        """dag = workflow("verify cancellation")
reservation = call("get_reservation_details")
payment = call("get_payment_details")
insurance = call("get_insurance_details")
verified = check("Payment and insurance details are both available")
reservation.then(payment, insurance)
payment.then(verified)
insurance.then(verified)"""
    )
    run.advance()
    run.merge_slot_values({"reservation": True})
    assert [action.slot_name for action in run.advance()[0]] == ["payment", "insurance"]
    run.merge_slot_values({"payment": True})
    actions, waiting = run.advance()
    assert [action.slot_name for action in actions] == ["insurance"]
    assert waiting == []
    run.merge_slot_values({"insurance": True})
    actions, waiting = run.advance()
    assert actions == []
    assert waiting == [4]


def test_boolean_and_named_branches_use_object_references():
    built = dag("""dag = workflow("route request")
eligible = check("Request is eligible")
approve = tell("Approve request")
transfer = tell("Transfer request")
intent = choose("Which operation is requested?")
cancel = tell("Cancel")
modify = tell("Modify")
eligible.if_true(approve)
eligible.if_false(transfer)
intent.case("cancel", cancel)
intent.case("modify", modify)
intent.default(transfer)""")

    assert built.nodes[1].node_type == "IfElseBranch"
    assert built.nodes[1].true_branch == 2
    assert built.nodes[1].false_branch == 3
    assert built.nodes[4].node_type == "ConditionalBranch"
    assert built.nodes[4].branch_mapping == {"cancel": 5, "modify": 6}
    assert built.nodes[4].default_branch == 3


def test_false_boolean_selects_false_branch():
    run = instance(
        """dag = workflow("Confirm cancellation")
confirm = ask("Confirm cancellation?")
cancel = call("cancel_reservation")
stop = tell("Do not cancel")
confirm.if_true(cancel)
confirm.if_false(stop)"""
    )
    run.advance()

    run.merge_slot_values({"confirm": False})
    actions, _ = run.advance()

    assert [action.slot_name for action in actions] == ["stop"]


@pytest.mark.parametrize(
    "source",
    [
        "import os",
        "dag = workflow('x')",
        "node = ask('x')",
        "dag = workflow('x')\nnode = ask('x')\nnode.then(unknown)",
        "dag = workflow('x')\nnode = ask('x')\nnode.case('x', node)",
        "dag = workflow('x')\nnode = call('search_exp')",
        "dag = workflow('x')\nnode = call('search_experience')",
        "dag = workflow('x')\nnode = call('read_experience')",
        "dag = workflow('x')\nnode = check('x')\nnode.if_true(node)\nnode.if_true(node)",
        "dag = workflow('x')\nnode = ask('x')\nnode.id",
    ],
)
def test_rejects_invalid_source(source):
    with pytest.raises(ValueError):
        compile_dag(source)


def test_ask_and_call_advance_with_boolean_completion():
    run = instance()
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == ["order_id"]

    run.merge_slot_values({"order_id": True})
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == ["order"]

    run.merge_slot_values({"order": True})
    assert run.advance() == ([], [])
    assert run.state == "completed"

    with pytest.raises(ValueError, match="Invalid value for slot order"):
        instance().merge_slot_values({"order": {"reservation_id": "DF89BM"}})


def test_tell_advances_only_with_true():
    run = instance(
        """dag = workflow("Locate reservation")
identify_target = tell("Identify the matching reservation")
continue_work = tell("Continue with the selected reservation")
identify_target.then(continue_work)"""
    )
    run.advance()
    run.merge_slot_values({"identify_target": False})
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == ["identify_target"]

    run.merge_slot_values({"identify_target": True})
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == ["continue_work"]
    assert run.slot_values["identify_target"] is True


def test_choose_accepts_declared_or_explicit_default_label_only():
    run = instance(
        """dag = workflow("Route request")
intent = choose("Which operation is requested?")
cancel = tell("Cancel")
fallback = tell("Escalate")
intent.case("cancel", cancel)
intent.default(fallback)"""
    )
    run.advance()

    with pytest.raises(ValueError, match="Invalid value for slot intent"):
        run.merge_slot_values({"intent": "invented"})

    run.merge_slot_values({"intent": DEFAULT_BRANCH_CHOICE})
    actions, _ = run.advance()
    assert [action.slot_name for action in actions] == ["fallback"]


def test_choose_rejects_reserved_default_case_label():
    with pytest.raises(ValueError, match="reserved for the default branch"):
        compile_dag(
            """dag = workflow("Route request")
intent = choose("Which operation is requested?")
fallback = tell("Escalate")
intent.case("__default__", fallback)"""
        )


def test_source_round_trip_preserves_literal_links():
    source = """dag = workflow('[order](https://example.com?q="x")')
order = tell("Finish")"""
    file = MemoryFile(
        uri="viking://user/u/memories/experiences/order.md",
        content=source,
        memory_type="experiences",
        links=[
            {"to_uri": "viking://user/u/memories/trajectories/a.md", "link_type": "derived_from"}
        ],
    )

    restored = MemoryFileUtils.read(MemoryFileUtils.write(file), uri=file.uri)

    assert restored.plain_content() == source
    assert dag(restored.plain_content()).applicability == '[order](https://example.com?q="x")'
    assert restored.links == file.links


def test_invalid_experience_does_not_block_valid_experience_operation():
    loop = object.__new__(ExtractLoop)
    valid = operation(SOURCE, name="valid")
    invalid = operation(
        "dag = workflow('x')\nnode = ask('x')\nnode.then(missing)", page_id=2, name="invalid"
    )
    batch = ResolvedOperations(
        upsert_operations=[valid, invalid], delete_file_contents=[], errors=[]
    )

    errors = loop._compile_experience_operations(batch)
    loop._discard_invalid_experience_operations(batch, errors)

    assert [item.memory_fields["experience_name"] for item in batch.upsert_operations] == ["valid"]
    assert dag(batch.upsert_operations[0].memory_fields["content"])


@pytest.mark.asyncio
async def test_invalid_source_blocks_upsert_and_delete():
    updater = object.__new__(MemoryUpdater)
    updater._registry = SimpleNamespace()
    updater._get_viking_fs = lambda: AsyncMock()
    updater._apply_upsert = AsyncMock()
    batch = ResolvedOperations(
        upsert_operations=[operation("dag = workflow('x')")],
        delete_file_contents=[MemoryFile(uri="viking://user/u/memories/experiences/old.md")],
        errors=[],
    )

    result = await updater.apply_operations(batch, ctx=SimpleNamespace())

    assert result.errors
    updater._apply_upsert.assert_not_awaited()
