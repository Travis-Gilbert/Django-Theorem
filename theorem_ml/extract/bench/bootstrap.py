"""Seed an explicit benchmark declaration and read back real tenant entity IDs.

Tool contracts: Theorem rustyred-thg-mcp/src/schema_tools.rs schema_declare;
rustyred-thg-schema/src/providers/record.rs generated create_one/find_one tools.
No model predictions, guessed IDs, or resolver outputs become identity gold.
"""
import json
from pathlib import Path

from ..spans.labels import SchemaClient

PERSON_TYPE = "ExtractionBenchPerson"
OBJECT_TYPE = "ExtractionBenchLease"


def field(key, kind, *, description=None, **extra):
    return {"key": key, "label": key.title(), "description": description or key.title(),
            "field_type": {"kind": kind, **extra}, "required": True, "system": False}


def declaration(name, fields, identifier, *, derive_claims=False):
    return {"name_singular": name, "name_plural": name + "s", "label_singular": name,
            "label_plural": name + "s", "node_label": name, "fields": fields,
            "label_identifier_field": identifier, "enforcement": "reject", "system": False,
            "extensions": {"extraction": {"derive_claims": derive_claims}}}


def bootstrap(corpus: Path, records: list, *, tenant: str, output: Path, client=None):
    if not tenant or not tenant.strip():
        raise ValueError("An authenticated benchmark tenant is required")
    client = client or SchemaClient()

    def declare(value):
        result = client.call_tool(tenant, "schema_declare", value)
        declared = result.get("object_type", {})
        if result.get("status") != "declared" or declared.get("tenant_id") != tenant:
            raise ValueError("Benchmark declaration conflicted or belongs to another tenant")
        if not declared.get("object_type_id") or declared.get("name_singular") != value["name_singular"]:
            raise ValueError("Schema declaration omitted its authoritative identity")
        return declared

    person = declare(declaration(PERSON_TYPE, [field("name", "text")], "name"))
    lease = declare(declaration(OBJECT_TYPE, [field("reference", "text"),
        field("person", "relation", description="Person who approved or signed the lease",
              target_object_type_id=person["object_type_id"], cardinality="one"),
        field("amount", "number"), field("deadline", "date")], "reference", derive_claims=True))
    golds = {r["id"]: json.loads((corpus / r["gold"]).read_text()) for r in records}
    identities = {}
    for name in sorted({g["fields"]["person"]["value"] for g in golds.values()}):
        created = client.call_tool(tenant, "create_one_extraction_bench_person", {"name": name})
        record = created.get("record", {})
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("Created benchmark entity omitted its authoritative ID")
        # Read separately through the generated tenant-scoped record tool.
        read = client.call_tool(tenant, "find_one_extraction_bench_person", {"id": record["id"]}).get("record", {})
        if read.get("id") != record["id"] or read.get("properties", {}).get("tenant_id") != tenant or read["properties"].get("name") != name:
            raise ValueError("Benchmark entity readback failed tenant/name/ID verification")
        identities[name] = read["id"]
    result = {"tenant": tenant, "object_type": OBJECT_TYPE,
              "object_type_id": lease["object_type_id"], "person_type_id": person["object_type_id"],
              "provenance": "schema_generated_create_and_independent_readback",
              "entities": identities,
              "documents": {document: [{"field_key": "person", "text": name, "entity_id": identities[name]}
                  for name in gold["spans"]["person"]] for document, gold in golds.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result
