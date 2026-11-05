"""Minimal in-memory OpenProject API for tests.

Implements the endpoints needed by startup resolution, the M2 poller and the
M3 scheduler (statuses, users, projects, types, schemas, priorities, work
packages, relations and work package updates). Responses use the real HAL
collection shape (``_embedded.elements`` plus ``_links.nextByOffset``), so
pagination and redirect behaviour are exercised the same way as against the
live API.

Work package, relation and priority fixtures are plain dicts shaped like the
live HAL entries; the fake applies the ``project``/``status``/``type``/
``parent`` and ``involved`` filters and the ``sortBy`` order the poller uses.

``PATCH /api/v3/work_packages/<id>`` mirrors the verified live behaviour:
the body must carry the current ``lockVersion`` or the fake answers 409
``UpdateConflict``; on success the status/assignee links are applied and the
lockVersion is incremented.
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
_ID_FROM_HREF = re.compile(r"/(?P<id>\d+)/?$")
_WP_BY_ID = re.compile(r"^/api/v3/work_packages/(?P<id>\d+)$")
_WP_ACTIVITIES = re.compile(r"^/api/v3/work_packages/(?P<id>\d+)/activities$")
_USER_BY_ID = re.compile(r"^/api/v3/users/(?P<id>\d+)$")
_USER_LOCK = re.compile(r"^/api/v3/users/(?P<id>\d+)/lock$")
_MEMBERSHIP_BY_ID = re.compile(r"^/api/v3/memberships/(?P<id>\d+)$")
_HREF_ID = re.compile(r"/(\d+)$")


def _link(entry: dict[str, JSON], name: str) -> dict[str, JSON] | None:
    links = entry.get("_links")
    if not isinstance(links, dict):
        return None
    value = links.get(name)
    return value if isinstance(value, dict) else None


def _href_id(link: dict[str, JSON] | None) -> int | None:
    if not isinstance(link, dict):
        return None
    href = link.get("href")
    if not isinstance(href, str):
        return None
    match = _HREF_ID.search(href)
    return int(match.group(1)) if match else None


def _wp_id(wp: dict[str, JSON] | None) -> int | None:
    if not isinstance(wp, dict):
        return None
    if isinstance(wp.get("id"), int):
        return wp["id"]
    links = wp.get("_links")
    if isinstance(links, dict):
        return _href_id(links.get("self"))
    return None


def _work_package_id(entry: dict[str, JSON]) -> int | None:
    """The work package id of an activity or comment fixture."""
    value = entry.get("workPackageId")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _wp_id(entry.get("workPackage"))


class FakeOpenProject:
    def __init__(
        self,
        *,
        statuses: list[dict[str, JSON]] | None = None,
        users: list[dict[str, JSON]] | None = None,
        projects: list[dict[str, JSON]] | None = None,
        project_schema: dict[str, JSON] | None = None,
        types: dict[str, list[dict[str, JSON]]] | None = None,
        schemas: dict[str, dict[str, JSON]] | None = None,
        allow_user_listing: bool = True,
        expected_token: str | None = "test-token",
        max_page_size: int = 100,
        work_packages: list[dict[str, JSON]] | None = None,
        relations: list[dict[str, JSON]] | None = None,
        priorities: list[dict[str, JSON]] | None = None,
        comments: list[dict[str, JSON]] | None = None,
        activities: list[dict[str, JSON]] | None = None,
        posting_user_id: int | None = 10,
        patch_error: tuple[int, str] | None = None,
        form_error: tuple[int, str] | None = None,
        delete_user_error: tuple[int, str] | None = None,
        roles: list[dict[str, JSON]] | None = None,
        memberships: list[dict[str, JSON]] | None = None,
        token_users: dict[str, int] | None = None,
    ) -> None:
        self.statuses = statuses or []
        self.users = users or []
        self.projects = projects or []
        self.project_schema = project_schema or {}
        self.types = types or {}
        self.schemas = schemas or {}
        self.allow_user_listing = allow_user_listing
        self.expected_token = expected_token
        # Additional token -> user id mappings used by the provisioning and
        # token-verification tests. ``expected_token`` stays the default
        # engine token mapped to ``posting_user_id``.
        self.token_users: dict[str, int] = dict(token_users or {})
        if expected_token and posting_user_id is not None:
            self.token_users.setdefault(expected_token, posting_user_id)
        self.roles: list[dict[str, JSON]] = list(roles or [])
        self.memberships: list[dict[str, JSON]] = list(memberships or [])
        self.max_page_size = max_page_size
        self.work_packages = list(work_packages or [])
        # The live API always includes ``lockVersion`` in work package
        # payloads (readOnly on reads, required on updates); normalise
        # fixtures that omit it.
        for wp in self.work_packages:
            if isinstance(wp, dict):
                wp.setdefault("lockVersion", 0)
        self.relations = relations or []
        self.priorities = priorities or []
        # Comment fixtures: dicts with at least ``workPackageId``,
        # ``authorId``, ``createdAt`` and ``content``. Posted comments are
        # appended here with auto-assigned ids.
        self.comments: list[dict[str, JSON]] = list(comments or [])
        # Activity fixtures shaped like the live API:
        # ``_type``, ``comment.raw``, ``details[]``, ``createdAt`` and
        # ``_links.user``. Posted engine comments are mirrored here so a
        # reviewer-comment check sees a realistic history.
        self.activities: list[dict[str, JSON]] = list(activities or [])
        # The user the API token belongs to; posted comments are authored by
        # this user (the engine user in the real deployment).
        self.posting_user_id = posting_user_id
        self._next_comment_id = (
            max((int(c["id"]) for c in self.comments if isinstance(c.get("id"), int)), default=0)
            + 1
        )
        self._next_activity_id = max(
            (int(a["id"]) for a in self.activities if isinstance(a.get("id"), int)),
            default=0,
        ) + 1
        self.requests: list[httpx.Request] = []
        # Bodies of every successful PATCH /api/v3/work_packages/<id>.
        self.patches: list[dict[str, JSON]] = []
        # When set, every PATCH returns this (status, message) instead.
        self.patch_error = patch_error
        # When set, POST /api/v3/work_packages/form returns this instead, so
        # tests can exercise the schema-endpoint fallback.
        self.form_error = form_error
        # When set, DELETE /api/v3/users/<id> returns this (status, message).
        self.delete_user_error = delete_user_error

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- request handling ------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._authorized(request):
            return self._error(401, "Unauthorized")

        path = request.url.path
        if request.method == "DELETE":
            user_by_id = _USER_BY_ID.match(path)
            if user_by_id:
                return self._delete_user(int(user_by_id.group("id")), request)
            membership_by_id = _MEMBERSHIP_BY_ID.match(path)
            if membership_by_id:
                return self._delete_membership(
                    int(membership_by_id.group("id")), request
                )
            return self._error(404, "The requested resource could not be found.")
        if request.method == "PATCH":
            wp_by_id = _WP_BY_ID.match(path)
            if wp_by_id:
                return self._patch_work_package(int(wp_by_id.group("id")), request)
            user_by_id = _USER_BY_ID.match(path)
            if user_by_id:
                return self._patch_user(int(user_by_id.group("id")), request)
            membership_by_id = _MEMBERSHIP_BY_ID.match(path)
            if membership_by_id:
                return self._patch_membership(
                    int(membership_by_id.group("id")), request
                )
            return self._error(404, "The requested resource could not be found.")
        if request.method == "POST":
            activities_match = _WP_ACTIVITIES.match(path)
            if activities_match:
                return self._post_comment(int(activities_match.group("id")), request)
            if path == "/api/v3/work_packages/form":
                return self._post_form(request)
            if path == "/api/v3/users":
                return self._create_user(request)
            if path == "/api/v3/memberships":
                return self._create_membership(request)
            lock_match = _USER_LOCK.match(path)
            if lock_match:
                if request.headers.get("content-type") != "application/json":
                    return self._error(406, "Missing content-type header")
                return self._lock_user(int(lock_match.group("id")))
            return self._error(404, "The requested resource could not be found.")
        if request.method != "GET":
            return self._error(405, "Method not allowed")

        if path == "/api/v3/users/me":
            return self._current_user(request)
        if path == "/api/v3/roles":
            return self._collection(request, self.roles, "/api/v3/roles")
        if path == "/api/v3/memberships":
            return self._collection(
                request,
                self._filter_memberships(request),
                "/api/v3/memberships",
            )

        activities_match = _WP_ACTIVITIES.match(path)
        if activities_match:
            work_package_id = int(activities_match.group("id"))
            elements = [
                activity
                for activity in self.activities
                if _work_package_id(activity) == work_package_id
            ]
            return self._collection(request, elements, path)

        if path == "/api/v3/statuses":
            return self._collection(request, self.statuses, "/api/v3/statuses")
        if path == "/api/v3/priorities":
            return self._collection(request, self.priorities, "/api/v3/priorities")
        if path == "/api/v3/work_packages":
            elements = self._sort_work_packages(
                self._filter_work_packages(request), request
            )
            return self._collection(request, elements, "/api/v3/work_packages")

        wp_by_id = _WP_BY_ID.match(path)
        if wp_by_id:
            wanted = int(wp_by_id.group("id"))
            for wp in self.work_packages:
                if wp.get("id") == wanted:
                    return self._json(wp)
            return self._error(404, "The requested resource could not be found.")
        if path == "/api/v3/relations":
            return self._collection(
                request, self._filter_relations(request), "/api/v3/relations"
            )
        if path == "/api/v3/users":
            if not self.allow_user_listing:
                return self._error(
                    403, "You are not authorized to access this resource."
                )
            return self._collection(
                request, self._filter_users(request), "/api/v3/users"
            )

        if path == "/api/v3/projects":
            return self._collection(request, self.projects, "/api/v3/projects")
        if path == "/api/v3/projects/schema":
            return self._json(self.project_schema)

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

    def _post_form(self, request: httpx.Request) -> httpx.Response:
        """Mirror POST /api/v3/work_packages/form.

        The live API (OpenProject 17.x) embeds the type schema, including the
        ``allowedValues`` links that the plain schemas endpoint omits.
        """
        if self.form_error is not None:
            status, message = self.form_error
            return self._error(status, message)
        try:
            body = json.loads(request.content.decode())
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._error(400, "Malformed request body.")
        links = [body.get("project"), body.get("type")]
        identifiers: list[str] = []
        for link in links:
            href = link.get("href") if isinstance(link, dict) else None
            match = _ID_FROM_HREF.search(href) if isinstance(href, str) else None
            if match is None:
                return self._error(400, "Malformed request body.")
            identifiers.append(match.group("id"))
        project = self._project_by_id(identifiers[0])
        if project is None:
            return self._error(404, "The requested resource could not be found.")
        schema = self.schemas.get(f"{identifiers[0]}-{identifiers[1]}")
        if schema is None:
            return self._error(404, "The requested resource could not be found.")
        return self._json({"_type": "Form", "_embedded": {"schema": schema}})

    def _patch_work_package(
        self, work_package_id: int, request: httpx.Request
    ) -> httpx.Response:
        """Mirror the verified live PATCH behaviour.

        * ``lockVersion`` is required in the body; missing or stale -> 409
          ``UpdateConflict``.
        * ``_links.status`` / ``_links.assignee`` are applied.
        * The lockVersion is incremented on success.
        """
        wp = next(
            (w for w in self.work_packages if w.get("id") == work_package_id), None
        )
        if wp is None:
            return self._error(404, "The requested resource could not be found.")
        if self.patch_error is not None:
            status, message = self.patch_error
            return self._error(status, message)

        try:
            body = json.loads(request.content.decode())
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._error(400, "Malformed request body.")

        current_version = wp.get("lockVersion", 0)
        sent_version = body.get("lockVersion")
        if not isinstance(sent_version, int) or isinstance(sent_version, bool):
            return self._conflict()
        if sent_version != current_version:
            return self._conflict()

        self.patches.append({"work_package_id": work_package_id, "body": body})
        links = body.get("_links")
        if isinstance(links, dict):
            wp_links = wp.setdefault("_links", {})
            for name in ("status", "assignee"):
                link = links.get(name)
                if isinstance(link, dict) and isinstance(link.get("href"), str):
                    wp_links[name] = {"href": link["href"]}
        wp["lockVersion"] = current_version + 1
        return self._json(wp)

    @staticmethod
    def _conflict() -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "_type": "Error",
                "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
                "message": "Could not update the resource because of "
                "conflicting modifications.",
            },
            headers={"Content-Type": "application/hal+json"},
        )

    @staticmethod
    def _request_token(request: httpx.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:]).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        username, _, token = decoded.partition(":")
        return token if username == "apikey" else None

    def _authorized(self, request: httpx.Request) -> bool:
        if self.expected_token is None and not self.token_users:
            return True
        token = self._request_token(request)
        if token is None:
            return False
        return token == self.expected_token or token in self.token_users

    def _current_user(self, request: httpx.Request) -> httpx.Response:
        token = self._request_token(request)
        user_id = self.token_users.get(token or "")
        if user_id is None:
            user_id = self.posting_user_id
        user = next(
            (entry for entry in self.users if entry.get("id") == user_id), None
        )
        if user is None:
            return self._error(404, "The requested resource could not be found.")
        return self._json(user)

    def _create_user(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if not isinstance(body, dict):
            return self._error(400, "Malformed request body")
        login = body.get("login")
        if not isinstance(login, str) or not login:
            return self._error(422, "Login can't be blank")
        if any(user.get("login") == login for user in self.users):
            return self._error(422, "Login has already been taken")
        user_id = max(
            (int(user["id"]) for user in self.users if isinstance(user.get("id"), int)),
            default=0,
        ) + 1
        first = str(body.get("firstName") or "")
        last = str(body.get("lastName") or "")
        user: dict[str, JSON] = {
            "_type": "User",
            "id": user_id,
            "login": login,
            "name": " ".join(part for part in (first, last) if part),
            "firstName": first,
            "lastName": last,
            "email": body.get("email"),
            "admin": bool(body.get("admin", False)),
            "status": body.get("status", "active"),
            "_links": {
                "self": {"href": f"/api/v3/users/{user_id}", "title": login}
            },
        }
        self.users.append(user)
        return self._json(user, status_code=201)

    def _delete_user(
        self, user_id: int, request: httpx.Request
    ) -> httpx.Response:
        if self.delete_user_error is not None:
            return self._error(*self.delete_user_error)
        user = next(
            (entry for entry in self.users if entry.get("id") == user_id), None
        )
        if user is None:
            return self._error(404, "The requested resource could not be found.")
        self.users = [entry for entry in self.users if entry.get("id") != user_id]
        self.memberships = [
            entry
            for entry in self.memberships
            if _href_id(entry.get("_links", {}).get("principal")) != user_id
        ]
        return httpx.Response(204, request=request)

    def _lock_user(self, user_id: int) -> httpx.Response:
        user = next(
            (entry for entry in self.users if entry.get("id") == user_id), None
        )
        if user is None:
            return self._error(404, "The requested resource could not be found.")
        user["status"] = "locked"
        return self._json(user)

    def _delete_membership(
        self, membership_id: int, request: httpx.Request
    ) -> httpx.Response:
        membership = next(
            (
                entry
                for entry in self.memberships
                if entry.get("id") == membership_id
            ),
            None,
        )
        if membership is None:
            return self._error(404, "The requested resource could not be found.")
        self.memberships = [
            entry for entry in self.memberships if entry.get("id") != membership_id
        ]
        return httpx.Response(204, request=request)

    def _patch_user(
        self, user_id: int, request: httpx.Request
    ) -> httpx.Response:
        user = next(
            (entry for entry in self.users if entry.get("id") == user_id), None
        )
        if user is None:
            return self._error(404, "The requested resource could not be found.")
        body = self._body(request)
        if not isinstance(body, dict):
            return self._error(400, "Malformed request body")
        for key in ("firstName", "lastName", "email"):
            if key in body:
                user[key] = body[key]
        if "firstName" in body or "lastName" in body:
            user["name"] = " ".join(
                part
                for part in (
                    str(user.get("firstName") or ""),
                    str(user.get("lastName") or ""),
                )
                if part
            )
        return self._json(user)

    def _create_membership(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if not isinstance(body, dict):
            return self._error(400, "Malformed request body")
        links = body.get("_links")
        if not isinstance(links, dict):
            return self._error(422, "Roles can't be blank")
        principal = _href_id(links.get("principal"))
        project = _href_id(links.get("project"))
        roles = links.get("roles")
        if not isinstance(roles, list) or principal is None:
            return self._error(422, "Roles can't be blank")
        membership_id = max(
            (
                int(entry["id"])
                for entry in self.memberships
                if isinstance(entry.get("id"), int)
            ),
            default=0,
        ) + 1
        membership: dict[str, JSON] = {
            "_type": "Membership",
            "id": membership_id,
            "_links": {
                "self": {
                    "href": f"/api/v3/memberships/{membership_id}",
                },
                "principal": {"href": f"/api/v3/users/{principal}"},
                "project": {"href": f"/api/v3/projects/{project}"},
                "roles": [
                    {"href": role.get("href")}
                    for role in roles
                    if isinstance(role, dict)
                ],
            },
        }
        self.memberships.append(membership)
        return self._json(membership, status_code=201)

    def _patch_membership(
        self, membership_id: int, request: httpx.Request
    ) -> httpx.Response:
        membership = next(
            (
                entry
                for entry in self.memberships
                if entry.get("id") == membership_id
            ),
            None,
        )
        if membership is None:
            return self._error(404, "The requested resource could not be found.")
        body = self._body(request)
        links = body.get("_links") if isinstance(body, dict) else None
        roles = links.get("roles") if isinstance(links, dict) else None
        if not isinstance(roles, list):
            return self._error(422, "Roles can't be blank")
        membership_links = membership.setdefault("_links", {})
        membership_links["roles"] = [
            {"href": role.get("href")}
            for role in roles
            if isinstance(role, dict)
        ]
        return self._json(membership)

    def _filter_memberships(
        self, request: httpx.Request
    ) -> list[dict[str, JSON]]:
        conditions = self._filter_conditions(request) or {}
        result: list[dict[str, JSON]] = []
        for membership in self.memberships:
            links = membership.get("_links")
            if not isinstance(links, dict):
                continue
            principal = _href_id(links.get("principal"))
            project = _href_id(links.get("project"))
            if "principal" in conditions and str(principal) not in conditions["principal"]:
                continue
            if "project" in conditions and str(project) not in conditions["project"]:
                continue
            result.append(membership)
        return result

    @staticmethod
    def _body(request: httpx.Request) -> JSON | None:
        try:
            return json.loads(request.content.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None

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

    def _filter_work_packages(self, request: httpx.Request) -> list[dict[str, JSON]]:
        conditions = self._filter_conditions(request)
        if conditions is None:
            return self.work_packages
        return [
            wp
            for wp in self.work_packages
            if self._wp_matches_conditions(wp, conditions)
        ]

    def _wp_matches_conditions(
        self, wp: dict[str, JSON], conditions: dict[str, set[str]]
    ) -> bool:
        for key, wanted in conditions.items():
            if key == "id":
                # The engine batches blocker lookups with an id filter.
                if str(wp.get("id")) in wanted:
                    continue
                return False
            actual = _href_id(_link(wp, key))
            if actual is None or str(actual) not in wanted:
                return False
        return True

    def _sort_work_packages(
        self, elements: list[dict[str, JSON]], request: httpx.Request
    ) -> list[dict[str, JSON]]:
        raw = request.url.params.get("sortBy")
        if not raw:
            return elements
        try:
            sort_by = json.loads(raw)
        except ValueError:
            return elements
        if not isinstance(sort_by, list):
            return elements
        position = {
            p["id"]: p.get("position", 0)
            for p in self.priorities
            if isinstance(p, dict) and isinstance(p.get("id"), int)
        }

        def key(wp: dict[str, JSON]) -> tuple:
            parts: list[int] = []
            for clause in sort_by:
                if not isinstance(clause, str):
                    continue
                field, _, direction = clause.partition(":")
                descending = direction == "desc"
                if field == "id":
                    value = wp.get("id", 0)
                elif field == "priority":
                    value = position.get(_href_id(_link(wp, "priority")), 0)
                else:
                    value = 0
                parts.append(-value if descending else value)
            return tuple(parts)

        return sorted(elements, key=key)

    def _filter_relations(self, request: httpx.Request) -> list[dict[str, JSON]]:
        conditions = self._filter_conditions(request)
        if conditions is None or "involved" not in conditions:
            return self.relations
        involved = conditions["involved"]
        result = []
        for relation in self.relations:
            from_id, to_id = self._relation_endpoints(relation)
            if (from_id is not None and str(from_id) in involved) or (
                to_id is not None and str(to_id) in involved
            ):
                result.append(relation)
        return result

    def _relation_endpoints(
        self, relation: dict[str, JSON]
    ) -> tuple[int | None, int | None]:
        embedded = relation.get("_embedded") or {}
        from_id = _wp_id(embedded.get("from"))
        to_id = _wp_id(embedded.get("to"))
        links = relation.get("_links") or {}
        if from_id is None:
            from_id = _href_id(links.get("from"))
        if to_id is None:
            to_id = _href_id(links.get("to"))
        return from_id, to_id

    def _filter_conditions(
        self, request: httpx.Request
    ) -> dict[str, set[str]] | None:
        filters_raw = request.url.params.get("filters")
        if not filters_raw:
            return None
        try:
            filters = json.loads(filters_raw)
        except ValueError:
            return {}
        if not isinstance(filters, list):
            return {}
        conditions: dict[str, set[str]] = {}
        for condition in filters:
            if not isinstance(condition, dict):
                continue
            for key, spec in condition.items():
                if isinstance(spec, dict) and isinstance(spec.get("values"), list):
                    conditions[key] = {str(value) for value in spec["values"]}
        return conditions

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
        page = elements[start: start + page_size]
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

    # -- comments --------------------------------------------------------

    def _post_comment(
        self, work_package_id: int, request: httpx.Request
    ) -> httpx.Response:
        """Mirror the live POST /work_packages/<id>/activities behaviour.

        Verified live: the body is ``{"comment": {"raw": "..."}}``;
        ``/work_packages/<id>/comments`` does not exist.
        """
        if not any(wp.get("id") == work_package_id for wp in self.work_packages):
            return self._error(404, "The requested resource could not be found.")
        try:
            body = json.loads(request.content.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._error(400, "Malformed request body")
        comment = body.get("comment")
        content = comment.get("raw") if isinstance(comment, dict) else None
        if not isinstance(content, str) or not content:
            return self._error(422, "Comment can't be blank")

        created_at = "2026-02-10T12:00:00Z"
        activity: dict[str, JSON] = {
            "id": self._next_activity_id,
            "workPackageId": work_package_id,
            "_type": "Activity::Comment",
            "comment": {"format": "markdown", "raw": content},
            "details": [],
            "createdAt": created_at,
            "updatedAt": created_at,
            "_links": {},
        }
        if self.posting_user_id is not None:
            activity["_links"] = {
                "user": {"href": f"/api/v3/users/{self.posting_user_id}"}
            }
        self._next_activity_id += 1
        self.activities.append(activity)
        self.comments.append(
            {
                "id": activity["id"],
                "workPackageId": work_package_id,
                "authorId": self.posting_user_id,
                "createdAt": created_at,
                "content": content,
            }
        )
        return self._json(activity, status_code=201)
