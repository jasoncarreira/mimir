"""Versioned web API contracts for the React dashboard.

The aiohttp app intentionally does not take a broad OpenAPI dependency yet:
the current migration target is a small set of operator-dashboard endpoints,
and several legacy static pages still consume the pre-v1 shapes directly.

This module is the Python source of truth for the additive React v1 contract:

* backend handlers use the response helpers here for stable envelopes;
* tests validate representative payloads with the schemas below;
* ``frontend/src/api/generated/contracts.ts`` is generated from this module.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

API_VERSION = "v1"


def list_meta(
    *,
    cursor: str | None = None,
    limit: int | None = None,
    total: int | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Return the consistent list metadata envelope used by v1 list calls."""
    return {
        "cursor": cursor,
        "limit": limit,
        "total": total,
        "truncated": truncated,
    }


def success_payload(
    data: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "version": API_VERSION,
        "data": data,
    }
    if meta is not None:
        payload["meta"] = meta
    return payload


def error_payload(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"ok": False, "version": API_VERSION, "error": error}


def json_success(
    data: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> web.Response:
    return web.json_response(success_payload(data, meta=meta), headers=headers)


def json_error(
    code: str,
    message: str,
    *,
    status: int = 400,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> web.Response:
    return web.json_response(
        error_payload(code, message, details=details),
        status=status,
        headers=headers,
    )


LIVE_EVENT_KINDS = (
    "chat.message",
    "chat.reaction",
    "turn.event",
    "turn.lifecycle",
)


def make_chat_message_event(
    *,
    channel_id: str,
    text: str,
    message_id: str,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "chat.message",
        "channel_id": channel_id,
        "text": text,
        "message_id": message_id,
        "attachments": attachments or [],
    }


def make_chat_reaction_event(
    *,
    channel_id: str,
    message_id: str,
    emoji: str,
) -> dict[str, Any]:
    return {
        "kind": "chat.reaction",
        "channel_id": channel_id,
        "message_id": message_id,
        "emoji": emoji,
    }


def validate_api_envelope(payload: Any, *, expect_ok: bool | None = None) -> None:
    """Small runtime validator for representative schema tests.

    This is deliberately not a full JSON Schema implementation. The contract
    surface is kept in normal Python structures and generated TS; these
    assertions catch accidental envelope/list-metadata regressions in the
    aiohttp handlers without adding another runtime dependency.
    """
    assert isinstance(payload, dict)
    assert payload.get("version") == API_VERSION
    assert isinstance(payload.get("ok"), bool)
    if expect_ok is not None:
        assert payload["ok"] is expect_ok
    if payload["ok"]:
        assert isinstance(payload.get("data"), dict)
        if "meta" in payload:
            validate_list_meta(payload["meta"])
    else:
        error = payload.get("error")
        assert isinstance(error, dict)
        assert isinstance(error.get("code"), str) and error["code"]
        assert isinstance(error.get("message"), str) and error["message"]


def validate_list_meta(meta: Any) -> None:
    assert isinstance(meta, dict)
    assert set(meta) == {"cursor", "limit", "total", "truncated"}
    assert meta["cursor"] is None or isinstance(meta["cursor"], str)
    assert meta["limit"] is None or isinstance(meta["limit"], int)
    assert meta["total"] is None or isinstance(meta["total"], int)
    assert isinstance(meta["truncated"], bool)


def validate_live_event(event: Any) -> None:
    assert isinstance(event, dict)
    kind = event.get("kind")
    assert kind in LIVE_EVENT_KINDS
    if kind == "chat.message":
        assert isinstance(event.get("channel_id"), str)
        assert isinstance(event.get("text"), str)
        assert isinstance(event.get("message_id"), str)
        assert isinstance(event.get("attachments"), list)
    elif kind == "chat.reaction":
        assert isinstance(event.get("channel_id"), str)
        assert isinstance(event.get("message_id"), str)
        assert isinstance(event.get("emoji"), str)
    elif kind == "turn.event":
        assert isinstance(event.get("turn_id"), str)
        assert isinstance(event.get("event"), dict)
    elif kind == "turn.lifecycle":
        assert isinstance(event.get("turn_id"), str)
        assert event.get("phase") in {"started", "finished", "failed"}


TYPESCRIPT_CONTRACTS = """// Auto-generated from mimir.web_contracts. Do not edit by hand.

export type ApiVersion = "v1";

export interface ApiErrorEnvelope {
  ok: false;
  version: ApiVersion;
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export interface ListMeta {
  cursor: string | null;
  limit: number | null;
  total: number | null;
  truncated: boolean;
}

export interface ApiSuccessEnvelope<TData, TMeta = undefined> {
  ok: true;
  version: ApiVersion;
  data: TData;
  meta?: TMeta;
}

export type ApiEnvelope<TData, TMeta = undefined> =
  | ApiSuccessEnvelope<TData, TMeta>
  | ApiErrorEnvelope;

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type JsonObject = Record<string, JsonValue>;

export type TurnTrigger =
  | "user_message"
  | "scheduled_tick"
  | "saga_session_end"
  | "poller"
  | "claude_code_spawn"
  | "shell_job_complete"
  | "react_received"
  | string;

export interface TurnEventBase {
  type: string;
  t_ms?: number | null;
  [key: string]: unknown;
}

export interface SagaCall {
  call_type?: string;
  args?: unknown;
  result?: unknown;
  error?: string | null;
  latency_ms?: number | null;
  t_ms?: number | null;
}

export interface InjectedInput {
  t_ms?: number | null;
  text: string;
}

export interface TurnRecord {
  turn_id?: string;
  ts?: string;
  trigger?: TurnTrigger;
  kind?: string | null;
  channel_id?: string | null;
  input?: string;
  output?: string;
  error?: string | null;
  duration_ms?: number | null;
  events?: TurnEventBase[];
  saga_calls?: SagaCall[];
  injected_inputs?: Array<InjectedInput | string>;
  usage?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface TurnsData {
  turns: TurnRecord[];
}

export interface EventsData {
  events: JsonObject[];
}

export interface OpsDashboardData {
  generated_at: string;
  window_days: number;
  summary: Record<string, number>;
  by_event: Record<string, number>;
  queued_by_trigger: Record<string, number>;
  queued_by_channel: Record<string, number>;
  resolution_paths: Record<string, Record<string, number>>;
  shell_jobs: {
    spawned: number;
    routed: number;
    no_channel: number;
    enqueue_failed: number;
    spawn_by_channel: Record<string, number>;
  };
  tools: Array<{
    tool: string;
    calls: number;
    errors: number;
    failure_rate: number;
    avg_duration_ms: number;
  }>;
  failures_by_kind: Record<string, number>;
  timeseries: Array<{ day: string; events: number; queued: number }>;
  recent_failures: Array<{
    t: string;
    kind: string;
    channel_id?: string | null;
    trigger?: string | null;
    detail: string;
  }>;
  backlog: Array<{ id: string; title: string; status: string; blocker: string }>;
  chainlink_issues: {
    available: boolean;
    issues: JsonObject[];
    error?: string | null;
    truncated?: boolean;
    total_count?: number;
  };
  usage_history: Record<string, Record<string, unknown[]>>;
  token_usage_history: unknown[];
}

export interface ChainlinkBoardIssue {
  id: number;
  title: string;
  status: "open" | "ready" | "blocked" | "in-progress" | "review" | "done" | string;
  raw_status: string;
  priority: string;
  labels: string[];
  parent_id: number | null;
  child_ids: number[];
  child_progress: { done: number; total: number };
  blocked_by: number[];
  blocking: number[];
  updated_at: string;
  created_at: string;
  description: string;
  comments: Array<{ id: string; author: string; created_at: string; body: string }>;
  worklink?: {
    issue: number;
    attempt: number;
    backend: string;
    status: string;
    branch: string;
    started_at: string;
    finished_at: string;
    diff_stat: string;
    tests: Record<string, unknown> | null;
    pr_url: string;
    blocked_reason: string;
    transcript: string;
    transcript_href: string;
    evidence_path: string;
    evidence_href: string;
  } | null;
}

export interface ChainlinkBoardData {
  available: boolean;
  error?: string | null;
  generated_at: string;
  columns: Array<{ id: string; title: string; issue_ids: number[] }>;
  issues: ChainlinkBoardIssue[];
  roots: number[];
  edges: Array<{ from: number; to: number; kind: "blocks" | "parent" | string }>;
  filters: {
    labels: string[];
    statuses: string[];
    priorities: string[];
  };
  truncated: boolean;
  total_count: number;
export interface AdminConfigFieldSchema {
  name: string;
  type: string;
  mutable: boolean;
}

export interface AdminConfigSchemaSection {
  id: string;
  label: string;
  mutable: boolean;
  fields: AdminConfigFieldSchema[];
}

export interface AdminConfigEnvItem {
  name: string;
  category: string;
  present: boolean;
  secret: boolean;
  value: string | null;
  mutable: boolean;
}

export interface AdminConfigScheduleItem {
  name: string;
  kind: string;
  cron?: string | null;
  time_of_day?: string | null;
  channel_id?: string | null;
  deliver?: string | null;
  priority?: string | null;
  mutable: boolean;
}

export interface AdminConfigPollerItem {
  name: string;
  cron: string;
  priority: string;
  batch_size?: number;
  recover_failed_turns?: boolean;
  mutable: boolean;
  [key: string]: unknown;
}

export interface AdminConfigData {
  generated_at: string;
  model: {
    model_spec: string;
    provider_prefix: string;
    model: string;
    provider: string;
    anthropic_base_url_present: boolean;
    context_window: string;
    context_1m_enabled: boolean;
    resource_window: {
      billing_mode: string;
      usage_block_enabled: boolean;
      capture_rate_limits: boolean;
      max_output_tokens: number | null;
    };
  };
  schema_sections: AdminConfigSchemaSection[];
  schedules: AdminConfigScheduleItem[];
  pollers: AdminConfigPollerItem[];
  env: AdminConfigEnvItem[];
  raw_config: JsonObject;
  mutation_policy: {
    mode: "read_only_v1";
    mutable_fields: string[];
    reveal_secret_values: false;
    reveal_path: string | null;
    edit_path: string | null;
    rate_limited: boolean;
  };
}

export interface MemoryTreeDir {
  name: string;
  type: "dir";
  path: string;
  desc: string | null;
  children: MemoryTreeNode[];
  error?: string;
}

export interface MemoryTreeFile {
  name: string;
  type: "file";
  path: string;
  size: number;
  modified: string;
  desc: string | null;
}

export type MemoryTreeNode = MemoryTreeDir | MemoryTreeFile;

export interface MemoryFileData {
  path: string;
  content: string;
  size: number;
  modified: string;
}

export interface MemorySearchHit {
  path: string;
  line_no: number;
  snippet: string;
}

export interface MemorySearchData {
  query: string;
  hits: MemorySearchHit[];
}

export interface MemoryChannelsData {
  channels: string[];
}

export interface SagaAtomSummary {
  id: string;
  content_preview: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  topics?: string[];
  arousal?: number | null;
  valence?: number | null;
  encoding_confidence?: number | null;
  is_pinned?: boolean | number;
  created_at?: string | null;
  session_id?: string | null;
  channel_id?: string | null;
}

export interface SagaRecentData {
  atoms: SagaAtomSummary[];
  channel_filter?: string | null;
  channels: string[];
}

export interface SagaStatsData {
  ready: boolean;
  atom_count?: number;
  tombstoned_count?: number;
  session_count?: number;
  triple_count?: number;
  schema_version?: number | null;
  db_size_bytes?: number;
  db_path?: string;
}

export interface SagaAtomDetailData {
  id: string;
  content?: string;
  metadata?: Record<string, unknown>;
  topics?: string[];
  relations_out?: unknown[];
  embedding?: unknown;
  [key: string]: unknown;
}

export interface SagaSearchData {
  atoms: SagaAtomSummary[];
  query: string;
  channel_filter?: string | null;
}

export interface SagaActivationHistData {
  buckets: Array<{ range_start: number; range_end: number; count: number }>;
  never_accessed?: number;
  days?: number;
}

export interface SagaClustersData {
  clusters: Array<{
    cluster_id: string | null;
    size: number;
    sample_atoms: Array<{ id: string; content_preview: string }>;
  }>;
}

export type SagaSqlCell = string | number | boolean | null;

export interface SagaSqlData {
  columns?: string[];
  rows?: SagaSqlCell[][];
  row_count?: number;
  rejected?: boolean;
}

export interface DashboardExtensionManifest {
  id: string;
  route_path: string;
  label: string;
  icon: string | null;
  nav_position: number;
  enabled: boolean;
  bundle: string | null;
  css: string[];
  api_namespace: string | null;
  trusted_first_party: true;
}

export interface WebBootstrapData {
  auth: {
    required: boolean;
    scheme: "x-api-key";
    storage: "browser-localStorage";
  };
  server: {
    web_host: string;
    public_bind: boolean;
    unauthenticated_allowed: boolean;
  };
  stream_auth: {
    shape: "fetch-event-stream";
    header: "X-API-Key";
    native_eventsource_supported_when_auth_required: false;
  };
  dashboard_extensions: DashboardExtensionManifest[];
}

export interface ChatPostRequest {
  channel_id?: string;
  content: string;
  author?: string;
  author_id?: string;
  msg_id?: string;
  extra?: Record<string, unknown>;
}

export interface ChatAcceptedData {
  channel_id: string;
  source_id: string;
}

export interface ChatMessageEvent {
  kind: "chat.message";
  channel_id: string;
  text: string;
  message_id: string;
  attachments: string[];
}

export interface ChatReactionEvent {
  kind: "chat.reaction";
  channel_id: string;
  message_id: string;
  emoji: string;
}

export interface TurnEventLiveEvent {
  kind: "turn.event";
  turn_id: string;
  event: TurnEventBase;
}

export interface TurnLifecycleEvent {
  kind: "turn.lifecycle";
  turn_id: string;
  phase: "started" | "finished" | "failed";
  ts?: string;
  error?: string | null;
}

export type LiveEvent =
  | ChatMessageEvent
  | ChatReactionEvent
  | TurnEventLiveEvent
  | TurnLifecycleEvent;

export interface LiveEventStreamItem {
  id: string;
  cursor: string;
  ts?: string | null;
  event: LiveEvent;
}
"""


def render_typescript_contracts() -> str:
    return TYPESCRIPT_CONTRACTS


def main() -> None:
    print(render_typescript_contracts(), end="")


if __name__ == "__main__":
    main()
