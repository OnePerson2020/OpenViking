# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
"""Interpret the restricted object-reference DAG source without executing it."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from openviking.session.memory.experience_dag import (
    DEFAULT_BRANCH_CHOICE,
    MAX_CONTENT_BYTES,
    AskUser,
    CallTool,
    Check,
    ConditionalBranch,
    Dag,
    GeneralNode,
    IfElseBranch,
    TellAgent,
)

_NODE_FACTORIES = {"ask", "call", "check", "tell", "choose"}
_RELATION_METHODS = {"then", "if_true", "if_false", "case", "default"}
_CONTROL_PLANE_TOOLS = {"search_exp", "search_experience", "read_experience"}


@dataclass(slots=True)
class _NodeDraft:
    provider: Any
    relation_kind: str = "general"
    targets: list[tuple[str | None, _NodeDraft]] = field(default_factory=list)
    name: str | None = None


@dataclass(slots=True)
class _Workflow:
    applicability: str
    nodes: list[_NodeDraft] = field(default_factory=list)

    def bind(self, name: str, draft: _NodeDraft) -> _NodeDraft:
        if draft.name is not None:
            raise ValueError("A node must be assigned exactly once")
        draft.name = name
        self.nodes.append(draft)
        return draft

    def build(self) -> Dag:
        if not self.nodes:
            raise ValueError("Experience DAG must contain at least one node")
        ids = {draft.name: index for index, draft in enumerate(self.nodes, start=1)}
        predecessors = {draft.name: [] for draft in self.nodes}
        for source in self.nodes:
            for _, target in source.targets:
                if target.name is None:
                    raise ValueError("Every referenced node must be assigned first")
                predecessors[target.name].append(ids[source.name])

        nodes = {}
        for draft in self.nodes:
            if draft.name is None:
                raise ValueError("Every node must have a source variable name")
            node_id = ids[draft.name]
            common = {
                "id": node_id,
                "predecessors": predecessors[draft.name],
                "slot_name": draft.name,
                "slot_provider": draft.provider,
            }
            if draft.relation_kind == "if":
                targets = {label: ids[target.name] for label, target in draft.targets}
                nodes[node_id] = IfElseBranch(
                    **common,
                    true_branch=targets.get("true"),
                    false_branch=targets.get("false"),
                )
            elif draft.relation_kind == "choose":
                mapping = {
                    label: ids[target.name]
                    for label, target in draft.targets
                    if label not in {None, "default"}
                }
                defaults = [
                    ids[target.name] for label, target in draft.targets if label == "default"
                ]
                nodes[node_id] = ConditionalBranch(
                    **common,
                    branch_mapping=mapping,
                    default_branch=defaults[0] if defaults else None,
                )
            else:
                nodes[node_id] = GeneralNode(**common)
        dag = Dag(applicability=self.applicability, nodes=nodes)
        dag.normalize_branch_edges()
        dag.validate_graph()
        return dag


def compile_dag(source: str) -> str:
    """Compile a complete persisted source program to internal DAG JSON."""
    return _compile_source(_normalize_source(source)).to_content()


def normalize_dag_source(source: str, _base_source: str | None = None) -> str:
    """Validate a complete standalone source program and retain that source verbatim."""
    if _base_source is not None:
        raise ValueError("Experience updates must be complete standalone DAG source")
    source = _normalize_source(source)
    _compile_source(source)
    return source


def _normalize_source(source: str) -> str:
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("DAG source must be a string of at most 256 KiB")
    source = source.strip()
    if source.startswith("```python\n") and source.endswith("```"):
        source = source[len("```python\n") : -3].strip()
    return source


def _compile_source(source: str) -> Dag:
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        raise ValueError(f"Invalid DAG source: {exc}") from exc
    if not tree.body or sum(1 for _ in ast.walk(tree)) > 12000:
        raise ValueError("DAG source is empty or too complex")

    workflow: _Workflow | None = None
    values: dict[str, _NodeDraft | _Workflow] = {}

    def literal(node: ast.AST, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("DAG expression is too deeply nested")
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool, type(None)):
            return node.value
        if isinstance(node, ast.List):
            return [literal(item, depth + 1) for item in node.elts]
        if isinstance(node, ast.Dict):
            if any(key is None for key in node.keys):
                raise ValueError("Dictionary unpacking is not supported")
            keys = [literal(key, depth + 1) for key in node.keys]
            if any(type(key) not in (str, int) for key in keys) or len(set(keys)) != len(keys):
                raise ValueError("Dictionary keys must be distinct strings or integers")
            return dict(
                zip(keys, [literal(value, depth + 1) for value in node.values], strict=True)
            )
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        raise ValueError(f"Unsupported DAG expression: {type(node).__name__}")

    def call_factory(node: ast.Call) -> _Workflow | _NodeDraft:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ValueError("DAG factories accept positional literal arguments only")
        name = node.func.id
        args = [literal(arg) for arg in node.args]
        if name == "workflow":
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("workflow requires one applicability string")
            return _Workflow(applicability=args[0])
        if name == "ask":
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("ask requires one question string")
            return _NodeDraft(provider=AskUser(question=args[0]))
        if name == "call":
            if not 1 <= len(args) <= 2 or not all(isinstance(value, str) for value in args):
                raise ValueError(
                    "call requires tool_name and optional parameter suggestion strings"
                )
            if args[0] in _CONTROL_PLANE_TOOLS:
                raise ValueError(f"{args[0]} is a memory control-plane operation, not a DAG action")
            return _NodeDraft(
                provider=CallTool(
                    tool_name=args[0], param_suggest=args[1] if len(args) == 2 else ""
                )
            )
        if name == "check":
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("check requires one condition string")
            return _NodeDraft(provider=Check(check_info=args[0]))
        if name == "tell":
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("tell requires one instruction string")
            return _NodeDraft(provider=TellAgent(instruction=args[0]))
        if name == "choose":
            if len(args) != 1 or not isinstance(args[0], str):
                raise ValueError("choose requires one question string")
            return _NodeDraft(provider=AskUser(question=args[0]), relation_kind="choose")
        raise ValueError(f"Unsupported DAG factory: {name}")

    def relation(node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            raise ValueError("Only node relation methods are allowed")
        source = values.get(node.func.value.id)
        method = node.func.attr
        if not isinstance(source, _NodeDraft) or method not in _RELATION_METHODS or node.keywords:
            raise ValueError("Unsupported DAG relation")
        args = [literal(arg) for arg in node.args]
        if method == "then":
            if not args or not all(isinstance(target, _NodeDraft) for target in args):
                raise ValueError("then requires one or more node variables")
            source.targets.extend((None, target) for target in args)
            return
        if method in {"if_true", "if_false", "default"}:
            if len(args) != 1 or not isinstance(args[0], _NodeDraft):
                raise ValueError(f"{method} requires one node variable")
            if method in {"if_true", "if_false"}:
                if source.relation_kind == "choose":
                    raise ValueError("choose nodes use case/default relations")
                source.relation_kind = "if"
                label = "true" if method == "if_true" else "false"
            else:
                if source.relation_kind != "choose":
                    raise ValueError("default is only valid for choose nodes")
                label = "default"
            if any(existing_label == label for existing_label, _ in source.targets):
                raise ValueError(f"Duplicate {method} relation")
            source.targets.append((label, args[0]))
            return
        if method == "case":
            if source.relation_kind != "choose" or len(args) != 2:
                raise ValueError("case requires a choose node, label string and node variable")
            label, target = args
            if not isinstance(label, str) or not isinstance(target, _NodeDraft):
                raise ValueError("case requires a label string and node variable")
            if label == DEFAULT_BRANCH_CHOICE:
                raise ValueError(f"{DEFAULT_BRANCH_CHOICE} is reserved for the default branch")
            if any(existing_label == label for existing_label, _ in source.targets):
                raise ValueError("Duplicate case label")
            source.targets.append((label, target))
            return

    for statement in tree.body:
        try:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise ValueError("Only simple variable assignments are allowed")
                name = statement.targets[0].id
                if name.startswith("_") or name in values:
                    raise ValueError(f"Reserved or repeated variable: {name}")
                if not isinstance(statement.value, ast.Call):
                    raise ValueError("Assignments must call workflow or a node factory")
                result = call_factory(statement.value)
                if name == "dag":
                    if not isinstance(result, _Workflow) or workflow is not None:
                        raise ValueError("Source must begin with dag = workflow(...)")
                    workflow = result
                    values[name] = result
                else:
                    if workflow is None or not isinstance(result, _NodeDraft):
                        raise ValueError("Declare dag = workflow(...) before node variables")
                    values[name] = workflow.bind(name, result)
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                relation(statement.value)
            else:
                raise ValueError("Only node declarations and relation calls are allowed")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"DAG source line {statement.lineno}: {exc}") from exc
    if workflow is None:
        raise ValueError("DAG source must begin with dag = workflow(...)")
    return workflow.build()
