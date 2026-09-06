"""The same machine-key admission as other Django internal executor APIs."""

from ninja import Body, Router
from ninja.errors import HttpError

from apps.keys.auth import OFFLOAD_INVOKE_SCOPE, require_machine_key

router = Router(tags=["index"])


@router.post("/execute")
def execute_index(request, body: dict = Body(...)):
    principal = require_machine_key(request, scope=OFFLOAD_INVOKE_SCOPE)
    payload = dict(body)
    name = payload.pop("executor", "")
    try:
        from .registry import execute

        return execute(name, payload, tenant=principal.tenant.slug)
    except ValueError as error:
        raise HttpError(422, str(error)) from error
    except (ImportError, RuntimeError, OSError) as error:
        raise HttpError(
            503, "Index executor dependency or runtime unavailable"
        ) from error
