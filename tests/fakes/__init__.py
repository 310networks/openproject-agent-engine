"""In-memory fakes used by the test suite."""

from __future__ import annotations

from typing import Iterable

import httpx


def non_write_requests(requests: Iterable[httpx.Request]) -> list[httpx.Request]:
    """Return requests that do not mutate OpenProject state.

    ``POST /api/v3/work_packages/form`` is included: it renders a form schema
    (including the ``allowedValues`` links that the schemas endpoint omits on
    OpenProject 17.x) and writes nothing.
    """
    reads: list[httpx.Request] = []
    for request in requests:
        if request.method == "GET":
            reads.append(request)
        elif (
            request.method == "POST"
            and request.url.path == "/api/v3/work_packages/form"
        ):
            reads.append(request)
    return reads
