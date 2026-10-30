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
