import type {
  ChatPostResponse,
  ChatStreamEvent,
  EventsResponse,
  MemoryChannelsResponse,
  MemoryDirNode,
  MemoryFileResponse,
  MemorySearchResponse,
  OpsPayload,
  SagaActivationHistResponse,
  SagaAtomDetails,
  SagaClustersResponse,
  SagaRecentResponse,
  SagaSearchResponse,
  SagaStatsResponse,
  TurnsResponse
} from "../api";

export const turnsFixture: TurnsResponse = {
  turns: [
    {
      turn_id: "turn-001",
      ts: "2026-06-01T12:00:00Z",
      trigger: "user_message",
      kind: "user_message",
      channel_id: "web-default",
      input: "Show me recent memory",
      output: "Found two relevant notes.",
      duration_ms: 1842,
      events: [
        { type: "reasoning", t_ms: 42, content: "Need to query memory." },
        {
          type: "tool_call",
          t_ms: 180,
          id: "call-memory-query",
          name: "saga_query",
          args: { query: "recent memory" }
        },
        {
          type: "tool_result",
          t_ms: 912,
          id: "call-memory-query",
          content: "2 results",
          is_error: false
        }
      ],
      saga_calls: [
        {
          call_type: "query",
          t_ms: 190,
          latency_ms: 720,
          args: { query: "recent memory" },
          result: { count: 2 }
        }
      ],
      injected_inputs: [{ t_ms: 650, text: "Narrow that to today." }]
    }
  ]
};

export const eventsFixture: EventsResponse = {
  events: [
    {
      timestamp: "2026-06-01T12:00:00Z",
      type: "event_queued",
      trigger: "user_message",
      channel_id: "web-default"
    },
    {
      timestamp: "2026-06-01T12:00:01Z",
      type: "tool_call",
      tool: "saga_query",
      ok: true,
      duration_ms: 720
    }
  ]
};

export const opsFixture: OpsPayload = {
  generated_at: "2026-06-01T12:00:05Z",
  window_days: 7,
  summary: {
    total_events: 4,
    events_queued: 1,
    messages_sent: 1,
    subagents_started: 0,
    subagents_completed: 0,
    shell_jobs_spawned: 1,
    shell_jobs_routed: 1,
    failures: 1,
    high_water_events: 0,
    client_pool_drains: 0,
    tool_calls: 1,
    tool_errors: 0
  },
  by_event: {
    event_queued: 1,
    send_message_sent: 1,
    bash_async_spawned: 1,
    shell_job_complete_routed: 1
  },
  queued_by_trigger: { user_message: 1 },
  queued_by_channel: { "web-default": 1 },
  resolution_paths: {
    saga_query_ctx_resolution: { saga_session_id: 1 }
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
      calls: 1,
      errors: 0,
      failure_rate: 0,
      avg_duration_ms: 720
    }
  ],
  failures_by_kind: { git_push_failed: 1 },
  timeseries: [{ day: "2026-06-01", events: 4, queued: 1 }],
  recent_failures: [
    {
      t: "2026-06-01T12:00:04Z",
      kind: "git_push_failed",
      channel_id: "web-default",
      trigger: "user_message",
      detail: "non-fast-forward"
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
    available: false,
    issues: [],
    error: null
  },
  usage_history: {},
  token_usage_history: {}
};

export const sagaRecentFixture: SagaRecentResponse = {
  atoms: [
    {
      id: "atom-001",
      content_preview: "User prefers concise implementation notes.",
      memory_type: "raw",
      stream: "semantic",
      source_type: "conversation",
      topics: ["preferences"],
      arousal: 0.5,
      valence: 0.1,
      encoding_confidence: 0.8,
      is_pinned: 0,
      created_at: "2026-06-01T11:59:00Z",
      session_id: "sess-001",
      channel_id: "web-default"
    }
  ],
  total: 1,
  limit: 50,
  channel_filter: null,
  channels: ["web-default"]
};

export const sagaSearchFixture: SagaSearchResponse = {
  atoms: sagaRecentFixture.atoms,
  total_matched: 1,
  query: "concise",
  channel_filter: null,
  limit: 100
};

export const sagaAtomFixture: SagaAtomDetails = {
  id: "atom-001",
  content: "User prefers concise implementation notes.",
  content_hash: "hash-atom-001",
  memory_type: "raw",
  stream: "semantic",
  source_type: "conversation",
  topics: ["preferences"],
  metadata: {},
  tombstoned: 0,
  is_pinned: 0,
  agent_id: "default",
  session_id: "sess-001",
  channel_id: "web-default",
  session_started_at: "2026-06-01T11:50:00Z",
  created_at: "2026-06-01T11:59:00Z",
  access_count: 2,
  last_access_ts: "2026-06-01T12:00:00Z",
  last_access_source: "retrieval",
  embedding: {
    provider: "voyage",
    model: "voyage-4-lite",
    dim: 256,
    embedded_at: "2026-06-01T11:59:10Z"
  },
  relations_out: [
    {
      relation_type: "evidenced_by",
      target_id: "atom-002",
      confidence: 0.9,
      target_preview: "Preference observed in chat."
    }
  ]
};

export const sagaStatsFixture: SagaStatsResponse = {
  ready: true,
  atom_count: 12,
  tombstoned_count: 1,
  session_count: 3,
  triple_count: 4,
  schema_version: 6,
  db_size_bytes: 8192,
  db_path: "/home/mimir/.mimir/saga.db"
};

export const sagaActivationHistFixture: SagaActivationHistResponse = {
  buckets: [
    { range_start: -9.2, range_end: -8.5, count: 2 },
    { range_start: -8.5, range_end: -7.8, count: 4 }
  ],
  total: 6,
  never_accessed: 3,
  days: 7
};

export const sagaClustersFixture: SagaClustersResponse = {
  clusters: [
    {
      cluster_id: "sess-001",
      size: 2,
      sample_atoms: [
        { id: "atom-001", content_preview: "User prefers concise..." }
      ]
    }
  ],
  total_clusters: 1,
  total_atoms: 2
};

export const memoryTreeFixture: MemoryDirNode = {
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
          size: 42,
          modified: "2026-06-01T12:00:00+00:00",
          desc: "memory index"
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
  content: "<!-- desc: memory index -->\n# Memory\n",
  size: 42,
  modified: "2026-06-01T12:00:00+00:00"
};

export const memorySearchFixture: MemorySearchResponse = {
  query: "memory",
  hits: [
    {
      path: "memory/INDEX.md",
      line_no: 2,
      snippet: "# Memory"
    }
  ],
  total: 1,
  truncated: false
};

export const memoryChannelsFixture: MemoryChannelsResponse = {
  channels: ["web-default"]
};

export const chatPostFixture: ChatPostResponse = {
  ok: true,
  channel_id: "web-default"
};

export const chatStreamFixtures: ChatStreamEvent[] = [
  {
    channel_id: "web-default",
    text: "Hello from the agent.",
    message_id: "msg-001",
    attachments: []
  },
  {
    _event: "react",
    channel_id: "web-default",
    message_id: "msg-001",
    emoji: "thumbs_up"
  }
];
