"""Tool surface smoke test.

An MCP client sees only three things about a tool before deciding whether to run
it: the name, the description, and the annotations. So all three are checked for
every registered tool, and every tool is then invoked with NO credentials
configured — the state a new user is in — to prove it answers with something
actionable instead of raising a traceback into the model's context.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server import MCPServer

from storepilot import server as server_module
from storepilot.app_store.tools import register as register_app_store
from storepilot.cross.tools import register as register_cross
from storepilot.google_play.tools_read import register as register_play_read
from storepilot.google_play.tools_write import register as register_play_write

#: Every tool StorePilot exposes, by area. Registration is gated on credentials
#: at runtime, so the count is asserted against the union rather than a live server.
PLAY_READ_TOOLS = {
    "play_list_apps",
    "play_get_vitals",
    "play_get_anomalies",
    "play_get_stats",
    "play_get_earnings",
    "play_list_reviews",
    "play_portfolio_health",
}
PLAY_WRITE_TOOLS = {
    "play_upload_bundle",
    "play_create_release",
    "play_promote_release",
    "play_expand_rollout",
    "play_halt_rollout",
    "play_update_listing",
    "play_reply_review",
}
APP_STORE_TOOLS = {
    "asc_list_apps",
    "asc_list_versions",
    "asc_list_builds",
    "asc_list_reviews",
    "asc_get_sales",
    "asc_get_analytics",
    "asc_upload_build",
    "asc_reply_review",
    "asc_update_metadata",
    "asc_submit_for_review",
}
CROSS_TOOLS = {
    "portfolio_overview",
    "compare_reviews",
    "parity_check",
    "release_both",
    "metadata_pull",
    "metadata_push",
    "list_app_pairs",
    "suggest_app_pairs",
    "pair_apps",
}
ALL_TOOLS = PLAY_READ_TOOLS | PLAY_WRITE_TOOLS | APP_STORE_TOOLS | CROSS_TOOLS | {"setup_doctor"}

#: Tools that publish text a user or a store visitor will see. Every one of these
#: must be flagged destructive so a client stops and asks a human.
PUBLISHING_TOOLS = {
    "play_upload_bundle",
    "play_create_release",
    "play_promote_release",
    "play_expand_rollout",
    "play_halt_rollout",
    "play_update_listing",
    "play_reply_review",
    "asc_reply_review",
    "asc_update_metadata",
    "asc_submit_for_review",
    "metadata_push",
    "release_both",
}

#: Writes that only ever touch the local filesystem.
LOCAL_WRITE_TOOLS = {"metadata_pull", "pair_apps"}

READ_ONLY_TOOLS = ALL_TOOLS - PUBLISHING_TOOLS - LOCAL_WRITE_TOOLS

#: Placeholder arguments for the credential-less invocation sweep. Anything not
#: listed is filled in from the schema type.
ARG_OVERRIDES: dict[str, Any] = {
    "package_name": "com.example.app",
    "app": "example",
    "aab_path": "/nonexistent/app-release.aab",
    "path": "/nonexistent/app.ipa",
    "month": "2026-07",
    "period": "2026-07-01",
    "locale": "en-US",
    "text": "Thanks for the feedback!",
    "user_fraction": 0.1,
    "version_name": "1.0.0",
    "from_track": "beta",
    "to_track": "internal",
    "track": "internal",
}


def build_server() -> MCPServer:
    """Exactly what ``server._register_adapters`` does when both stores are set up."""
    mcp = MCPServer("storepilot-test")
    register_play_read(mcp)
    register_play_write(mcp)
    register_app_store(mcp)
    register_cross(mcp)
    mcp.tool(annotations=server_module.READ_ONLY)(server_module.setup_doctor)
    return mcp


@pytest.fixture(scope="module")
def tools() -> list[Any]:
    import asyncio

    return asyncio.run(build_server().list_tools())


def schema_of(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {})
    return schema if isinstance(schema, dict) else {}


def annotation(tool: Any, snake: str, camel: str) -> Any:
    ann = tool.annotations
    return getattr(ann, snake, None) if hasattr(ann, snake) else getattr(ann, camel, None)


def text_of(result: Any) -> str:
    blocks = getattr(result, "content", result)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    if isinstance(blocks, (list, tuple)):
        return "\n".join(getattr(block, "text", str(block)) for block in blocks)
    return str(blocks)


# --- Registration ------------------------------------------------------------


def test_every_tool_registers(tools: list[Any]) -> None:
    names = {tool.name for tool in tools}
    assert names == ALL_TOOLS
    assert len(tools) == 34


def test_adapters_only_register_when_their_store_is_configured() -> None:
    """A server with no credentials must still start, exposing only setup_doctor."""
    from storepilot.config import settings

    assert settings.google_play_enabled is False
    assert settings.app_store_enabled is False

    mcp = MCPServer("gated")
    original = server_module.mcp
    server_module.mcp = mcp
    try:
        server_module._register_adapters()
    finally:
        server_module.mcp = original

    import asyncio

    assert asyncio.run(mcp.list_tools()) == []


def test_every_tool_has_a_description_an_llm_can_choose_from(tools: list[Any]) -> None:
    for tool in tools:
        description = (tool.description or "").strip()
        assert description, f"{tool.name} has no description"
        assert len(description) >= 120, (
            f"{tool.name}: {len(description)} characters is too terse for a model to "
            f"pick this tool correctly"
        )
        assert description[0].isupper(), f"{tool.name}: description does not start with a sentence"


def test_every_destructive_tool_documents_every_parameter(tools: list[Any]) -> None:
    """Descriptions must live in the schema, not in an 'Args:' docstring section.

    The SDK does not parse 'Args:' — a tool can read as fully documented in source
    and still hand the model a bare parameter name. These are the tools where a
    wrong value publishes something or reaches real users, so the schema itself has
    to carry the meaning of every argument.
    """
    for tool in tools:
        if annotation(tool, "destructive_hint", "destructiveHint") is not True:
            continue
        properties = schema_of(tool).get("properties") or {}
        undocumented = [
            name for name, spec in properties.items() if not (spec.get("description") or "")
        ]
        assert not undocumented, (
            f"{tool.name} exposes {undocumented} to the model with no description"
        )


# --- Annotation coherence ----------------------------------------------------


def test_read_only_tools_are_not_marked_destructive(tools: list[Any]) -> None:
    for tool in tools:
        if annotation(tool, "read_only_hint", "readOnlyHint") is not True:
            continue
        assert annotation(tool, "destructive_hint", "destructiveHint") is False, (
            f"{tool.name} claims to be read-only and destructive at once"
        )
        assert tool.name in READ_ONLY_TOOLS, f"{tool.name} is marked read-only unexpectedly"


def test_every_publishing_tool_is_marked_destructive(tools: list[Any]) -> None:
    """These are the calls a client must stop and ask a human about."""
    by_name = {tool.name: tool for tool in tools}
    for name in PUBLISHING_TOOLS:
        tool = by_name[name]
        assert annotation(tool, "read_only_hint", "readOnlyHint") is False, name
        assert annotation(tool, "destructive_hint", "destructiveHint") is True, name


def test_local_writes_are_neither_read_only_nor_destructive(tools: list[Any]) -> None:
    """They write to disk, so not read-only; nothing reaches a store, so not destructive."""
    by_name = {tool.name: tool for tool in tools}
    for name in LOCAL_WRITE_TOOLS:
        tool = by_name[name]
        assert annotation(tool, "read_only_hint", "readOnlyHint") is False, name
        assert annotation(tool, "destructive_hint", "destructiveHint") is False, name
        assert annotation(tool, "open_world_hint", "openWorldHint") is False, name


def test_no_tool_reaches_a_store_without_being_destructive(tools: list[Any]) -> None:
    """The general rule behind the explicit lists above."""
    for tool in tools:
        if annotation(tool, "open_world_hint", "openWorldHint") is not True:
            continue
        if annotation(tool, "read_only_hint", "readOnlyHint") is True:
            continue
        assert annotation(tool, "destructive_hint", "destructiveHint") is True, tool.name


def test_two_step_write_tools_expose_the_confirmation_pair(tools: list[Any]) -> None:
    for tool in tools:
        # play_halt_rollout is the one deliberate exception: stopping a bad
        # rollout makes things safer, and demanding a second round-trip during an
        # incident is itself the failure mode. It is audited instead of gated.
        if tool.name not in PUBLISHING_TOOLS or tool.name == "play_halt_rollout":
            continue
        properties = schema_of(tool).get("properties") or {}
        assert "confirm" in properties, f"{tool.name} is destructive but has no confirm argument"
        assert "confirmation_token" in properties, f"{tool.name} has no confirmation_token"


def test_the_only_unguarded_write_is_the_one_that_makes_things_safer(
    tools: list[Any],
) -> None:
    unguarded = [
        tool.name
        for tool in tools
        if tool.name in PUBLISHING_TOOLS and "confirm" not in (schema_of(tool).get("properties") or {})
    ]
    assert unguarded == ["play_halt_rollout"]


def test_no_read_only_tool_accepts_a_confirm_argument(tools: list[Any]) -> None:
    for tool in tools:
        if annotation(tool, "read_only_hint", "readOnlyHint") is not True:
            continue
        assert "confirm" not in (schema_of(tool).get("properties") or {}), tool.name


# --- Behaviour with no credentials -------------------------------------------


def build_args(tool: Any) -> dict[str, Any]:
    schema = schema_of(tool)
    properties = schema.get("properties") or {}
    args: dict[str, Any] = {}
    for name in schema.get("required") or []:
        spec = properties.get(name) or {}
        if name in ARG_OVERRIDES:
            args[name] = ARG_OVERRIDES[name]
            continue
        kind = spec.get("type")
        if kind == "integer":
            args[name] = 1
        elif kind == "number":
            args[name] = 1.0
        elif kind == "boolean":
            args[name] = False
        elif kind == "array":
            args[name] = ["4501"]
        else:
            args[name] = name
    return args


@pytest.mark.parametrize("tool_name", sorted(ALL_TOOLS))
async def test_a_tool_with_no_credentials_answers_instead_of_raising(tool_name: str) -> None:
    mcp = build_server()
    tool = next(t for t in await mcp.list_tools() if t.name == tool_name)

    result = await mcp.call_tool(tool_name, build_args(tool))
    output = text_of(result)

    assert output.strip(), f"{tool_name} returned nothing at all"
    assert "Traceback" not in output, f"{tool_name} leaked a traceback:\n{output}"
    assert "Traceback" not in output


@pytest.mark.parametrize(
    "tool_name",
    sorted(ALL_TOOLS - {"list_app_pairs", "suggest_app_pairs", "pair_apps", "asc_upload_build"}),
)
async def test_a_credential_failure_always_carries_a_remedy(tool_name: str) -> None:
    """"Credentials missing" with no next step is the #1 way a user gets stuck."""
    mcp = build_server()
    tool = next(t for t in await mcp.list_tools() if t.name == tool_name)
    output = text_of(await mcp.call_tool(tool_name, build_args(tool)))

    lowered = output.lower()
    assert any(
        clue in output or clue.lower() in lowered
        for clue in (
            "Fix:",
            "STOREPILOT_",
            "setup_doctor",
            "not configured",
            "remedy",
            # A preview needs no credentials, and returning one is a valid answer:
            # nothing has been changed and the user is told exactly what would be.
            "CONFIRMATION REQUIRED",
        )
    ), f"{tool_name} failed without telling the user what to do:\n{output[:800]}"


