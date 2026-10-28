"""Minimal in-memory OpenProject API for tests.

Only the endpoints needed by startup resolution are implemented. Responses use
the real HAL collection shape (``_embedded.elements`` plus
``_links.nextByOffset``), so pagination and redirect behaviour are exercised
the same way as against the live API.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urlencode

import httpx

JSON = Any

_PROJECT = re.compile(r"^/api/v3/projects/(?P<identifier>[^/]+)$")
_PROJECT_TYPES = re.compile(r"^/api/v3/projects/(?P<project_id>[^/]+)/types$")
_SCHEMA = re.compile(r"^/api/v3/work_packages/schemas/(?P<identifier>[^/]+)$")


class FakeOpenProject:
    def __init__(
        self,
        *,
        statuses: list[dict[str, JSON]] | None = None,
        users: list[dict[str, JSON]] | None = None,
        projects: list[dict[str, JSON]] | None = None,
        types: dict[str, list[dict[str, JSON]]] | None = None,
        schemas: dict[str, dict[str, JSON]] | None = None,
        allow_user_listing: bool = True,
        expected_token: str | None = "test-token",
        max_page_size: int = 100,
    ) -> None:
        self.statuses = statuses or []
        self.users = users or []
        self.projects = projects or []
        self.types = types or {}
        self.schemas = schemas or {}
        self.allow_user_listing = allow_user_listing
        self.expected_token = expected_token
        self.max_page_size = max_page_size
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- request handling ------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.expected_token is not None and not self._authorized(request):
            return self._error(401, "Unauthorized")
        if request.method != "GET":
            return self._error(405, "Method not allowed")

        path = request.url.path
        if path == "/api/v3/statuses":
            return self._collection(request, self.statuses, "/api/v3/statuses")
        if path == "/api/v3/users":
            if not self.allow_user_listing:
                return self._error(
                    403, "You are not authorized to access this resource."
                )
            return self._collection(
                request, self._filter_users(request), "/api/v3/users"
            )

        types_match = _PROJECT_TYPES.match(path)
        if types_match:
            project = self._project_by_id(types_match.group("project_id"))
            if project is None:
                return self._error(404, "The requested resource could not be found.")
            identifier = str(project["identifier"])
            return self._collection(
                request,
                self.types.get(identifier, []),
                path,
            )

        project_match = _PROJECT.match(path)
        if project_match:
            project = self._project_by_identifier(project_match.group("identifier"))
            if project is None:
                return self._error(404, "The requested resource could not be found.")
            return self._json(project)

        schema_match = _SCHEMA.match(path)
        if schema_match:
            schema = self.schemas.get(schema_match.group("identifier"))
            if schema is None:
                return self._error(404, "The requested resource could not be found.")
            return self._json(schema)

        return self._error(404, "The requested resource could not be found.")

    def _authorized(self, request: httpx.Request) -> bool:
        header = request.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(
            f"apikey:{self.expected_token}".encode()
        ).decode()
        return header == expected

    def _filter_users(self, request: httpx.Request) -> list[dict[str, JSON]]:
        filters_raw = request.url.params.get("filters")
        if not filters_raw:
            return self.users
        try:
            filters = json.loads(filters_raw)
        except ValueError:
            return []
        if not isinstance(filters, list):
            return []
        logins: set[str] = set()
        for condition in filters:
            if not isinstance(condition, dict):
                continue
            login_filter = condition.get("login")
            if not isinstance(login_filter, dict):
                continue
            values = login_filter.get("values")
            if isinstance(values, list):
                logins.update(str(value) for value in values)
        if not logins:
            return self.users
        return [user for user in self.users if user.get("login") in logins]

    def _project_by_identifier(self, identifier: str) -> dict[str, JSON] | None:
        for project in self.projects:
            if project.get("identifier") == identifier:
                return project
        return None

    def _project_by_id(self, project_id: str) -> dict[str, JSON] | None:
        for project in self.projects:
            if str(project.get("id")) == project_id:
                return project
        return None

    # -- response helpers ------------------------------------------------

    def _collection(
        self,
        request: httpx.Request,
        elements: list[dict[str, JSON]],
        path: str,
    ) -> httpx.Response:
        params = dict(request.url.params)
        page_size = min(self.max_page_size, int(params.get("pageSize", 20)))
        offset = int(params.get("offset", 1))
        start = max(0, (offset - 1) * page_size)
        page = elements[start : start + page_size]
        payload: dict[str, JSON] = {
            "_type": "Collection",
            "count": len(page),
            "total": len(elements),
            "offset": offset,
            "pageSize": page_size,
            "_embedded": {"elements": page},
        }
        if start + page_size < len(elements):
            next_params = {**params, "offset": str(offset + 1)}
            payload["_links"] = {
                "nextByOffset": {"href": f"{path}?{urlencode(next_params)}"}
            }
        return self._json(payload)

    @staticmethod
    def _json(payload: JSON, status_code: int = 200) -> httpx.Response:
        return httpx.Response(
            status_code,
            json=payload,
            headers={"Content-Type": "application/hal+json"},
        )

    @staticmethod
    def _error(status_code: int, message: str) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"_type": "Error", "message": message},
            headers={"Content-Type": "application/hal+json"},
        )
