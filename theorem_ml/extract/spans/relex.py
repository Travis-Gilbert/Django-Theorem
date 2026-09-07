"""Evaluation-only GLiNER-Relex; no schema writer or ingestion entrypoint."""

MODEL_ID = "knowledgator/gliner-relex-large-v1.0"
MODEL_REVISION = "4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290"


class RelexEvaluator:
    def __init__(self) -> None:
        self._model = None

    def predict(self, texts: list[str], entity_labels: list[str], relation_labels: list[str]) -> dict:
        if self._model is None:
            try:
                from gliner import GLiNER
            except ImportError as error:
                raise RuntimeError("Install gliner in the isolated evaluation environment to run Relex") from error
            from huggingface_hub import snapshot_download
            checkpoint = snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION)
            self._model = GLiNER.from_pretrained(checkpoint)
            self._model.eval()
        entities, relations = self._model.inference(
            texts=texts, labels=entity_labels, relations=relation_labels,
            return_relations=True, flat_ner=False,
        )
        return {"evidence_class": "model_inference", "model": MODEL_ID,
                "revision": MODEL_REVISION, "entities": entities, "relations": relations}
