"""Routes for stage rollbacks and realignment confirmations."""

from __future__ import annotations

from typing import Any

from .router import Request, Response, Router


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the rollback routes."""

    router.add("GET", "/api/rollbacks", lambda _: Response.json(runtime.rollbacks.status()))
    router.add("POST", "/api/lines/{line}/rollback", _rollback(runtime))
    router.add("POST", "/api/lines/{line}/realign", _realign(runtime))


def _rollback(runtime: Any):
    def handler(request: Request) -> Response:
        line = request.params["line"]
        reason = request.require_field("reason")
        result = runtime.rollback_phase(line, reason=str(reason))
        return Response.json(result)

    return handler


def _realign(runtime: Any):
    def handler(request: Request) -> Response:
        line = request.params["line"]
        reason = request.require_field("reason")
        confirmer = request.field("confirmer", "operator")
        result = runtime.confirm_realignment(line, reason=str(reason), confirmer=str(confirmer))
        return Response.json(result)

    return handler
