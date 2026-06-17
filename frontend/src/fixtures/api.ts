import type {
  ChatOutboundMessage,
  ChatPostAccepted,
  ChatReactionEvent,
  MemoryChannelsResponse,
  MemoryFileResponse,
  MemorySearchResponse,
  MemoryTreeDir,
  OpsDashboardResponse,
  SagaActivationHistResponse,
  SagaAtomDetail,
  SagaClustersResponse,
  SagaRecentResponse,
  SagaSearchResponse,
  SagaSqlResponse,
  SagaStatsResponse,
  TurnsResponse
} from "../api";

export const turnsFixture: TurnsResponse = {
  turns: [
    {
      turn_id: "turn-20260617-001",
      ts: "2026-06-17T12:00:00Z",
      trigger: "user_message",
      kind: "user_message",
      channel_id: "web-default",
      input: "Summarize the current state.",
      output: "State summary complete.",
      duration_ms: 1842,
      events: [
        { type: "reasoning", content: "Read current memory summary.", t_ms: 120 },
        {
          type: "tool_call",
          id: "call-state",
          name: "state_read",
          args: { path: "memory/INDEX.md" },
          t_ms: 240
        },
        {
          type: "tool_result",
          id: "call-state",
          content: "Loaded memory index.",
          is_error: false,
          t_ms: 430
        }
      ],
      saga_calls: [
        {
          call_type: "query",
          args: { q: "current state" },
          result: { atoms: 2 },
          latency_ms: 31,
          t_ms: 510
        }
      ],
      injected_inputs: [{ t_ms: 900, text: "Also include recent ops." }]
    }
  ]
};

export const opsDashboardFixture: OpsDashboardResponse = {
  generated_at: "2026-06-17T12:05:00+00:00",
  window_days: 7,
  summary: {
    total_events: 12,
    events_queued: 3,
    messages_sent: 2,
    subagents_started: 1,
    subagents_completed: 1,
    shell_jobs_spawned: 1,
    shell_jobs_routed: 1,
    failures: 1,
    high_water_events: 0,
    client_pool_drains: 0,
    tool_calls: 4,
    tool_errors: 1
  },
  by_event: { event_queued: 3, tool_call: 4, send_message_sent: 2 },
  queued_by_trigger: { user_message: 2, scheduled_tick: 1 },
  queued_by_channel: { "web-default": 2, "discord-ops": 1 },
  resolution_paths: {
    saga_query_ctx_resolution: { saga_session_id: 3, missing: 1 }
  },
  shell_jobs: {
    spawned: 1,
    routed: 1,
    no_channel: 0,
    enqueue_failed: 0,
    spawn_by_channel: { "web-default": 1 }
  },
  tools: [
    {
      tool: "saga_query",
      calls: 3,
      errors: 1,
      failure_rate: 1 / 3,
      avg_duration_ms: 24
    }
  ],
  failures_by_kind: { saga_query_error: 1 },
  timeseries: [{ day: "2026-06-17", events: 12, queued: 3 }],
  recent_failures: [
    {
      t: "2026-06-17T12:01:00+00:00",
      kind: "saga_query_error",
      channel_id: "web-default",
      trigger: "user_message",
      detail: "query timed out"
    }
  ],
  backlog: [
    {
      id: "turn-timing-histogram",
      title: "Turn duration histogram",
      status: "Partial",
      blocker: "Turn duration lives in turns.jsonl."
    }
  ],
  chainlink_issues: {
    available: true,
    issues: [
      {
        id: 526,
        title: "Define React API contracts",
        status: "open",
        priority: "medium",
        parent_id: 524,
        updated_at: "2026-06-17T12:00:00Z"
      }
    ],
    error: null,
    truncated: false,
    total_count: 1
  },
  usage_history: {
    codex_plus: {
      seven_day: [{ ts: "2026-06-17T12:00:00Z", utilization: 0.42 }]
    }
  },
  token_usage_history: [
    {
      date: "2026-06-17",
      turn_count: 1,
      input_tokens: 1200,
      cache_creation_input_tokens: 200,
      cache_read_input_tokens: 800,
      output_tokens: 300,
      total_cost_usd: null
    }
  ]
};

export const sagaStatsFixture: SagaStatsResponse = {
  ready: true,
  atom_count: 42,
  tombstoned_count: 2,
  session_count: 8,
  triple_count: 12,
  schema_version: 6,
  db_size_bytes: 1048576,
  db_path: "/home/mimir/.mimir/saga.db"
};

