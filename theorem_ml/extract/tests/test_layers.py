import json

from theorem_ml.extract.elements import DocElement, build_tree
from theorem_ml.extract.layers import build_layers, read


def test_lease_summary_and_exact_table_descent():
    tree = build_tree('lease', [DocElement('section', 'SectionHeader', 'Lease', attributes={'level': 1}),
        DocElement('body', 'Paragraph', 'The lease ends in June. ' + 'Supporting contractual language. ' * 100, parent='section'),
        DocElement('rent', 'Table', 'Year 1 1450\nYear 2 1500', parent='section',
                   attributes={'rows': [['Year 1', '1450'], ['Year 2', '1500']], 'merged_cells': []})])
    claims = [{'id': 'accepted', 'element_id': 'body', 'text': 'The lease ends in June.', 'verified': True, 'score': .9},
              {'id': 'rejected', 'element_id': 'body', 'text': 'Rent is free.', 'verified': False, 'score': .1}]
    layers = build_layers(tree, fields={}, claims=claims, entities=[])
    summary = read(layers, 'section')
    assert 'June' in summary['abstract'] and 'free' not in json.dumps(summary)
    assert 'rent' in summary['children']
    assert read(layers, 'rent', 'full')['elements'][0]['text'] == 'Year 1 1450\nYear 2 1500'
    assert len(json.dumps(summary).split()) < len(json.dumps(read(layers, 'section', 'full')).split()) / 4
    assert set(read(layers, 'section', 'pointer')) == {'element_ids', 'entity_ids', 'claim_ids'}
