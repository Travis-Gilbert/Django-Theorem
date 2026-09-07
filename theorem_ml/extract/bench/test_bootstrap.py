"""Fixture authority verifies bootstrap protocol, not live substrate writes."""
import json
from pathlib import Path

import pytest

from theorem_ml.extract.bench.bootstrap import OBJECT_TYPE, bootstrap
from theorem_ml.extract.bench.__main__ import load_corpus


class FixtureAuthority:
    def __init__(self, *, returned_tenant="fixture"):
        self.returned_tenant = returned_tenant
        self.calls = []
        self.records = {}

    def call_tool(self, tenant, tool, arguments):
        self.calls.append((tenant, tool, arguments))
        assert "tenant" not in arguments
        if tool == "schema_declare":
            return {"status": "declared", "object_type": {**arguments,
                "tenant_id": self.returned_tenant, "object_type_id": "fixture-type:" + arguments["name_singular"]}}
        if tool == "create_one_extraction_bench_person":
            identifier = "fixture-authority:" + str(len(self.records))
            record = {"id": identifier, "properties": {**arguments, "tenant_id": self.returned_tenant}}
            self.records[identifier] = record
            return {"record": record}
        assert tool == "find_one_extraction_bench_person"
        return {"record": self.records[arguments["id"]]}


def test_bootstrap_uses_returned_readback_ids_for_all_documents(tmp_path):
    corpus = Path(__file__).parent / "corpus"
    authority = FixtureAuthority()
    output = tmp_path / "entities.json"
    result = bootstrap(corpus, load_corpus(corpus), tenant="fixture", output=output, client=authority)
    assert result["object_type"] == OBJECT_TYPE and len(result["documents"]) == 100
    assert len(result["entities"]) == 10
    assert json.loads(output.read_text()) == result
    assert all(mention["entity_id"] in authority.records for mentions in result["documents"].values() for mention in mentions)
    assert sum(name.startswith("find_one_") for _, name, _ in authority.calls) == 10
    assert all(not mentions for document, mentions in result["documents"].items() if document.startswith("code-"))


def test_mismatched_authenticated_tenant_cannot_seed_gold(tmp_path):
    with pytest.raises(ValueError, match="another tenant"):
        bootstrap(tmp_path, [], tenant="fixture", output=tmp_path / "gold.json",
                  client=FixtureAuthority(returned_tenant="other"))
    assert not (tmp_path / "gold.json").exists()