export const sagaRecentFixture: SagaRecentResponse = {
  atoms: [
    {
      id: "atom-A",
      content_preview: "User prefers terse operator updates.",
      memory_type: "observation",
      stream: "semantic",
      source_type: "turn",
      topics: ["preferences"],
      arousal: 0.2,
      valence: 0.1,
      encoding_confidence: 0.92,
      is_pinned: false,
      created_at: "2026-06-17T11:59:00Z",
      session_id: "session-1",
      channel_id: "web-default"
    }
  ],
  total: 42,
  limit: 50,
  channel_filter: null,
  channels: ["web-default", "discord-ops"]
};

export const sagaAtomFixture: SagaAtomDetail = {
  id: "atom-A",
  content: "User prefers terse operator updates.",
  memory_type: "observation",
  stream: "semantic",
  source_type: "turn",
  topics: ["preferences"],
  metadata: { source_turn_id: "turn-20260617-001" },
  arousal: 0.2,
  valence: 0.1,
  encoding_confidence: 0.92,
  is_pinned: false,
  created_at: "2026-06-17T11:59:00Z",
  session_id: "session-1",
  channel_id: "web-default",
  access_count: 3,
  last_access_ts: "2026-06-17T12:00:00Z",
  last_access_source: "query",
  embedding: {
    provider: "local",
    model: "test-embedding",
    dim: 384,
    embedded_at: "2026-06-17T11:59:10Z"
  },
  relations_out: [
    {
      relation_type: "supports",
      target_id: "atom-B",
      confidence: 0.8,
      target_preview: "Keep dashboards dense."
    }
  ],
  tombstoned: false
};

export const sagaSearchFixture: SagaSearchResponse = {
  atoms: sagaRecentFixture.atoms,
  total_matched: 1,
  query: "terse",
  channel_filter: null,
  limit: 100
};

export const sagaActivationFixture: SagaActivationHistResponse = {
  buckets: [
    { range_start: -2.5, range_end: -2, count: 4 },
    { range_start: -2, range_end: -1.5, count: 8 }
  ],
  total: 12,
  never_accessed: 30,
  days: 7
};

export const sagaClustersFixture: SagaClustersResponse = {
  clusters: [
    {
      cluster_id: "session-1",
      size: 2,
      sample_atoms: [
        { id: "atom-A", content_preview: "User prefers terse operator updates." }
      ]
    }
  ],
  total_clusters: 1,
  total_atoms: 2
};

export const sagaSqlFixture: SagaSqlResponse = {
  columns: ["id", "memory_type"],
  rows: [["atom-A", "observation"]],
  row_count: 1,
  truncated: false
};

export const memoryTreeFixture: MemoryTreeDir = {
  name: "home",
  type: "dir",
  path: "",
  desc: null,
  children: [
    {
      name: "memory",
      type: "dir",
      path: "memory",
      desc: null,
      children: [
        {
          name: "INDEX.md",
          type: "file",
          path: "memory/INDEX.md",
          size: 256,
          modified: "2026-06-17T12:00:00+00:00",
          desc: "Memory index"
        }
      ]
    },
    {
      name: "state",
      type: "dir",
      path: "state",
      desc: null,
      children: []
    }
  ]
};

export const memoryFileFixture: MemoryFileResponse = {
  path: "memory/INDEX.md",
  content: "# Memory\n\nCurrent memory index.",
  size: 256,
  modified: "2026-06-17T12:00:00+00:00"
};

export const memorySearchFixture: MemorySearchResponse = {
  query: "memory",
  hits: [
    {
      path: "memory/INDEX.md",
      line_no: 1,
      snippet: "# Memory"
    }
  ],
  total: 1,
  truncated: false
};

export const memoryChannelsFixture: MemoryChannelsResponse = {
  channels: ["web-default", "discord-ops"]
};

export const chatAcceptedFixture: ChatPostAccepted = {
  ok: true,
  channel_id: "web-default"
};

export const chatMessageFixture: ChatOutboundMessage = {
  channel_id: "web-default",
  text: "State summary complete.",
  message_id: "msg-abc123",
  attachments: []
};

export const chatReactionFixture: ChatReactionEvent = {
  _event: "react",
  channel_id: "web-default",
  message_id: "msg-abc123",
  emoji: "thumbs_up"
};
