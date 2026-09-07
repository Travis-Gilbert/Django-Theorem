"""Pinned MiniCheck inference; probabilities are evidence, not truth."""
from __future__ import annotations

import math
from dataclasses import replace

CHECKPOINT = 'lytang/MiniCheck-Flan-T5-Large'
REVISION = '96eafd01cee2d16cf81aaa2fb226b14f422a37b3'


def load_minicheck(*, cache_dir=None, device='cpu', batch_size=8):
    # Preserve the upstream scoring/chunking code. Only checkpoint loading is
    # replaced, because MiniCheck's public constructor cannot pin a revision.
    # Reference: Liyan06/MiniCheck b58b9fa69acbd1015ec970fa65dd752413a053d2.
    import nltk
    import torch
    # Current NLTK separates the sentence tables from the legacy punkt model;
    # MiniCheck's upstream import only provisions the latter.
    try:
        nltk.data.find('tokenizers/punkt_tab/english/')
    except LookupError:
        nltk.download('punkt_tab', quiet=True, raise_on_error=True)
    from minicheck.inference import Inferencer
    from minicheck.minicheck import MiniCheck
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    class PinnedInferencer(Inferencer):
        def __init__(self):
            self.model_name = 'flan-t5-large'
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                CHECKPOINT, revision=REVISION, cache_dir=cache_dir,
                # This pinned revision publishes PyTorch weights. Explicitly
                # select them so Transformers cannot fetch a conversion PR
                # from a different revision in a background thread.
                trust_remote_code=False, use_safetensors=False).to(device)
            self.tokenizer = AutoTokenizer.from_pretrained(
                CHECKPOINT, revision=REVISION, cache_dir=cache_dir,
                trust_remote_code=False)
            self.model.eval()
            self.max_model_len = 2048
            self.max_output_length = 256
            self.batch_size = batch_size
            self.softmax = torch.nn.Softmax(dim=-1)

    scorer = MiniCheck.__new__(MiniCheck)
    scorer.model = PinnedInferencer()
    return scorer


class ClaimVerifier:
    def __init__(self, *, cutoff=0.5, scorer=None, cache_dir=None, device='cpu'):
        if not math.isfinite(cutoff) or not 0 <= cutoff <= 1:
            raise ValueError('MiniCheck cutoff must be finite and between zero and one')
        self.cutoff = cutoff
        self._scorer = scorer
        self.cache_dir = cache_dir
        self.device = device

    def verify(self, tree, claims):
        if not claims:
            return []
        docs = []
        for claim in claims:
            element = tree.get(claim.element_id)
            raw = element.text.encode('utf-8')
            if raw[claim.byte_start:claim.byte_end] != claim.quote.encode('utf-8'):
                raise ValueError('claim quote/span must match its element before verification')
            if not claim.text.strip():
                raise ValueError('claim text must not be empty')
            docs.append(element.text)
        if self._scorer is None:
            self._scorer = load_minicheck(cache_dir=self.cache_dir, device=self.device)
        _, probabilities, _, _ = self._scorer.score(docs=docs, claims=[c.text for c in claims])
        if len(probabilities) != len(claims):
            raise ValueError('MiniCheck returned the wrong number of scores')
        result = []
        for claim, probability in zip(claims, probabilities):
            score = float(probability)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('MiniCheck returned an invalid probability')
            result.append(replace(claim, score=score, verified=score > self.cutoff))
        return result
