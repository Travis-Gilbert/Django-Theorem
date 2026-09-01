#!/usr/bin/env python3
"""RunPod worker for theorem.extraction.v1 Arrow extraction shards."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pyarrow as pa


OFFLOAD_CONTRACT = "theorem.offload.v1"
EXTRACTION_CONTRACT = "theorem.extraction.v1"
SUPPORTED_OPERATIONS = frozenset(
    {
        "data_science.extraction.atlas",
        "data_science.extraction.typed",
    }
)
ARROW_IPC_CONTENT_TYPE = "application/vnd.apache.arrow.stream"
MAX_ARTIFACT_BYTES = int(os.environ.get("ARTIFACT_MAX_BYTES", 64 * 1024 * 1024))
CONTRACT_PATH = Path(
    os.environ.get(
        "THEOREM_EXTRACTION_CONTRACT",
        Path(__file__).resolve().parents[2] / "contracts/theorem.extraction.v1.json",
    )
)


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _schema_json(schema: pa.Schema) -> str:
    metadata = {
        key.decode("utf-8", errors="replace"): value.decode("utf-8", errors="replace")
        for key, value in sorted((schema.metadata or {}).items())
    }
    return json.dumps(
        {
            "fields": [
                {"name": field.name, "type": str(field.type), "nullable": field.nullable}
                for field in schema
            ],
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_arrow(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _decode_arrow(payload: bytes) -> pa.Table:
    try:
        return pa.ipc.open_stream(pa.py_buffer(payload)).read_all()
    except (pa.ArrowException, ValueError, OSError) as exc:
        raise ValueError("input artifact is not a valid Arrow IPC stream") from exc


def _load_contract() -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("contract") != EXTRACTION_CONTRACT:
        raise ValueError(f"contract file must declare {EXTRACTION_CONTRACT}")
    return contract


def _arrow_type(type_name: str) -> pa.DataType:
    try:
        return {
            "utf8": pa.string(),
            "int32": pa.int32(),
            "int64": pa.int64(),
            "float32": pa.float32(),
        }[type_name]
    except KeyError as exc:
        raise ValueError(f"unsupported contract Arrow type: {type_name}") from exc


def extraction_schema(contract: Mapping[str, Any]) -> pa.Schema:
    return pa.schema(
        [
            pa.field(
                field["name"],
                _arrow_type(field["type"]),
                nullable=bool(field["nullable"]),
            )
            for field in contract["arrow_schema"]["fields"]
        ]
    )


def input_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("passage_id", pa.string(), nullable=False),
            pa.field("text", pa.string(), nullable=False),
            pa.field("metadata_json", pa.string(), nullable=True),
        ]
    )


def _download(url: str, *, max_bytes: int) -> bytes:
    with urlopen(url, timeout=60) as response:  # noqa: S310 - signed capability from Django.
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("input artifact exceeds max_bytes")
    return payload


def _upload(url: str, payload: bytes) -> None:
    request = Request(
        url,
        data=payload,
        method="PUT",
        headers={"Content-Type": ARROW_IPC_CONTENT_TYPE},
    )
    with urlopen(request, timeout=60):  # noqa: S310 - signed capability from Django.
        pass


def validate_input(value: Any) -> str | None:
    """Return a contract error for an invalid RunPod handler request."""
    if not isinstance(value, Mapping):
        return "job input must be an object"
    if value.get("contract") != OFFLOAD_CONTRACT:
        return f"contract must equal {OFFLOAD_CONTRACT!r}"
    if value.get("operation") not in SUPPORTED_OPERATIONS:
        return "operation is not a supported extraction operation"
    if not isinstance(value.get("operation_id"), str) or not value["operation_id"].strip():
        return "operation_id must be a non-empty string"
    descriptor = value.get("input")
    if not isinstance(descriptor, Mapping):
        return "input must be an Arrow descriptor object"
    if not isinstance(descriptor.get("schema_json"), str):
        return "input.schema_json must be a string"
    rows = descriptor.get("rows")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        return "input.rows must be a non-negative integer"
    if not _is_sha256(descriptor.get("payload_digest")):
        return "input.payload_digest must be a sha256 digest"
    if not isinstance(descriptor.get("artifact_key"), str) or not descriptor["artifact_key"]:
        return "input.artifact_key must be a non-empty string"
    if not isinstance(descriptor.get("read_url"), str) or not descriptor["read_url"].startswith(
        "https://"
    ):
        return "input.read_url must be an HTTPS signed URL"
    output = value.get("output")
    if not isinstance(output, Mapping):
        return "output must be an object"
    if not isinstance(output.get("artifact_key"), str) or not output["artifact_key"]:
        return "output.artifact_key must be a non-empty string"
    if not isinstance(output.get("write_url"), str) or not output["write_url"].startswith(
        "https://"
    ):
        return "output.write_url must be an HTTPS signed URL"
    params = value.get("params")
    if not isinstance(params, Mapping):
        return "params must be an object"
    if params.get("contract") != EXTRACTION_CONTRACT:
        return f"params.contract must equal {EXTRACTION_CONTRACT!r}"
    for name in ("tenant_id", "job_id"):
        if not isinstance(params.get(name), str) or not params[name].strip():
            return f"params.{name} must be a non-empty string"
    if not isinstance(params.get("shard"), int) or isinstance(params.get("shard"), bool):
        return "params.shard must be an integer"
    max_bytes = value.get("max_bytes", MAX_ARTIFACT_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        return "max_bytes must be a positive integer"
    if value["operation"].endswith(".typed"):
        object_type = params.get("object_type")
        if not isinstance(object_type, Mapping):
            return "typed extraction requires params.object_type"
        for name in ("object_type_id", "label_identifier_field", "system"):
            if not isinstance(object_type.get(name), str) or not object_type[name].strip():
                return f"params.object_type.{name} must be a non-empty string"
        if not isinstance(object_type.get("schema"), Mapping):
            return "params.object_type.schema must be an object"
    return None


def _validated_input_table(payload: bytes, descriptor: Mapping[str, Any]) -> pa.Table:
    if _sha256(payload) != descriptor["payload_digest"]:
        raise ValueError("input artifact digest does not match the descriptor")
    table = _decode_arrow(payload)
    expected = input_schema()
    if not table.schema.equals(expected, check_metadata=False):
        raise ValueError("input artifact must use the extraction passage schema")
    if _schema_json(table.schema) != descriptor["schema_json"]:
        raise ValueError("input artifact schema does not match the descriptor")
    if table.num_rows != descriptor["rows"]:
        raise ValueError("input artifact row count does not match the descriptor")
    return table


def _passages(table: pa.Table) -> list[dict[str, Any]]:
    passages = []
    for raw in table.to_pylist():
        metadata_raw = raw.get("metadata_json")
        metadata = json.loads(metadata_raw) if metadata_raw else {}
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata_json must decode to an object")
        passages.append(
            {
                "passage_id": raw["passage_id"],
                "text": raw["text"],
                "metadata": dict(metadata),
            }
        )
    return passages


def _prompt_hash(contract: Mapping[str, Any], stage: str) -> str:
    return str(contract["prompts"][stage]["sha256"])


def _find_span(text: str, subject: str, *, offset: int = 0) -> tuple[int | None, int | None]:
    local_start = text.find(subject)
    if local_start < 0:
        local_start = text.casefold().find(subject.casefold())
    if local_start < 0:
        return None, None
    start = offset + local_start
    return start, start + len(subject)


def _base_row(
    *,
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
    passage_id: str,
    passage_text: str,
    stage: str,
    subject: str,
    subject_kind: str,
    predicate: str,
    object_value: str,
    object_kind: str,
    chunk_offset: int = 0,
    prompt_hash: str | None = None,
    schema_hash: str | None = None,
    object_type_id: str | None = None,
    record_json: str | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    span_start, span_end = _find_span(passage_text, subject, offset=chunk_offset)
    return {
        "tenant_id": params["tenant_id"],
        "job_id": params["job_id"],
        "shard": params["shard"],
        "stage": stage,
        "passage_id": passage_id,
        "span_start": span_start,
        "span_end": span_end,
        "subject": subject,
        "subject_kind": subject_kind,
        "predicate": predicate,
        "object": object_value,
        "object_kind": object_kind,
        "object_type_id": object_type_id,
        "record_json": record_json,
        "confidence": confidence,
        "model_id": params.get("model_id") or contract["model"]["default_id"],
        "prompt_hash": prompt_hash or _prompt_hash(contract, stage),
        "schema_hash": schema_hash or contract["schema_sha256"],
        "extractor_version": contract["extractor_version"],
    }


def _canonical_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        key = tuple(
            row.get(name)
            for name in (
                "tenant_id",
                "job_id",
                "shard",
                "stage",
                "passage_id",
                "subject",
                "predicate",
                "object",
                "record_json",
            )
        )
        unique[key] = row
    return [unique[key] for key in sorted(unique, key=lambda item: tuple(str(v) for v in item))]


def _chunk_offset(
    passage_text: str,
    chunk_text: str,
    prior_offset: int | None,
) -> int:
    search_from = 0 if prior_offset is None else prior_offset + 1
    offset = passage_text.find(chunk_text, search_from)
    if offset < 0:
        offset = passage_text.find(chunk_text)
    return max(offset, 0)


def _atlas_json_rows(
    *,
    output_directory: Path,
    passages: Mapping[str, Mapping[str, Any]],
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prior_offsets: dict[str, int] = {}
    for path in sorted((output_directory / "kg_extraction").glob("*.json")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            passage_id = str(value["id"])
            passage = passages.get(passage_id)
            if passage is None:
                raise ValueError(f"ATLAS returned unknown passage id {passage_id}")
            chunk_text = str(value.get("original_text") or "")
            offset = _chunk_offset(
                str(passage["text"]),
                chunk_text,
                prior_offsets.get(passage_id),
            )
            prior_offsets[passage_id] = offset
            for triple in value.get("entity_relation_dict") or []:
                rows.append(
                    _base_row(
                        params=params,
                        contract=contract,
                        passage_id=passage_id,
                        passage_text=chunk_text,
                        chunk_offset=offset,
                        stage="entity_relation",
                        subject=str(triple["Head"]),
                        subject_kind="entity",
                        predicate=str(triple["Relation"]),
                        object_value=str(triple["Tail"]),
                        object_kind="entity",
                    )
                )
            for pair in value.get("event_entity_dict") or []:
                for entity in pair.get("Entity") or []:
                    rows.append(
                        _base_row(
                            params=params,
                            contract=contract,
                            passage_id=passage_id,
                            passage_text=chunk_text,
                            chunk_offset=offset,
                            stage="event_entity",
                            subject=str(pair["Event"]),
                            subject_kind="event",
                            predicate="participant",
                            object_value=str(entity),
                            object_kind="entity",
                        )
                    )
            for triple in value.get("event_relation_dict") or []:
                rows.append(
                    _base_row(
                        params=params,
                        contract=contract,
                        passage_id=passage_id,
                        passage_text=chunk_text,
                        chunk_offset=offset,
                        stage="event_relation",
                        subject=str(triple["Head"]),
                        subject_kind="event",
                        predicate=str(triple["Relation"]),
                        object_value=str(triple["Tail"]),
                        object_kind="event",
                    )
                )
    return rows


def _concept_rows(
    *,
    output_directory: Path,
    passages: Mapping[str, Mapping[str, Any]],
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    concept_files = sorted((output_directory / "concepts").glob("*.csv"))
    for path in concept_files:
        with path.open(encoding="utf-8", newline="") as handle:
            for item in csv.DictReader(handle):
                subject = str(item.get("node") or "").strip()
                kind = str(item.get("node_type") or "").strip().lower()
                concepts = [
                    concept.strip()
                    for concept in str(item.get("conceptualized_node") or "").split(",")
                    if concept.strip()
                ]
                subject_kind = {"entity": "entity", "event": "event"}.get(kind, "concept")
                for passage_id, passage in passages.items():
                    passage_text = str(passage["text"])
                    if subject.casefold() not in passage_text.casefold():
                        continue
                    for concept in concepts:
                        rows.append(
                            _base_row(
                                params=params,
                                contract=contract,
                                passage_id=passage_id,
                                passage_text=passage_text,
                                stage="concept",
                                subject=subject,
                                subject_kind=subject_kind,
                                predicate="has_concept",
                                object_value=concept,
                                object_kind="concept",
                            )
                        )
    return rows


def _run_atlas(
    table: pa.Table,
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Run ATLAS 0.0.5.post1 wholesale against the co-located vLLM server."""
    from atlas_rag.kg_construction.triple_config import ProcessingConfig
    from atlas_rag.kg_construction.triple_extraction import KnowledgeGraphExtractor
    from atlas_rag.llm_generator import LLMGenerator
    from atlas_rag.llm_generator.generation_config import GenerationConfig
    from openai import OpenAI

    class GuidedLLMGenerator(LLMGenerator):
        def triple_extraction(self, messages, result_schema, **kwargs):
            previous = self.config.response_format
            self.config.response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "atlas_extraction",
                    "schema": result_schema,
                    "strict": True,
                },
            }
            try:
                return super().triple_extraction(messages, result_schema, **kwargs)
            finally:
                self.config.response_format = previous

    passage_rows = _passages(table)
    passage_map = {item["passage_id"]: item for item in passage_rows}
    model_id = params.get("model_id") or contract["model"]["default_id"]
    with tempfile.TemporaryDirectory(prefix="theorem-extraction-") as temporary:
        root = Path(temporary)
        data_directory = root / "data"
        output_directory = root / "output"
        data_directory.mkdir()
        source_path = data_directory / "theorem.jsonl"
        with source_path.open("w", encoding="utf-8") as handle:
            for passage in passage_rows:
                handle.write(
                    json.dumps(
                        {
                            "id": passage["passage_id"],
                            "text": passage["text"],
                            "metadata": passage["metadata"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        client = OpenAI(
            base_url=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key=os.environ.get("VLLM_API_KEY", "local-vllm"),
        )
        generation = GenerationConfig(
            max_tokens=int(contract["model"]["max_new_tokens"]),
            temperature=float(contract["model"]["temperature"]),
        )
        generator = GuidedLLMGenerator(
            client=client,
            model_name=model_id,
            backend="vllm",
            max_workers=max(1, int(params.get("max_workers", 8))),
            default_config=generation,
        )
        chunking = contract["chunking"]
        config = ProcessingConfig(
            model_path=model_id,
            data_directory=str(data_directory),
            filename_pattern="theorem",
            batch_size_triple=max(1, int(params.get("batch_size_triple", 16))),
            batch_size_concept=max(1, int(params.get("batch_size_concept", 64))),
            output_directory=str(output_directory),
            max_new_tokens=int(contract["model"]["max_new_tokens"]),
            max_workers=max(1, int(params.get("max_workers", 8))),
            remove_doc_spaces=bool(chunking["remove_doc_spaces"]),
            chunk_size=int(chunking["chunk_size"]),
            chunk_overlap=int(chunking["chunk_overlap"]),
        )
        extractor = KnowledgeGraphExtractor(model=generator, config=config)
        extractor.run_extraction()
        extractor.convert_json_to_csv()
        extractor.generate_concept_csv_temp(batch_size=config.batch_size_concept)
        extractor.create_concept_csv()
        rows = _atlas_json_rows(
            output_directory=output_directory,
            passages=passage_map,
            params=params,
            contract=contract,
        )
        rows.extend(
            _concept_rows(
                output_directory=output_directory,
                passages=passage_map,
                params=params,
                contract=contract,
            )
        )
    return _canonical_rows(rows)


def _typed_rows_from_records(
    *,
    passage: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    object_type = params["object_type"]
    label_field = object_type["label_identifier_field"]
    object_type_id = object_type["object_type_id"]
    schema_text = json.dumps(object_type["schema"], sort_keys=True, separators=(",", ":"))
    prompt_hash = object_type.get("prompt_hash") or hashlib.sha256(
        object_type["system"].encode("utf-8")
    ).hexdigest()
    schema_hash = object_type.get("schema_hash") or hashlib.sha256(
        schema_text.encode("utf-8")
    ).hexdigest()
    rows = []
    for record in records:
        subject = record.get(label_field)
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError(f"typed record omitted label field {label_field}")
        rows.append(
            _base_row(
                params=params,
                contract=contract,
                passage_id=str(passage["passage_id"]),
                passage_text=str(passage["text"]),
                stage="typed",
                subject=subject,
                subject_kind="record",
                predicate="is_a",
                object_value=object_type_id,
                object_kind="record",
                object_type_id=object_type_id,
                record_json=json.dumps(record, sort_keys=True, separators=(",", ":")),
                prompt_hash=prompt_hash,
                schema_hash=schema_hash,
            )
        )
    return rows


def _run_typed(
    table: pa.Table,
    params: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    from jsonschema import validate
    from openai import OpenAI

    object_type = params["object_type"]
    schema = object_type["schema"]
    model_id = params.get("model_id") or contract["model"]["default_id"]
    client = OpenAI(
        base_url=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=os.environ.get("VLLM_API_KEY", "local-vllm"),
    )
    rows: list[dict[str, Any]] = []
    for passage in _passages(table):
        instructions = str(object_type.get("instructions") or "").strip()
        examples = object_type.get("examples") or []
        user_parts = [instructions, "Passage:", passage["text"]]
        if examples:
            user_parts.extend(
                ["Examples:", json.dumps(examples, ensure_ascii=False, sort_keys=True)]
            )
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": object_type["system"]},
                {"role": "user", "content": "\n\n".join(part for part in user_parts if part)},
            ],
            max_tokens=int(contract["model"]["max_new_tokens"]),
            temperature=float(contract["model"]["temperature"]),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "typed_extraction",
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("typed vLLM response omitted content")
        payload = json.loads(content)
        validate(instance=payload, schema=schema)
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("typed response must contain records")
        rows.extend(
            _typed_rows_from_records(
                passage=passage,
                records=records,
                params=params,
                contract=contract,
            )
        )
    return _canonical_rows(rows)


class FixtureModelClient:
    """Deterministic local replay; never used by the RunPod handler."""

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.fixture = fixture

    def atlas_rows(
        self,
        table: pa.Table,
        params: Mapping[str, Any],
        contract: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        passage_ids = set(table["passage_id"].to_pylist())
        rows = [
            dict(row)
            for row in self.fixture["stub_responses"]["atlas_rows"]
            if row["passage_id"] in passage_ids
        ]
        for row in rows:
            row["tenant_id"] = params["tenant_id"]
            row["job_id"] = params["job_id"]
            row["shard"] = params["shard"]
            row["model_id"] = params.get("model_id") or contract["model"]["default_id"]
        return _canonical_rows(rows)

    def typed_rows(
        self,
        table: pa.Table,
        params: Mapping[str, Any],
        contract: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        records = self.fixture["stub_responses"]["typed_records_by_passage"]
        rows: list[dict[str, Any]] = []
        for passage in _passages(table):
            rows.extend(
                _typed_rows_from_records(
                    passage=passage,
                    records=records.get(passage["passage_id"], []),
                    params=params,
                    contract=contract,
                )
            )
        return _canonical_rows(rows)


def execute(
    request: Mapping[str, Any],
    *,
    input_payload: bytes | None = None,
    upload: Callable[[str, bytes], None] | None = _upload,
    fixture_client: FixtureModelClient | None = None,
) -> dict[str, Any]:
    error = validate_input(request)
    if error:
        return {"error": f"invalid {OFFLOAD_CONTRACT} input: {error}"}
    descriptor = request["input"]
    output = request["output"]
    params = request["params"]
    max_bytes = int(request.get("max_bytes", MAX_ARTIFACT_BYTES))
    try:
        payload = input_payload
        if payload is None:
            payload = _download(str(descriptor["read_url"]), max_bytes=max_bytes)
        table = _validated_input_table(payload, descriptor)
        contract = _load_contract()
        if request["operation"].endswith(".atlas"):
            rows = (
                fixture_client.atlas_rows(table, params, contract)
                if fixture_client
                else _run_atlas(table, params, contract)
            )
        else:
            rows = (
                fixture_client.typed_rows(table, params, contract)
                if fixture_client
                else _run_typed(table, params, contract)
            )
        output_table = pa.Table.from_pylist(rows, schema=extraction_schema(contract))
        output_payload = _encode_arrow(output_table)
        if len(output_payload) > max_bytes:
            return {"error": "output_exceeds_max_bytes"}
        if upload is not None:
            upload(str(output["write_url"]), output_payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"error": f"{EXTRACTION_CONTRACT} execution failed: {exc}"}
    return {
        "output": {
            "schema_json": _schema_json(output_table.schema),
            "rows": output_table.num_rows,
            "payload_digest": _sha256(output_payload),
        }
    }


def handler(job: Mapping[str, Any]) -> dict[str, Any]:
    request = job.get("input")
    if not isinstance(request, Mapping):
        return {"error": "RunPod job must contain an input object"}
    return execute(request)


def _local_request(
    fixture: Mapping[str, Any],
    *,
    operation: str,
    max_bytes: int,
) -> dict[str, Any]:
    params_key = "typed_params" if operation.endswith(".typed") else "params"
    return {
        "contract": OFFLOAD_CONTRACT,
        "operation": operation,
        "operation_id": f"fixture:{operation}",
        "input": {
            **{key: fixture["input"][key] for key in ("schema_json", "rows", "payload_digest")},
            "artifact_key": "tenants/fixture/extraction/input.arrow",
            "read_url": "https://fixture.invalid/input.arrow",
        },
        "output": {
            "artifact_key": "tenants/fixture/extraction/output.arrow",
            "write_url": "https://fixture.invalid/output.arrow",
        },
        "params": fixture[params_key],
        "max_bytes": max_bytes,
    }


def run_local(
    input_path: str,
    *,
    stub_model: bool,
    operation: str,
    max_bytes: int,
) -> int:
    fixture = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if fixture.get("contract") != "theorem.extraction.fixture.v1":
        raise ValueError("local input must be a theorem.extraction.fixture.v1 fixture")
    payload = base64.b64decode(fixture["input"]["payload_base64"], validate=True)
    client = FixtureModelClient(fixture) if stub_model else None
    request = _local_request(fixture, operation=operation, max_bytes=max_bytes)
    result = execute(request, input_payload=payload, upload=None, fixture_client=client)
    print(json.dumps(result, indent=2, sort_keys=True))
    if "error" in result:
        return 1
    expected_key = "typed_output" if operation.endswith(".typed") else "atlas_output"
    expected = fixture["expected"][expected_key]
    return 0 if result["output"] == {key: expected[key] for key in result["output"]} else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-input")
    parser.add_argument("--stub-model", action="store_true")
    parser.add_argument(
        "--operation",
        choices=sorted(SUPPORTED_OPERATIONS),
        default="data_science.extraction.atlas",
    )
    parser.add_argument("--max-bytes", type=int, default=MAX_ARTIFACT_BYTES)
    args = parser.parse_args(argv)
    if args.local_input:
        return run_local(
            args.local_input,
            stub_model=args.stub_model,
            operation=args.operation,
            max_bytes=args.max_bytes,
        )
    import runpod

    runpod.serverless.start({"handler": handler})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
