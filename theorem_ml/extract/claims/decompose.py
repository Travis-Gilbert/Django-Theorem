"""Deterministic dependency-parse propositions; generative escalation is separate."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from .resolve_span import resolve_span


@dataclass
class Claim:
    text: str
    quote: str
    element_id: str
    byte_start: int
    byte_end: int
    subject_id: str | None = None
    predicate: str | None = None
    object_text: str | None = None
    score: float | None = None
    verified: bool = False
    entity_mentions: list[dict] = field(default_factory=list)

    @property
    def id(self) -> str:
        value = f"{self.element_id}\0{self.byte_start}\0{self.byte_end}\0{self.text}"
        return 'claim:' + hashlib.sha256(value.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}


@dataclass
class Decomposition:
    claims: list[Claim] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)


class Decomposer:
    def __init__(self, model: str = "en_core_web_sm", *, nlp: Any = None):
        if nlp is None:
            import spacy
            nlp = spacy.load(model, disable=["ner"])
        if "parser" not in nlp.pipe_names:
            raise ValueError("claim decomposition requires a dependency parser")
        self.nlp = nlp
        self.model = model

    def decompose(self, tree, *, derive_claims: bool, resolved_entities: list[dict] = (), declared_predicates=()) -> Decomposition:
        result = Decomposition()
        if not derive_claims:
            return result
        for element in tree.elements:
            if element.kind not in {"Paragraph", "ListItem", "Footnote"}:
                continue
            mentions = [m for m in resolved_entities if m.get("element_id") == element.id]
            parsed = self.nlp(element.text)
            for sentence in parsed.sents:
                candidates = self._sentence_claims(sentence, element, mentions, declared_predicates)
                finite = any(t.pos_ in {"VERB", "AUX"} and
                             ("Fin" in t.morph.get("VerbForm") or t.tag_ in {"VBD", "VBP", "VBZ", "MD"})
                             for t in sentence)
                if not candidates and finite:
                    result.ambiguous.append({"element_id": element.id, "quote": sentence.text,
                                             "reason": "finite_verb_without_claim"})
                for text, subject, predicate, object_text in candidates:
                    try:
                        start, end = resolve_span(element.text, sentence.text, char_hint=sentence.start_char)
                    except ValueError:
                        result.ambiguous.append({"element_id": element.id, "quote": sentence.text,
                                                 "reason": "unresolved_quote"})
                        continue
                    sentence_mentions = [m for m in mentions if start <= m['byte_start'] < end]
                    claim = Claim(text=text, quote=sentence.text, element_id=element.id,
                                  byte_start=start, byte_end=end, subject_id=subject,
                                  predicate=predicate, object_text=object_text,
                                  entity_mentions=sentence_mentions)
                    if claim.id not in {c.id for c in result.claims}:
                        result.claims.append(claim)
        return result

    def _sentence_claims(self, sentence, element, mentions, declared_predicates):
        clauses = [t for t in sentence if t.dep_ == 'ROOT' or
                   (t.dep_ in {'relcl', 'conj', 'acl'} and t.pos_ in {'VERB', 'AUX'})]
        outputs = []
        for verb in clauses:
            if verb.pos_ not in {'VERB', 'AUX'}:
                continue
            excluded = {t.i for child in verb.children if child.dep_ in {'relcl', 'appos', 'conj', 'cc'}
                        for t in child.subtree}
            for token in verb.subtree:
                if token.dep_ in {'appos', 'relcl'} and token != verb:
                    excluded.update(t.i for t in token.subtree)
            tokens = [t for t in verb.subtree if t.i not in excluded and not t.is_punct]
            subjects = [t for t in verb.children if t.dep_ in {'nsubj', 'nsubjpass', 'csubj'}]
            inherited = verb
            while not subjects and inherited.dep_ == 'conj':
                inherited = inherited.head
                subjects = [t for t in inherited.children if t.dep_ in {'nsubj', 'nsubjpass'}]
            # spaCy may attach a finite predicate after a comma-delimited
            # apposition as acl. Its subject is the apposition's antecedent.
            if not subjects and inherited.dep_ == 'acl' and inherited.head.dep_ == 'appos':
                subjects = [inherited.head.head]
            if subjects and not any(t.i == subjects[0].i for t in tokens):
                tokens = self._noun_tokens(subjects[0]) + tokens
            replacements = {}
            if verb.dep_ == 'relcl':
                antecedent = verb.head
                name = self._noun_phrase(antecedent)
                for token in tokens:
                    if token.lower_ in {'who', 'which', 'that', 'whom'}:
                        replacements[token.i] = name
                if not subjects:
                    tokens = [antecedent] + tokens
            for token in tokens:
                if token.pos_ == 'PRON' and token.i not in replacements:
                    offset = len(element.text[:token.idx].encode('utf-8'))
                    prior = [m for m in mentions if m.get('entity_id') and m['byte_end'] <= offset]
                    if prior:
                        name = max(prior, key=lambda m: m['byte_end'])['text']
                        replacements[token.i] = name + ("'s" if token.dep_ == 'poss' else '')
            text = ' '.join(replacements.get(t.i, t.text) for t in tokens).strip()
            if not text:
                continue
            subject_id = None
            if subjects:
                subject = subjects[0]
                subject_text = replacements.get(subject.i, self._noun_phrase(subject))
                candidates = [m for m in mentions if m.get('entity_id') and
                              m['text'].casefold() == subject_text.casefold()]
                if candidates:
                    subject_id = candidates[-1]['entity_id']
            objects = [t for t in verb.children if t.dep_ in {'dobj', 'attr', 'acomp', 'oprd'}]
            object_text = ' '.join(t.text for t in objects[0].subtree) if objects else None
            predicate = verb.lemma_
            if subjects and verb.lemma_ == 'be':
                # A copular assertion about a declared scalar field names that
                # predicate explicitly ("its deadline is Friday"). The owning
                # entity must already have a resolved mention in this element.
                key = next((key for key in declared_predicates
                            if subjects[0].lemma_.casefold() == key.casefold()), None)
                if key:
                    predicate = key
                    offset = len(element.text[:subjects[0].idx].encode())
                    prior = [m for m in mentions if m.get('entity_id') and m['byte_end'] <= offset]
                    if prior:
                        subject_id = max(prior, key=lambda m: m['byte_end'])['entity_id']
            outputs.append((text, subject_id, predicate, object_text))
        for appos in (t for t in sentence if t.dep_ == 'appos'):
            head = self._noun_phrase(appos.head)
            excluded = {t.i for child in appos.children if child.dep_ in {'acl', 'relcl', 'conj'} for t in child.subtree}
            description = ' '.join(t.text for t in appos.subtree if not t.is_punct and t.i not in excluded)
            candidates = [m for m in mentions if m.get('entity_id') and m['text'].casefold() == head.casefold()]
            outputs.append((f'{head} is {description}', candidates[-1]['entity_id'] if candidates else None,
                            'be', description))
        return outputs

    @staticmethod
    def _noun_tokens(token):
        members = [token]
        for child in token.children:
            if child.dep_ in {'compound', 'det', 'amod', 'poss', 'nummod'}:
                members.extend(Decomposer._noun_tokens(child))
        return sorted(members, key=lambda t: t.i)

    @staticmethod
    def _noun_phrase(token):
        return ' '.join(t.text for t in Decomposer._noun_tokens(token))
