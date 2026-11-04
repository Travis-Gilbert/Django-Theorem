# Knowledge evolution contract boundary

`apps/orchestration/evolution_contract.py` consumes the Rust-owned
`contracts/theorem.knowledge_evolution.v1.json` with jsonschema 4.25.1. The source
schema lives under Theorem's existing `theorem-harness-core/contracts` owner;
the copies are checked byte-for-byte by the cross-repository verification runner.

`EvolutionMessage.parse` validates bounded UTF-8 JSON and retains immutable
canonical bytes. Unknown fields/versions, malformed artifact references,
missing ceilings and unsafe numeric representations refuse. Errors do not echo
candidate contents. `digest` and `bind_to` share the Rust contract semantics;
the latter checks exact request/attempt bindings, not current lease authority.

This module is the wire portion of SPEC-THEOREM-EXECUTABLE-KNOWLEDGE-EVOLUTION-1.0
Slice 0. It does not add a queue, Job model, knowledge store or execution API.
Existing authenticated orchestration, artifact access and lease/accounting
owners must still be connected before the engines may execute. Parsing a
principal/grant reference cannot grant permission or choose a tenant.

Theorem's `scripts/check-knowledge-evolution-contract.py` runs this package's
contract tests against the actual compiled Rust conformance driver. Tests use
clearly identified fixtures and do not claim paid/model-backed execution.
`docs/evolution-evidence/` retains the focused Python regression, mutation and
complexity results and the isolated test environment package versions.
