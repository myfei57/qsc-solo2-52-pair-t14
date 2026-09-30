"""Routes for controlled stage rollback and realignment."""

from __future__ import annotations

from typing import Any

from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/rollback/begin", "rollback.begin"),
    ("POST", "/api/rollback/realign", "rollback.realign"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the rollback routes."""

    router.add("GET", "/api/rollback", lambda _: Response.json(runtime.rollback_service.status()))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
