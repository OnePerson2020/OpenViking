"""Map independent source units or intermediate records into records or file candidates."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vikingbot.compile.ops import common
from vikingbot.compile.ops import reduce as reduce_op
from vikingbot.compile.pipeline_io import bounded_jobs
from vikingbot.compile.plan import Group, Node, Record, RecordResponse

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


# Map guidance covers one assignment, which may be only part of the source collection.
_RECORDS = """Do not add plausible business consequences, instructions or definitions that the
sources do not establish. Do not turn examples into rules. Retain ambiguity in the actor of a
condition, conjunctions, slash notation and missing units; quote an unclear clause instead of
selecting a plausible interpretation or inventing an obligation.
Transform supplied inputs into the declared record fields, preserving the Skill.
Use record_fields names and descriptions as a guide for payload facts, which may contain structured JSON.
Use short scope keys with evidence-based values; scope_fields explains each suggested field.
Omit unavailable fields and add useful fields when the evidence calls for them.
Each record.inputs lists ONLY supplied input IDs supporting its payload; runtime assigns IDs,
stores complete source evidence and propagates provenance. Return records; runtime tracks
unreferenced inputs. References establish provenance, not semantic completeness.
Source text uses shard-local 1-based line numbers; source_range is the raw input ID. Optional top-level
evidence_spans use inclusive start_line/end_line. Include relevant conditions, exceptions, headings
and table headers/notes; omit uncertain locations.
Read all supplied text; preserve required detail, citations, exceptions and applicability conditions.
Include a short routing_text for each record; never group just by title.
Ready drafts are allowed.
If ready_content is non-null, ready_path MUST be a non-empty relative file
path under the compile target.
Put both fields at the record's top level, never inside payload.
Before calling emit, check this pairing for every record.
Independent finished files use ready_content with
ready_path and concise identity/scope/relationship payloads. Evidence details already in the body
need not be repeated in payload. Check finished content against originals and every Skill rule.
Fragments requiring joint synthesis retain full necessary evidence in payload and no ready content.
"""

_FILES = """# File generation
Follow the Skill, user instruction and assigned stage task for file count, paths, format and
content organization. This assignment contributes to the overall task's deliverables.

## Evidence
Check derived records against original sources and distinguish source facts from inference.
original_evidence entries marked complete=false are excerpts; read more with read_evidence
when the supplied text is insufficient.

## Related context
- related_outputs: a partial catalog of confirmed output files; an omitted page may still exist.
- related_subjects: topics assigned elsewhere, not confirmed files or link destinations.
Use known destinations for links.

## Submission
Submit files through emit using its field definitions.
"""


async def run(runtime: Pipeline, node: Node, inputs: list[Record]) -> list[Record] | list[str]:
    """Batch inputs for the selected transform and retain successful job outputs.

    File-granularity source assignments retain all ranges of one URI in offset order.
    Other agent/file-output assignments contain one record; direct record calls batch.
    Job failures update runtime state while other assignments continue.
    """
    transform = getattr(runtime.contract, node.task)
    if node.source == "sources" and transform.input_unit == "file":
        files: dict[str, list[Record]] = {}
        for record in inputs:
            files.setdefault(runtime.evidence[record.record_id]["uri"], []).append(record)
        jobs = [
            sorted(parts, key=lambda r: runtime.evidence[r.record_id]["start_char"])
            for parts in files.values()
        ]
    elif transform.execution == "agent" or transform.output == "files" or node.source != "sources":
        jobs = [[record] for record in inputs]
    else:
        jobs = await common.pack(
            runtime,
            inputs,
            runtime.system + _RECORDS + "\n## Stage task\n\n" + transform.instructions,
            RecordResponse,
            {
                "record_fields": transform.fields,
                "scope_fields": runtime.contract.distinguish,
            },
            max_payload_chars=runtime.model.reserve,
        )
    outputs = await bounded_jobs(
        enumerate(jobs),
        partial(map_job, runtime, node),
        concurrency=runtime.limits.source_concurrency,
        metrics=runtime.metrics,
        failures=runtime.failures,
    )
    result = [record for output in outputs for record in output]
    return await reduce_op.resolve_files(runtime, result) if transform.output == "files" else result


async def map_job(runtime: Pipeline, node, item):
    """Expand one packed Map assignment without a parent agent spawning children."""
    index, records = item
    transform = getattr(runtime.contract, node.task)
    name = f"{node.name}-{index}"
    return await common.job(
        runtime,
        name,
        records,
        lambda: (
            reduce_op.reduce_group(
                runtime, node, Group(name, records), stage="map", file_prompt=_FILES
            )
            if transform.output == "files"
            else common.transform(runtime, "map", transform, records, prompt=_RECORDS)
        ),
    )
