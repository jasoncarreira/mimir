// Auto-generated from mimir.web_contracts. Do not edit by hand.

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
