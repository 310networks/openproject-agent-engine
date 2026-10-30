from op_agent_engine.openproject.workpackage import parse_work_package

OPTIONS = {1: "FRONTEND", 2: "BACKEND", 3: "LEAD_DESIGNER", 4: "REVIEWER"}


def entry(**links: object) -> dict[str, object]:
    return {
        "id": 77,
        "displayId": "77",
        "subject": "Gather data points",
        "_links": {
            "project": {"href": "/api/v3/projects/4", "title": "Example Project"},
            "type": {"href": "/api/v3/types/1", "title": "Task"},
            "status": {"href": "/api/v3/statuses/1", "title": "New"},
            "priority": {"href": "/api/v3/priorities/8", "title": "Normal"},
            **links,
        },
    }


def test_agent_value_from_top_level_attribute() -> None:
    data = entry()
    data["customField1"] = [
        {"_links": {"self": {"href": "/api/v3/custom_options/2"}}, "title": "x"}
    ]

    wp = parse_work_package(data, "customField1", OPTIONS)

    assert wp.agent_value == "BACKEND"


def test_agent_value_from_links_attribute() -> None:
    data = entry(
        customField1={
            "href": "/api/v3/custom_options/2",
            "title": "BACKEND",
        }
    )

    wp = parse_work_package(data, "customField1", OPTIONS)

    assert wp.agent_value == "BACKEND"


def test_missing_agent_value() -> None:
    wp = parse_work_package(entry(), "customField1", OPTIONS)

    assert wp.agent_value is None


def test_description_uses_raw_markdown() -> None:
    data = entry()
    data["description"] = {
        "format": "markdown",
        "raw": "## Acceptance\n\n- one thing\n- another",
        "html": "<h2>Acceptance</h2>",
    }

    wp = parse_work_package(data, "customField1", OPTIONS)

    assert wp.description == "## Acceptance\n\n- one thing\n- another"


def test_description_accepts_a_plain_string() -> None:
    data = entry()
    data["description"] = "plain text"

    assert parse_work_package(data, "customField1", OPTIONS).description == "plain text"


def test_description_falls_back_to_html_then_empty() -> None:
    data = entry()
    data["description"] = {"format": "markdown", "html": "<p>hi</p>"}
    assert parse_work_package(data, "customField1", OPTIONS).description == "<p>hi</p>"

    data["description"] = {"format": "markdown"}
    assert parse_work_package(data, "customField1", OPTIONS).description == ""
    del data["description"]
    assert parse_work_package(data, "customField1", OPTIONS).description == ""
