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

export interface SessionMessage {
  ts: string;
  kind: string;
  author?: string | null;
  content: string;
  content_snippet: string;
  msg_id?: string | null;
}

export interface SessionTurnSummary {
  turn_id: string;
  ts: string;
  trigger: string;
  channel_id: string;
  input_snippet: string;
  output_snippet: string;
}

export interface SessionSagaAtom {
  id: string;
  content_preview: string;
  memory_type?: string | null;
  stream?: string | null;
  source_type?: string | null;
  topics?: unknown[];
  created_at?: string | null;
}

export interface ConversationSession {
  id: string;
  saga_session_id?: string | null;
  channel_id?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  last_activity_at?: string | null;
  reflected_at?: string | null;
  turn_ids: string[];
  turns: SessionTurnSummary[];
  messages: SessionMessage[];
  triggers: string[];
  summary: string;
  unfinished: unknown[];
  topics_discussed?: unknown[];
  decisions_made?: unknown[];
  closed_since?: unknown[];
  related_saga_atoms: SessionSagaAtom[];
  synthetic: boolean;
  message_count: number;
  turn_count: number;
}

export interface SessionsData {
  sessions: ConversationSession[];
  channels: string[];
  triggers: string[];
}

export interface EventsData {
  events: JsonObject[];
}

export interface OpsUsagePoint {
  ts: string;
  utilization?: number | null;
  resets_at?: number | null;
  projection?: number | null;
  pressure?: string;
  [key: string]: unknown;
}

export interface OpsTokenUsagePoint {
  date: string;
  turn_count?: number;
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  output_tokens?: number;
  total_cost_usd?: number | null;
  [key: string]: unknown;
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
  usage_history: Record<string, Record<string, OpsUsagePoint[]>>;
  token_usage_history: OpsTokenUsagePoint[];
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
}

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

export interface SchedulerRunSurface {
  id: string;
  name: string;
  kind: "schedule" | "poller";
  cron?: string | null;
  time_of_day?: string | null;
  next_run_at?: string | null;
  last_run_at?: string | null;
  channel?: string | null;
  deliver?: string | null;
  priority: string;
  prompt_source: string;
  recent_result?: string | null;
  recent_error?: string | null;
  suppression_reason?: string | null;
  suppression_severity?: string | null;
  manifest_path?: string | null;
  pass_env?: string[];
  env_required?: string[];
  config?: JsonObject;
}

export interface CommitmentSurface {
  id: string;
  text: string;
  status: string;
  kind: string;
  sensitivity: string;
  channel?: string | null;
  recipient_identity?: string | null;
  due_window_start?: string | null;
  due_window_end?: string | null;
  due_window_hint?: string | null;
  due_bucket: string;
  attempts: number;
  snooze_count: number;
  snoozed_until?: string | null;
  suggested_reminder?: string;
  source_turn_id?: string | null;
}

export interface SchedulerDashboardData {
  generated_at: string;
  available: boolean;
  due_window: string;
  schedules: SchedulerRunSurface[];
  pollers: SchedulerRunSurface[];
  commitments: CommitmentSurface[];
  actions: {
    mutations_enabled: boolean;
    policy: string;
    deferred: string[];
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
  requires_role?: string | null;
}

export interface WhoamiData {
  canonical: string | null;
  display_name: string | null;
  roles: string[];
  is_admin: boolean;
  is_master: boolean;
}

export interface AdminUser {
  canonical: string;
  display_name: string | null;
  roles: string[];
  is_admin: boolean;
  has_web_key: boolean;
}

export interface AdminUsersData {
  users: AdminUser[];
}

export interface IssueKeyData {
  canonical: string;
  key: string;
}

export interface RevokeKeyData {
  canonical: string;
  revoked: boolean;
}

export interface WebBootstrapData {
  /** mimir build/release version, for the app shell's version label. */
  version: string;
  /** The model the agent is running on (e.g. "gpt-5.5"), for the dossier. */
  model: string;
  /** Running turn total (latest turn record's seq), for the dossier. */
  turns_total: number;
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
  /** Agent-owned UI config (<home>/state/web_ui.json), editable by the agent. */
  ui: {
    agent_name: string;
    skin: string;
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
  /** The turn's channel + trigger, so consumers can scope (e.g. chat-only). */
  channel_id?: string | null;
  trigger?: string | null;
  event: TurnEventBase;
}

export interface TurnLifecycleEvent {
  kind: "turn.lifecycle";
  turn_id: string;
  phase: "started" | "finished" | "failed";
  ts?: string;
  error?: string | null;
  /** Monotonic turn seq — consumers show the running total as max(seq). */
  seq?: number | null;
  /** The turn's channel + trigger, so consumers can scope (e.g. chat-only). */
  channel_id?: string | null;
  trigger?: string | null;
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
