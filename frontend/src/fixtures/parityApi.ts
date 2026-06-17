import type {
  ChatMessageEvent,
  ChatReactEvent,
  ChatSendResponse,
  EventsResponse,
  MemoryChannelsResponse,
  MemoryFileResponse,
  MemorySearchResponse,
  MemoryTreeResponse,
  OpsPayload,
  SagaActivationHistResponse,
  SagaAtomDetail,
  SagaClustersResponse,
  SagaRecentResponse,
  SagaSearchResponse,
  SagaSqlResponse,
  SagaStatsResponse,
  TurnsResponse
} from "../api/contracts";

export const turnsFixture: TurnsResponse = {
  turns: [
    {
      turn_id: "turn-001",
      ts: "2026-06-17T12:00:00Z",
      trigger: "user_message",
      kind: "user_message",
      channel_id: "web-default",
      duration_ms: 1842,
      input: "Show me recent memory notes",
      output: "I found two relevant notes.",
      events: [
        { type: "reasoning", content: "Need to search memory.", t_ms: 120 },
        {
          type: "tool_call",
          id: "call-search-1",
          name: "memory_search",
          args: { q: "recent memory notes" },
          t_ms: 430
        },
        {
          type: "tool_result",
          id: "call-search-1",
          content: "2 hits",
          is_error: false,
          t_ms: 780
        }
      ],
      saga_calls: [
        {
          call_type: "query",
          args: { q: "recent memory notes" },
          result: { atoms: 2 },
          latency_ms: 91,
          t_ms: 510
        }
      ],
      injected_inputs: [{ text: "Prioritize project state", t_ms: 620 }]
    }
  ]
};

export const eventsFixture: EventsResponse = {
  events: [
    {
      timestamp: "2026-06-17T12:00:00Z",
      type: "event_queued",
      trigger: "user_message",
      channel_id: "web-default"
    },
    {
      timestamp: "2026-06-17T12:00:01Z",
      type: "tool_call",
      tool: "memory_search",
      ok: true,
      duration_ms: 94
    }
  ]
};

export const opsFixture: OpsPayload = {
  generated_at: "2026-06-17T12:05:00Z",
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
  queued_by_channel: { "web-default": 2, "ops": 1 },
  resolution_paths: {
    saga_query_ctx_resolution: { saga_session_id: 2, missing: 1 }
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
      tool: "memory_search",
      calls: 3,
      errors: 0,
      failure_rate: 0,
      avg_duration_ms: 84
    }
  ],
  failures_by_kind: { scheduler_invalid: 1 },
  timeseries: [{ day: "2026-06-17", events: 12, queued: 3 }],
  recent_failures: [
    {
      t: "2026-06-17T11:59:00Z",
      kind: "scheduler_invalid",
      channel_id: "ops",
      trigger: "scheduled_tick",
      detail: "bad schedule"
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
        title: "Define React API contracts for turns, ops, SAGA, state, and chat",
        status: "open",
        priority: "medium",
        parent: 524,
        updated_at: "2026-06-17T12:00:00Z"
      }
    ],
    error: null,
    truncated: false,
    total_count: 1
  },
  usage_history: {
    codex_plus: [{ timestamp: "2026-06-17T12:00:00Z", used_percent: 42 }]
  },
  token_usage_history: [
    { day: "2026-06-17", input_tokens: 1200, output_tokens: 340, total_tokens: 1540 }
  ]
};

export const sagaStatsFixture: SagaStatsResponse = {
  ready: true,
  atom_count: 8,
  tombstoned_count: 1,
  session_count: 3,
  triple_count: 5,
  schema_version: 7,
  db_size_bytes: 49152,
  db_path: "/home/mimir/.mimir/saga.db"
};

