"""Production extraction executors with no fabricated fitness values."""

from dataclasses import asdict


def parse_paddle(payload, *, tenant):
    import base64
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from theorem_ml.extract.parse.paddle import PaddleParser
    # Machine callers submit bytes; arbitrary server-local paths are not an API.
    image = base64.b64decode(payload["image_base64"], validate=True)
    if len(image) > 50 * 1024 * 1024:
        raise ValueError("Paddle input exceeds the 50 MiB page limit")
    with TemporaryDirectory(prefix="theorem-paddle-") as temporary:
        path = Path(temporary) / "page.png"
        path.write_bytes(image)
        tree = PaddleParser().parse(path, payload["source_id"], page=int(payload.get("page", 1)))
    return {"implementation": "paddleocr_vl_1_6", "tree": tree.to_dict(), "fitness": None}


def spans_gliner2(payload, *, tenant):
    from theorem_ml.extract.elements import DocElementTree
    from theorem_ml.extract.spans.gliner2 import SpanExtractor
    result = SpanExtractor().extract(DocElementTree.from_dict(payload["document"]), tenant, payload["object_types"])
    return {"implementation": "gliner2_large_v1", **asdict(result), "fitness": None}


def claims_minicheck(payload, *, tenant):
    from theorem_ml.extract.elements import DocElementTree
    from theorem_ml.extract.claims.decompose import Claim
    from theorem_ml.extract.claims.verify import ClaimVerifier
    tree = DocElementTree.from_dict(payload["document"])
    claims = [Claim(**{k: v for k, v in c.items() if k != "id"}) for c in payload["claims"]]
    result = ClaimVerifier(cutoff=float(payload.get("cutoff", .5))).verify(tree, claims)
    return {"implementation": "minicheck_flan_t5_large", "claims": [c.to_dict() for c in result], "fitness": None}


def resolve_link(payload, *, tenant):
    from theorem_ml.extract.resolve import resolve_mentions
    from theorem_ml.extract.spans.labels import SchemaClient
    return {"implementation": "rustyred_thg_resolve", "relations": resolve_mentions(
        payload["spans"], tenant=tenant, client=SchemaClient()), "fitness": None}
