# Captured Web objects

`apps.orchestration.artifacts.ArtifactStore` owns tenant-scoped S3 access for
Arrow and captured Web content. `write_web_content` writes to deterministic
`tenants/<tenant>/web/{raw,text,captures}/<sha256>` keys and verifies readback
before returning a `WebObjectDescriptor`. Its JSON fields match RustyWeb's
`WebObjectRef`, including an explicit tenant scope and immutable snapshot id.
`read_web_content` rejects mismatched tenant, namespace, length, or digest.

The same `ARTIFACT_S3_ENDPOINT_URL`, `ARTIFACT_S3_REGION`,
`ARTIFACT_S3_BUCKET`, `ARTIFACT_S3_ACCESS_KEY_ID`, and
`ARTIFACT_S3_SECRET_ACCESS_KEY` settings configure Django and the trusted Rust
service. Rust reads verified objects and exact passage ranges directly from
S3; citations do not route through Django to download content. Deploy both
services with credentials restricted to their admitted tenant prefixes. Never
put credentials in a descriptor, source file, or deployment configuration.

Tigris is the preferred provider. Set the endpoint issued for the selected
Tigris account/bucket and region `auto`, while retaining the existing setting
names. The current [Tigris S3 configuration guide](https://www.tigrisdata.com/docs/ai-agents/replace-s3-with-tigris/)
documents `https://t3.storage.dev` for its public service. Existing Fly-integrated
buckets may expose their own configured endpoint; preserve that value rather
than rewriting it from a provider name. Provision the private bucket and scoped
keys before switching deployment secrets. No production migration was run by
this implementation, and local fake-client tests do not prove one.

Validation: `.venv/bin/python -m pytest tests/test_web_content_artifacts.py tests/test_artifacts.py -q`.
The Rust ignored test `web_object::tests::live_s3_web_object_reopen_digest_and_range`
uses the shared `ARTIFACT_S3_*` settings plus an explicit `THEOREM_WEB_OBJECT_TEST_TENANT`
to validate an isolated test bucket. It writes immutable content and leaves it
for retention policy; do not point it at a tenant without that authorization.
