from datetime import datetime, timezone

from theorem_ml.extract.temporal import invalidations, source_time


def test_message_time_precedes_document_date_and_normalizes_zone():
    assert source_time({'message_date': 'Fri, 05 Sep 2025 10:00:00 -0400',
                        'document_date': '2026-01-01T00:00:00Z'}, datetime.now(timezone.utc)) == '2025-09-05T14:00:00+00:00'


def test_deadline_moved_closes_old_without_changing_evidence():
    old = {'id': 'old', 'tenant_id': 'a', 'subject_id': 'project', 'predicate': 'deadline',
           'object_text': 'Friday', 'verified': True, 'valid_from': '2026-09-05T09:00:00-04:00', 'valid_to': None}
    incoming = {**old, 'id': 'new', 'object_text': 'Monday', 'exclusive_predicate': True,
                'valid_from': '2026-09-05T14:00:00+00:00'}
    updates = invalidations([old], incoming)
    assert updates[0]['valid_to'] == incoming['valid_from']
    assert updates[0]['edge']['target'] == 'new'
    assert old['valid_to'] is None and old['object_text'] == 'Friday'
    assert invalidations([old], {**incoming, 'verified': False}) == []
    assert invalidations([old], {**incoming, 'tenant_id': 'b'}) == []
