"""Regression checks for the batched dashboard defects in Chainlink #1240."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_saga_detail_values_are_escaped_once() -> None:
    source = _source("mimir/saga_dashboard.html")

    assert 'const kv = (k, v) =>' in source
    assert '+ esc(v !== null && v !== undefined ? String(v) : "—")' in source
    assert 'fmtTs(d.last_access_ts) + " (" + (d.last_access_source || "") + ")"' in source
    assert '"yes (" + (d.tombstoned_reason || "?") + ")"' in source
    assert 'esc(d.last_access_source)' not in source
    assert 'esc(d.tombstoned_reason' not in source


def test_saga_relation_preserves_zero_confidence() -> None:
    source = _source("mimir/saga_dashboard.html")

    assert "(r.confidence ?? 1).toFixed(2)" in source
    assert "(r.confidence || 1).toFixed(2)" not in source


def test_turn_duration_rounding_carries_into_minutes() -> None:
    source = _source("mimir/turn_viewer.html")

    assert "var seconds = Math.round(ms / 1000);" in source
    assert "Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's'" in source


def test_file_search_surfaces_error_payload_before_results() -> None:
    source = _source("mimir/file_memory_dashboard.html")
    error_check = "if (data.error) {"
    result_header = 'header.className = "search-results-header";'
    search_start = source.index("async function loadSearch(q)")
    search_end = source.index("async function loadFile", search_start)
    load_search = source[search_start:search_end]

    assert error_check in load_search
    assert "Error: ' + esc(data.error)" in load_search
    assert load_search.index(error_check) < load_search.index(result_header)


def test_channel_selection_expands_first_segment_and_target() -> None:
    source = _source("mimir/file_memory_dashboard.html")

    assert "for (let i = 1; i <= parts.length; i++)" in source


def test_wiki_bare_fence_has_explicit_open_state() -> None:
    source = _source("frontend/src/routes/WikiRoute.tsx")

    assert "let inCodeFence = false;" in source
    assert "if (inCodeFence) {" in source
    assert "inCodeFence = true;" in source
    assert "if (codeLanguage || codeLines.length)" not in source


def test_ops_routes_do_not_offer_unauthenticated_json_links() -> None:
    source = _source("frontend/src/routes/OpsRoute.tsx")
    test_source = _source("frontend/src/routes/OpsRoute.test.tsx")

    assert "ops-json-link" not in source
    assert 'href={`/api/v1/ops?days=${validDays}`}' not in source
    assert 'queryByRole("link", { name: "JSON" })' in test_source


def test_trigger_pill_rules_match_real_trigger_values() -> None:
    source = _source("frontend/src/styles.css")

    assert '.turn-trigger[data-trigger="claude_code_spawn"]' in source
    assert '.turn-trigger[data-trigger="shell_job_complete"]' in source
    assert '.turn-trigger[data-trigger="spawn"]' not in source
    assert '.turn-trigger[data-trigger="job"]' not in source


def test_surface_route_calls_hooks_before_early_returns() -> None:
    source = _source("frontend/src/main.tsx")
    start = source.index("function SurfaceRoute")
    end = source.index("function AppFrame", start)
    surface_route = source[start:end]
    first_return = surface_route.index('if (surface.id === "state-memory")')

    assert surface_route.index("useRouteState(surface)") < first_return
    assert surface_route.index("useUiState(") < first_return


def test_chat_store_bounds_retained_messages() -> None:
    source = _source("frontend/src/chatStore.ts")
    test_source = _source("frontend/src/ChatRoute.test.tsx")

    assert "const MAX_CHAT_MESSAGES = 100;" in source
    assert "updater(state.messages).slice(-MAX_CHAT_MESSAGES)" in source
    assert 'it("retains only the newest 100 messages"' in test_source


def test_turn_time_escapes_unparseable_input() -> None:
    source = _source("mimir/turn_viewer.html")

    assert "if (isNaN(d.getTime())) return esc(ts);" in source
    assert "if (isNaN(d.getTime())) return ts;" not in source
