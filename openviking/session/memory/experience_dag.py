# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""SOP templates and session-local execution state for experience memory."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_NODES = 256
MAX_CONTENT_BYTES = 256 * 1024
DEFAULT_BRANCH_CHOICE = "__default__"


class DagModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallTool(DagModel):
    type: Literal["CallTool"] = "CallTool"
    tool_name: str = Field(min_length=1)
    param_suggest: str = ""


class AskUser(DagModel):
    type: Literal["AskUser"] = "AskUser"
    question: str = Field(min_length=1)


class Check(DagModel):
    type: Literal["Check"] = "Check"
    check_info: str = Field(min_length=1)


class TellAgent(DagModel):
    type: Literal["TellAgent"] = "TellAgent"
    instruction: str = Field(min_length=1)


SlotProvider = Annotated[CallTool | AskUser | Check | TellAgent, Field(discriminator="type")]


class GeneralNode(DagModel):
    id: int = Field(gt=0, strict=True)
    predecessors: list[int] = Field(default_factory=list, max_length=MAX_NODES)
    node_type: Literal["GeneralNode"] = "GeneralNode"
    slot_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    slot_provider: SlotProvider

    def action_description(self) -> str:
        provider = self.slot_provider
        if isinstance(provider, CallTool):
            return f"Call {provider.tool_name}: {provider.param_suggest}"
        if isinstance(provider, AskUser):
            return f"Ask the user: {provider.question}"
        if isinstance(provider, TellAgent):
            return provider.instruction
        return f"Check the context: {provider.check_info}"


class IfElseBranch(GeneralNode):
    node_type: Literal["IfElseBranch"] = "IfElseBranch"
    true_branch: int | None = None
    false_branch: int | None = None


class ConditionalBranch(GeneralNode):
    node_type: Literal["ConditionalBranch"] = "ConditionalBranch"
    branch_mapping: dict[str, int] = Field(default_factory=dict)
    default_branch: int | None = None


class ParallelBranch(GeneralNode):
    node_type: Literal["ParallelBranch"] = "ParallelBranch"
    branches: list[int] = Field(default_factory=list, max_length=MAX_NODES)


Node = Annotated[
    GeneralNode | IfElseBranch | ConditionalBranch | ParallelBranch,
    Field(discriminator="node_type"),
]


def branch_targets(node: GeneralNode) -> set[int] | None:
    if isinstance(node, IfElseBranch):
        return {target for target in (node.true_branch, node.false_branch) if target is not None}
    if isinstance(node, ConditionalBranch):
        return set(node.branch_mapping.values()) | (
            {node.default_branch} if node.default_branch is not None else set()
        )
    if isinstance(node, ParallelBranch):
        return set(node.branches)
    return None


class Dag(DagModel):
    """A reusable template. All predecessors must complete before activation (AND)."""

    version: Literal[1] = 1
    applicability: str = ""
    nodes: dict[int, Node] = Field(default_factory=dict, max_length=MAX_NODES)

    def add_node(self, node: GeneralNode) -> None:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists; use update_node")
        if len(self.nodes) >= MAX_NODES:
            raise ValueError(f"DAG exceeds {MAX_NODES} nodes")
        self.nodes[node.id] = node

    def update_node(self, node: GeneralNode) -> None:
        if node.id not in self.nodes:
            raise ValueError(f"Node {node.id} does not exist")
        self.nodes[node.id] = node

    def delete_node(self, node_id: int) -> None:
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} does not exist")
        # Rewiring is explicit: validation rejects any remaining references.
        del self.nodes[node_id]

    def set_applicability(self, applicability: str) -> None:
        if not isinstance(applicability, str):
            raise ValueError("applicability must be a string")
        self.applicability = applicability

    def successors(self, node_id: int) -> list[int]:
        return sorted(key for key, node in self.nodes.items() if node_id in node.predecessors)

    def normalize_branch_edges(self) -> None:
        for branch in self.nodes.values():
            targets = branch_targets(branch)
            if targets is None:
                continue
            for node in self.nodes.values():
                if branch.id in node.predecessors and node.id not in targets:
                    node.predecessors.remove(branch.id)
            for target in targets:
                node = self.nodes.get(target)
                if node is not None and branch.id not in node.predecessors:
                    node.predecessors.append(branch.id)

    def validate_graph(self) -> None:
        if not self.nodes:
            raise ValueError("Experience DAG must contain at least one node")
        slots: set[str] = set()
        for key, node in self.nodes.items():
            if key != node.id:
                raise ValueError(f"Node key {key} differs from id {node.id}")
            if node.slot_name in slots:
                raise ValueError(f"Duplicate slot_name: {node.slot_name}")
            slots.add(node.slot_name)
            if len(set(node.predecessors)) != len(node.predecessors):
                raise ValueError(f"Duplicate predecessors for node {key}")
            missing = set(node.predecessors) - self.nodes.keys()
            if missing:
                raise ValueError(f"Node {key} has missing predecessors: {sorted(missing)}")
            targets = branch_targets(node)
            if targets is not None and targets != set(self.successors(key)):
                raise ValueError(f"Branch {key} targets must match its successors/predecessors")
            if isinstance(node, ParallelBranch) and (
                not node.branches or len(node.branches) != len(targets)
            ):
                raise ValueError(f"Parallel branch {key} must have distinct nonempty targets")
            if isinstance(node, ConditionalBranch) and not node.branch_mapping:
                raise ValueError(f"Conditional branch {key} must have choices")

        # Kahn's algorithm checks cycles in every connected component.
        indegrees = {key: len(node.predecessors) for key, node in self.nodes.items()}
        ready = [key for key, degree in indegrees.items() if degree == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for successor in self.successors(key):
                indegrees[successor] -= 1
                if indegrees[successor] == 0:
                    ready.append(successor)
        if visited != len(self.nodes):
            raise ValueError("Experience graph contains a cycle")

    def to_content(self) -> str:
        self.normalize_branch_edges()
        self.validate_graph()
        content = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        )
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("Experience DAG content is too large")
        return content

    @classmethod
    def from_content(cls, content: str) -> Dag:
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("Experience DAG content is too large")
        dag = cls.model_validate_json(content)
        dag.normalize_branch_edges()
        dag.validate_graph()
        return dag


