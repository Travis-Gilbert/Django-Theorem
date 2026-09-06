"""Source-time precedence and immutable bitemporal claim invalidation planning."""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def source_time(metadata: dict, captured_at: datetime) -> str:
    for key in ('message_date', 'document_date'):
        value = metadata.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(str(value))
            except (TypeError, ValueError, IndexError):
                continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat()
    if captured_at.tzinfo is None:
        raise ValueError('capture timestamp must be timezone-aware')
    return captured_at.astimezone(UTC).isoformat()


def temporal_properties(observed_at: str, *, valid_from: str | None = None) -> dict:
    observed = datetime.fromisoformat(observed_at)
    start = datetime.fromisoformat(valid_from or observed_at)
    if observed.tzinfo is None or start.tzinfo is None:
        raise ValueError('temporal facts require timezone-aware timestamps')
    return {'observed_at': observed.isoformat(), 'valid_from': start.isoformat(), 'valid_to': None}


def invalidations(existing: list[dict], incoming: dict) -> list[dict]:
    """Return graph updates for contradictory, single-valued verified assertions.

    Callers provide entity-linked subjects and a declared exclusive predicate.
    Merely different sentences are not treated as contradictions.
    """
    if not incoming.get('verified') or not incoming.get('exclusive_predicate'):
        return []
    subject = incoming.get('subject_id')
    predicate = incoming.get('predicate')
    if not subject or not predicate or incoming.get('object_text') is None:
        return []
    updates = []
    for old in existing:
        if (old.get('tenant_id') == incoming['tenant_id'] and old.get('subject_id') == subject
                and old.get('predicate') == predicate and old.get('verified')
                and old.get('valid_to') is None and old.get('id') != incoming['id']
                and old.get('object_text') != incoming['object_text']
                and datetime.fromisoformat(old['valid_from']) <= datetime.fromisoformat(incoming['valid_from'])):
            updates.append({'id': old['id'], 'valid_to': incoming['valid_from'],
                            'edge': {'kind': 'INVALIDATED_BY', 'source': old['id'], 'target': incoming['id']}})
    return updates
