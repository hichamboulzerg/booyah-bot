"""Small Anthropic Messages API helpers used by the bot."""

from __future__ import annotations

import copy
import json

import requests


CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _structured_schema(schema: dict) -> dict:
    """Convert the existing renderer schema to Claude structured-output syntax."""
    result = copy.deepcopy(schema)

    def visit(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            limits = []
            if "maxLength" in node:
                limits.append(f"Use at most {node['maxLength']} characters.")
            if "minLength" in node:
                limits.append(f"Use at least {node['minLength']} characters.")
            if "minItems" in node and "maxItems" in node:
                if node["minItems"] == node["maxItems"]:
                    limits.append(f"Return exactly {node['minItems']} items.")
                else:
                    limits.append(
                        f"Return {node['minItems']} to {node['maxItems']} items."
                    )
            if limits:
                description = str(node.get("description", "")).strip()
                node["description"] = " ".join(
                    ([description] if description else []) + limits
                )
            # These validation constraints are enforced by the existing parser and
            # prompt. Removing them keeps the schema compatible across Claude models.
            for key in ("minLength", "maxLength", "minItems", "maxItems"):
                node.pop(key, None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def _message_text(payload: dict) -> str:
    blocks = payload.get("content") or []
    text = "".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned an empty response")
    return text


def claude_text(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int = 1400,
) -> str:
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    response = requests.post(
        CLAUDE_MESSAGES_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=(20, 600),
    )
    if not response.ok:
        raise RuntimeError(
            f"Claude API error {response.status_code}: {response.text[:500]}"
        )
    return _message_text(response.json())


def claude_json(
    api_key: str,
    model: str,
    prompt: str,
    schema: dict,
    max_tokens: int = 3000,
) -> dict:
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    response = requests.post(
        CLAUDE_MESSAGES_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": _structured_schema(schema),
                }
            },
        },
        timeout=(20, 600),
    )
    if not response.ok:
        raise RuntimeError(
            f"Claude API error {response.status_code}: {response.text[:500]}"
        )
    try:
        return json.loads(_message_text(response.json()))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude returned invalid structured JSON") from exc
