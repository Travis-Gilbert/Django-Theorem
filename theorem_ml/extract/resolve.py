"""Resolve extracted mentions through the authenticated Rust schema authority."""
from __future__ import annotations

from .spans.labels import SchemaClient


def link_or_pend(span: dict, *, tenant: str, client: SchemaClient) -> dict:
    result = client.call(tenant, 'link_or_pend', object_type=span['object_type'],
                         field=span['field_key'], name=span['text'])
    if result.get('status') == 'linked' and isinstance(result.get('value'), str):
        return {**span, 'entity_id': result['value']}
    if result.get('status') == 'pending' and isinstance(result.get('value'), dict):
        return {**span, 'entity_id': None, 'pending_relation': result['value']}
    raise ValueError('resolver returned neither Linked nor PendingRelation')


def resolve_mentions(spans, *, tenant: str, client: SchemaClient):
    return [link_or_pend(span, tenant=tenant, client=client)
            for span in spans if span.get('target_object_type_id')]
