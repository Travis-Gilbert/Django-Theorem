"""Route-specific wire admission before Ninja parses theorem.layout.v1."""

from __future__ import annotations

from io import BytesIO
from time import monotonic

from django.http import JsonResponse

from apps.layout.budget import (
    LAYOUT_COMPUTE_PATH,
    LAYOUT_REQUEST_DEADLINE_ATTRIBUTE,
    LAYOUT_REQUEST_TIMEOUT_SECONDS,
    MAX_LAYOUT_REQUEST_BYTES,
)


def _request_too_large() -> JsonResponse:
    return JsonResponse(
        {"detail": f"layout request exceeds {MAX_LAYOUT_REQUEST_BYTES} bytes"},
        status=413,
    )


def _request_timed_out() -> JsonResponse:
    return JsonResponse(
        {"detail": "layout request exceeded its whole-request deadline"},
        status=504,
    )


class LayoutRequestBudgetMiddleware:
    """Bound only the layout route without changing unrelated upload policy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "POST" or request.path_info != LAYOUT_COMPUTE_PATH:
            return self.get_response(request)

        deadline = monotonic() + LAYOUT_REQUEST_TIMEOUT_SECONDS
        setattr(request, LAYOUT_REQUEST_DEADLINE_ATTRIBUTE, deadline)
        declared_value = request.META.get("CONTENT_LENGTH")
        declared_length: int | None = None
        if declared_value not in {None, ""}:
            try:
                declared_length = int(declared_value)
            except (TypeError, ValueError):
                return _request_too_large()
            if declared_length < 0 or declared_length > MAX_LAYOUT_REQUEST_BYTES:
                return _request_too_large()

        try:
            body = request.read(MAX_LAYOUT_REQUEST_BYTES + 1)
        except OSError:
            return JsonResponse(
                {"detail": "layout request body could not be read"}, status=400
            )
        if monotonic() >= deadline:
            return _request_timed_out()
        if len(body) > MAX_LAYOUT_REQUEST_BYTES:
            return _request_too_large()
        if declared_length is not None and declared_length != len(body):
            return JsonResponse(
                {"detail": "layout request Content-Length does not match its body"},
                status=400,
            )

        request._body = body
        request._stream = BytesIO(body)
        response = self.get_response(request)
        if monotonic() >= deadline and response.status_code != 504:
            return _request_timed_out()
        return response