async def test_a_write_tool_refuses_to_execute_without_a_preview() -> None:
    """confirm=True with no token is refused, and the refusal explains the two-step flow."""
    mcp = build_server()
    output = text_of(
        await mcp.call_tool(
            "play_update_listing",
            {
                "package_name": "com.example.app",
                "locale": "en-US",
                "title": "A new title",
                "confirm": True,
            },
        )
    )
    assert "no confirmation_token" in output
    assert "confirm=False first" in output
    assert "Do not invent a token" in output


async def test_a_write_tool_previews_without_needing_credentials() -> None:
    """The preview is the safety mechanism, so it must not be gated behind setup."""
    mcp = build_server()
    output = text_of(
        await mcp.call_tool(
            "play_reply_review",
            {
                "package_name": "com.example.app",
                "review_id": "gp:AOqpTOEx",
                "text": "Thanks — fixed in 4.2.1.",
            },
        )
    )
    assert "CONFIRMATION REQUIRED" in output
    assert "nothing has been changed yet" in output
    assert "PUBLIC" in output, "the user must be told the reply is public before approving"


async def test_tools_that_work_offline_actually_answer() -> None:
    """The registry tools need no store at all, so they must answer, not error."""
    mcp = build_server()
    output = text_of(await mcp.call_tool("list_app_pairs", {}))
    assert "App registry:" in output
    assert "No apps are paired yet." in output