export const sagaRecentFixture: SagaRecentResponse = {
  atoms: [
    {
      id: "atom-001",
      content_preview: "The operator prefers scoped React migrations.",
      memory_type: "observation",
      stream: "semantic",
      source_type: "turn",
      topics: ["react", "migration"],
      arousal: 0.2,
      valence: 0.1,
      encoding_confidence: 0.91,
      is_pinned: 0,
      created_at: "2026-06-17T11:50:00Z",
      session_id: "session-001",
      channel_id: "web-default"
    }
  ],
  total: 1,
  limit: 50,
  channel_filter: null,
  channels: ["web-default"]
};

export const sagaAtomFixture: SagaAtomDetail = {
  id: "atom-001",
  content: "The operator prefers scoped React migrations.",
  memory_type: "observation",
  stream: "semantic",
  source_type: "turn",
  topics: ["react", "migration"],
  metadata: { source_turn_id: "turn-001" },
  session_id: "session-001",
  channel_id: "web-default",
  arousal: 0.2,
  valence: 0.1,
  encoding_confidence: 0.91,
  is_pinned: 0,
  created_at: "2026-06-17T11:50:00Z",
  access_count: 2,
  last_access_ts: "2026-06-17T12:00:00Z",
  last_access_source: "query",
  embedding: {
    provider: "test",
    model: "fixture",
    dim: 3,
    embedded_at: "2026-06-17T11:51:00Z"
  },
  relations_out: [
    {
      relation_type: "supports",
      target_id: "atom-002",
      confidence: 0.8,
      target_preview: "Route work can consume typed clients."
    }
  ],
  tombstoned: 0,
  tombstoned_reason: null
};

export const sagaSearchFixture: SagaSearchResponse = {
  ...sagaRecentFixture,
  query: "React",
  total_matched: 1
};

export const sagaActivationFixture: SagaActivationHistResponse = {
  buckets: [
    { range_start: -3.1, range_end: -2.4, count: 2 },
    { range_start: -2.4, range_end: -1.7, count: 1 }
  ],
  total: 3,
  never_accessed: 5,
  days: 7
};

export const sagaClustersFixture: SagaClustersResponse = {
  clusters: [
    {
      cluster_id: "session-001",
      size: 2,
      sample_atoms: [
        { id: "atom-001", content_preview: "The operator prefers scoped React migrations." }
      ]
    }
  ],
  total_clusters: 1,
  total_atoms: 2
};

export const sagaSqlFixture: SagaSqlResponse = {
  columns: ["id", "memory_type"],
  rows: [["atom-001", "observation"]],
  row_count: 1,
  truncated: false
};

export const memoryTreeFixture: MemoryTreeResponse = {
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
          name: "core",
          type: "dir",
          path: "memory/core",
          desc: null,
          children: [
            {
              name: "00-identity.md",
              type: "file",
              path: "memory/core/00-identity.md",
              size: 128,
              modified: "2026-06-17T10:00:00Z",
              desc: "Identity notes"
            }
          ]
        }
      ]
    }
  ]
};

export const memoryFileFixture: MemoryFileResponse = {
  path: "memory/core/00-identity.md",
  content: "<!-- desc: Identity notes -->\n# Identity\n",
  size: 128,
  modified: "2026-06-17T10:00:00Z"
};

export const memorySearchFixture: MemorySearchResponse = {
  query: "identity",
  hits: [
    {
      path: "memory/core/00-identity.md",
      line_no: 1,
      snippet: "<!-- desc: Identity notes -->"
    }
  ],
  total: 1,
  truncated: false
};

export const memoryChannelsFixture: MemoryChannelsResponse = {
  channels: ["web-default", "ops"]
};

export const chatSendFixture: ChatSendResponse = {
  ok: true,
  channel_id: "web-default"
};

export const chatMessageEventFixture: ChatMessageEvent = {
  channel_id: "web-default",
  text: "I found two relevant notes.",
  message_id: "msg-001",
  attachments: []
};

export const chatReactEventFixture: ChatReactEvent = {
  _event: "react",
  channel_id: "web-default",
  message_id: "msg-001",
  emoji: "👍"
};
