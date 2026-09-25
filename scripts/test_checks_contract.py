#!/usr/bin/env python3
"""Shared canonical contract for project-local TEST_CHECKS.json."""

RUN_CHECKS_COMMAND = ".opencode-v2/bin/run-checks"

TEST_CHECKS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "V2 TEST_CHECKS.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["checks"],
    "properties": {
        "checks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "command"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "command": {"type": "string", "minLength": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
            },
        },
        "required_files": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}
