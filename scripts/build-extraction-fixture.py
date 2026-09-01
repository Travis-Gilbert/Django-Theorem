#!/usr/bin/env python3
"""Build the deterministic theorem.extraction.v1 Arrow replay fixture."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runpod/theorem_extraction_worker"))

import worker  # noqa: E402


PASSAGES = [
    {
        "passage_id": "passage:law:1",
        "text": "Fett Law declined representation for the employee in March 2026.",
        "metadata_json": json.dumps(
            {"lang": "en", "date": "2026-03-10", "source": "life_email"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    },
    {
        "passage_id": "passage:law:2",
        "text": "In March 2026, Fett Law declined representation after reviewing the matter.",
        "metadata_json": json.dumps(
            {"lang": "en", "date": "2026-03-12", "source": "web_corpus"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    },
    {
        "passage_id": "passage:law:3",
        "text": "The employee contacted another employment attorney about the dispute.",
        "metadata_json": json.dumps(
            {"lang": "en", "date": "2026-03-13", "source": "life_email"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    },
]


def _descriptor(table: pa.Table) -> dict[str, Any]:
    payload = worker._encode_arrow(table)
    return {
        "schema_json": worker._schema_json(table.schema),
        "rows": table.num_rows,
        "payload_digest": worker._sha256(payload),
        "payload_base64": base64.b64encode(payload).decode("ascii"),
    }


def _atlas_rows(
    contract: dict[str, Any], params: dict[str, Any]
) -> list[dict[str, Any]]:
    texts = {item["passage_id"]: item["text"] for item in PASSAGES}

    def row(
        passage_id: str,
        stage: str,
        subject: str,
        subject_kind: str,
        predicate: str,
        object_value: str,
        object_kind: str,
    ) -> dict[str, Any]:
        return worker._base_row(
            params=params,
            contract=contract,
            passage_id=passage_id,
            passage_text=texts[passage_id],
            stage=stage,
            subject=subject,
            subject_kind=subject_kind,
            predicate=predicate,
            object_value=object_value,
            object_kind=object_kind,
        )

    return worker._canonical_rows(
        [
            row(
                "passage:law:1",
                "entity_relation",
                "Fett Law",
                "entity",
                "declined",
                "representation",
                "entity",
            ),
            row(
                "passage:law:2",
                "entity_relation",
                "Fett Law",
                "entity",
                "declined",
                "representation",
                "entity",
            ),
            row(
                "passage:law:3",
                "entity_relation",
                "employee",
                "entity",
                "contacted",
                "employment attorney",
                "entity",
            ),
            row(
                "passage:law:1",
                "event_entity",
                "Fett Law declined representation",
                "event",
                "participant",
                "Fett Law",
                "entity",
            ),
            row(
                "passage:law:2",
                "event_entity",
                "Fett Law declined representation",
                "event",
                "participant",
                "Fett Law",
                "entity",
            ),
            row(
                "passage:law:1",
                "concept",
                "Fett Law",
                "entity",
                "has_concept",
                "law firm",
                "concept",
            ),
            row(
                "passage:law:2",
                "concept",
                "Fett Law declined representation",
                "event",
                "has_concept",
                "declined engagement",
                "concept",
            ),
            row(
                "passage:law:3",
                "concept",
                "employment attorney",
                "entity",
                "has_concept",
                "lawyer",
                "concept",
            ),
        ]
    )


def build_fixture() -> dict[str, Any]:
    contract = worker._load_contract()
    input_table = pa.Table.from_pylist(PASSAGES, schema=worker.input_schema())
    params = {
        "contract": contract["contract"],
        "tenant_id": "tenant-fixture",
        "job_id": "job-fixture",
        "shard": 0,
        "batch_size_triple": 2,
        "batch_size_concept": 4,
        "model_id": contract["model"]["default_id"],
    }
    atlas_rows = _atlas_rows(contract, params)
    atlas_table = pa.Table.from_pylist(
        atlas_rows,
        schema=worker.extraction_schema(contract),
    )
    object_schema = {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "counterparty": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["counterparty", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["records"],
        "additionalProperties": False,
    }
    record = {"counterparty": "Fett Law", "status": "declined"}
    typed_system = (
        "Extract Obligation records from the passage. Return only records "
        "supported by the source."
    )
    schema_text = json.dumps(object_schema, sort_keys=True, separators=(",", ":"))
    typed_params = {
        **params,
        "object_type": {
            "object_type_id": "theorem.objects.obligation/v1",
            "label_identifier_field": "counterparty",
            "system": typed_system,
            "instructions": "Extract the counterparty and engagement status.",
            "examples": [record],
            "schema": object_schema,
            "prompt_hash": hashlib.sha256(typed_system.encode("utf-8")).hexdigest(),
            "schema_hash": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
        },
    }
    typed_rows = worker._typed_rows_from_records(
        passage={
            "passage_id": PASSAGES[0]["passage_id"],
            "text": PASSAGES[0]["text"],
        },
        records=[record],
        params=typed_params,
        contract=contract,
    )
    typed_table = pa.Table.from_pylist(
        worker._canonical_rows(typed_rows),
        schema=worker.extraction_schema(contract),
    )
    return {
        "contract": "theorem.extraction.fixture.v1",
        "fixture_origin": {
            "kind": "deterministic_stub",
            "live_recorded": False,
            "note": (
                "Replace with a recorded real-worker artifact after the live GPU "
                "oracle succeeds."
            ),
        },
        "input": _descriptor(input_table),
        "params": params,
        "typed_params": typed_params,
        "stub_responses": {
            "atlas_rows": atlas_rows,
            "typed_records_by_passage": {PASSAGES[0]["passage_id"]: [record]},
        },
        "expected": {
            "atlas_rows": atlas_rows,
            "typed_rows": worker._canonical_rows(typed_rows),
            "atlas_output": _descriptor(atlas_table),
            "typed_output": _descriptor(typed_table),
        },
    }


def main() -> int:
    target = ROOT / "contracts/theorem.extraction.v1.fixture.json"
    target.write_text(
        json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
