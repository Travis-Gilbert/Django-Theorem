"""Element-addressed pointer, summary and full disclosure bodies."""
from __future__ import annotations

from enum import StrEnum


class DisclosureTier(StrEnum):
    POINTER = 'pointer'
    SUMMARY = 'summary'
    FULL = 'full'


def build_layers(tree, *, fields: dict, claims: list, entities: list[dict]) -> dict:
    tree.validate()
    by_id = {element.id: element for element in tree.elements}
    children = {key: [] for key in by_id}
    for element in tree.elements:
        if element.parent:
            children[element.parent].append(element.id)
    for ids in children.values():
        ids.sort(key=lambda key: by_id[key].order)

    def descendants(key):
        result = [key]
        for child in children[key]:
            result.extend(descendants(child))
        return result

    claim_dicts = [c.to_dict() if hasattr(c, 'to_dict') else c for c in claims]
    result = {}
    for element in tree.elements:
        member_ids = descendants(element.id)
        selected_claims = [c for c in claim_dicts if c['element_id'] in member_ids and c['verified']]
        selected_entities = [e for e in entities if e.get('element_id') in member_ids and e.get('entity_id')]
        scoped_fields = {key: value for key, value in fields.items()
                         if isinstance(value, dict) and value.get('element_id') in member_ids}
        first = next((by_id[key].text for key in member_ids if by_id[key].kind == 'Paragraph'), '')
        # A lead sentence stays bounded, while complete supporting claims and
        # element IDs retain the path back to the source paragraph.
        lead = first.split('. ', 1)[0].strip()
        abstract_parts = list(dict.fromkeys([lead, *[c['text'] for c in selected_claims]]))
        summary = {'element_id': element.id, 'kind': element.kind,
                   'fields': scoped_fields,
                   'claims': [{'id': c['id'], 'text': c['text'], 'score': c.get('score'),
                               'observed_at': c.get('observed_at'), 'valid_from': c.get('valid_from'),
                               'valid_to': c.get('valid_to')}
                              for c in selected_claims],
                   'entities': [{'id': e['entity_id'], 'text': e['text']} for e in selected_entities],
                   'abstract': ' '.join(part for part in abstract_parts if part),
                   'children': children[element.id]}
        if element.kind == 'Table':
            summary['table'] = {'rows': element.attributes.get('rows', []).__len__(),
                                'merged_cells': len(element.attributes.get('merged_cells', []))}
        result[element.id] = {
            'pointer': {'element_ids': member_ids, 'entity_ids': sorted({e['entity_id'] for e in selected_entities}),
                        'claim_ids': [c['id'] for c in selected_claims]},
            'summary': summary,
            'full': {'elements': [by_id[key].to_dict() for key in member_ids]},
        }
    return result


def read(layers: dict, element_id: str, tier: DisclosureTier | str = DisclosureTier.SUMMARY):
    return layers[element_id][DisclosureTier(tier).value]
