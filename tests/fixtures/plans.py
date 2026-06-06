"""Canned Plan samples for use across tests."""

from __future__ import annotations

from ai.discovery.planner import Plan


def single_step_plan() -> Plan:
    return Plan(
        target_domain="https://api.example.com",
        target_endpoints=["/posts"],
        action="create_post",
        parameters={"title": "Test", "body": "Body"},
    )


def multi_step_plan() -> Plan:
    return Plan(
        target_domain="https://api.example.com",
        target_endpoints=["/users/1", "/users/1/posts"],
        action="fetch_user",
        parameters={"id": 1},
        steps=[
            {"action": "fetch_user", "endpoint": "/users/1", "parameters": {"id": 1}},
            {
                "action": "fetch_user_posts",
                "endpoint": "/users/1/posts",
                "parameters": {"id": 1},
            },
        ],
    )


def failing_plan() -> Plan:
    return Plan(
        target_domain="https://api.example.com",
        target_endpoints=["/posts"],
        action="create_post",
        parameters={"title": "WillFail"},
        steps=[
            {"action": "create_post", "endpoint": "/posts", "parameters": {"title": "WillFail"}},
        ],
    )
