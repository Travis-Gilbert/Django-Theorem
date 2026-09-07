"""Resolve verbatim evidence quotes to unambiguous UTF-8 byte ranges."""
from __future__ import annotations


def resolve_span(text: str, quote: str, *, char_hint: int | None = None) -> tuple[int, int]:
    if not quote:
        raise ValueError("claim quote must not be empty")
    if char_hint is not None:
        if char_hint < 0 or text[char_hint:char_hint + len(quote)] != quote:
            raise ValueError("claim quote does not match its source position")
        start = char_hint
    else:
        start = text.find(quote)
        if start < 0:
            raise ValueError("claim quote is absent from the element")
        if text.find(quote, start + 1) >= 0:
            raise ValueError("repeated quote requires a source position")
    begin = len(text[:start].encode("utf-8"))
    end = begin + len(quote.encode("utf-8"))
    if text.encode("utf-8")[begin:end].decode("utf-8") != quote:
        raise ValueError("claim evidence span is not recoverable")
    return begin, end