class DagAction(DagModel):
    node_id: int
    slot_name: str
    provider: SlotProvider
    description: str


class DagEvidenceRef(DagModel):
    id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=4096)


class DagCompletedNode(DagModel):
    node_id: int
    slot_name: str
    slot_value: bool | str
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class DagInstance(DagModel):
    """One task's state, independent of the shared experience template."""

    instance_id: str = "default"
    experience_uri: str
    dag: Dag
    slot_values: dict[str, bool | str] = Field(default_factory=dict)
    slot_evidence: dict[str, list[str]] = Field(default_factory=dict)
    executed_nodes: list[int] = Field(default_factory=list)
    current_nodes: list[int] = Field(default_factory=list)
    state: Literal["pending", "running", "completed"] = "pending"

    def merge_slot_values(
        self,
        values: dict[str, bool | str],
        slot_evidence: dict[str, list[str]] | None = None,
    ) -> None:
        by_slot = {node.slot_name: node for node in self.dag.nodes.values()}
        for name, value in values.items():
            node = by_slot.get(name)
            if node is None:
                raise ValueError(f"Unknown slot: {name}")
            if value is None:
                continue
            if isinstance(node, ConditionalBranch):
                valid = isinstance(value, str) and (
                    value in node.branch_mapping
                    or (value == DEFAULT_BRANCH_CHOICE and node.default_branch is not None)
                )
            else:
                valid = type(value) is bool
            if not valid:
                raise ValueError(f"Invalid value for slot {name}: {value!r}")
        for name, refs in (slot_evidence or {}).items():
            if name not in values or values[name] is None or name not in by_slot:
                raise ValueError(f"Evidence supplied for unknown slot: {name}")
            if not isinstance(refs, list) or len(refs) != len(set(refs)):
                raise ValueError(f"Invalid evidence references for slot {name}")
            if any(not isinstance(ref, str) or not ref for ref in refs):
                raise ValueError(f"Invalid evidence references for slot {name}")
        self.slot_values.update({key: value for key, value in values.items() if value is not None})
        self.slot_evidence.update(
            {key: list(value) for key, value in (slot_evidence or {}).items() if key in values}
        )

    def advance(self) -> tuple[list[DagAction], list[int]]:
        self.dag.validate_graph()
        if self.state == "completed":
            return [], []
        if self.state == "pending":
            self.current_nodes = sorted(
                key for key, node in self.dag.nodes.items() if not node.predecessors
            )
            self.state = "running"
        executed = set(self.executed_nodes)
        current = set(self.current_nodes)
        while True:
            finished: set[int] = set()
            candidate_successors: set[int] = set()
            for key in sorted(current):
                node = self.dag.nodes[key]
                value = self.slot_values.get(node.slot_name)
                if value is None:
                    continue
                if isinstance(node, IfElseBranch):
                    target = node.true_branch if value else node.false_branch
                    targets = [] if target is None else [target]
                elif isinstance(node, ConditionalBranch):
                    target = (
                        node.default_branch
                        if value == DEFAULT_BRANCH_CHOICE
                        else node.branch_mapping.get(value)
                    )
                    targets = [] if target is None else [target]
                else:
                    if value is not True:
                        continue
                    targets = (
                        node.branches
                        if isinstance(node, ParallelBranch)
                        else self.dag.successors(key)
                    )
                finished.add(key)
                candidate_successors.update(targets)
            if not finished:
                break
            completed = executed | finished
            successors = {
                successor
                for successor in candidate_successors
                if set(self.dag.nodes[successor].predecessors).issubset(completed)
            }
            executed.update(finished)
            current = (current | successors) - executed
        self.executed_nodes = sorted(executed)
        self.current_nodes = sorted(current)
        if not current:
            self.state = "completed"
        actions: list[DagAction] = []
        waiting: list[int] = []
        for key in sorted(current):
            node = self.dag.nodes[key]
            if isinstance(node.slot_provider, Check):
                waiting.append(key)
            else:
                actions.append(
                    DagAction(
                        node_id=key,
                        slot_name=node.slot_name,
                        provider=node.slot_provider,
                        description=node.action_description(),
                    )
                )
        return actions, waiting
