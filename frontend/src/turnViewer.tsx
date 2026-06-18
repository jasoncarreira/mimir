import { useMutation, useQuery } from "@tanstack/react-query";
import React from "react";
import { useSearchParams } from "react-router-dom";
import { listTurns } from "./api";
import type { InjectedInput, SagaCall, TurnEvent, TurnRecord } from "./api";
import { Badge, Button, CodeBlock, ErrorState, LoadingState, Panel, TextInput } from "./ui";

const PAGE_SIZE = 100;
const KNOWN_KEYS = new Set([
  "turn_id",
  "ts",
  "trigger",
  "kind",
  "channel_id",
  "input",
  "output",
  "error",
  "duration_ms",
  "events",
  "saga_calls",
  "injected_inputs",
  "usage"
]);

type TimelineEntry =
  | { kind: "event"; tMs: number | null; index: number; event: TurnEvent }
  | { kind: "saga"; tMs: number | null; index: number; call: SagaCall }
  | { kind: "injected"; tMs: number | null; index: number; text: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringify(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function formatTime(ts?: string): string {
  if (!ts) return "unknown";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return "unknown";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function formatRelative(ms: number | null): string {
  if (typeof ms !== "number") return "";
  return ms < 10000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function eventTitle(event: TurnEvent, index: number): string {
  const type = String(event.type || "unknown");
  if (type === "reasoning") return `Reasoning ${index + 1}`;
  if (type === "tool_call") return `Tool call: ${String(event.name || "unknown")}`;
  if (type === "tool_result") return `Tool result${event.is_error ? " error" : ""}`;
  if (type.includes("feedback") || type.includes("algedonic")) return `Feedback: ${type}`;
  return type;
}

function eventBody(event: TurnEvent): string {
  if (event.type === "reasoning") return stringify(event.content);
  if (event.type === "tool_call") return stringify(event.args ?? event);
  if (event.type === "tool_result") return stringify(event.content ?? event.result ?? event);
  return stringify(event);
}

function eventTone(event: TurnEvent): "neutral" | "info" | "success" | "warning" | "danger" {
  if (event.type === "reasoning") return "info";
  if (event.type === "tool_call") return "warning";
  if (event.type === "tool_result") return event.is_error ? "danger" : "success";
  if (String(event.type).includes("feedback") || String(event.type).includes("algedonic")) return "info";
  return "neutral";
}

function normalizeInjected(input: Array<InjectedInput | string> | undefined): TimelineEntry[] {
  return (input ?? []).map((item, index) => {
    if (typeof item === "string") {
      return { kind: "injected", tMs: null, index, text: item };
    }
    return {
      kind: "injected",
      tMs: typeof item.t_ms === "number" ? item.t_ms : null,
      index,
      text: item.text || ""
    };
  });
}

export function buildTurnTimeline(turn: TurnRecord): TimelineEntry[] {
  const entries: TimelineEntry[] = [
    ...(turn.events ?? []).map((event, index) => ({
      kind: "event" as const,
      tMs: typeof event.t_ms === "number" ? event.t_ms : null,
      index,
      event
    })),
    ...(turn.saga_calls ?? []).map((call, index) => ({
      kind: "saga" as const,
      tMs: typeof call.t_ms === "number" ? call.t_ms : null,
      index,
      call
    })),
    ...normalizeInjected(turn.injected_inputs)
  ];
  return entries.sort((a, b) => {
    if (a.tMs === null && b.tMs === null) return a.index - b.index;
    if (a.tMs === null) return 1;
    if (b.tMs === null) return -1;
    return a.tMs - b.tMs;
  });
}

function TurnSummary({ turn }: { turn: TurnRecord }) {
  return (
    <dl className="turn-facts">
      <div><dt>Trigger</dt><dd><Badge>{turn.trigger || "unknown"}</Badge></dd></div>
      <div><dt>Kind</dt><dd>{turn.kind || "none"}</dd></div>
      <div><dt>Channel</dt><dd>{turn.channel_id || "none"}</dd></div>
      <div><dt>Duration</dt><dd>{formatDuration(turn.duration_ms)}</dd></div>
      <div><dt>Events</dt><dd>{turn.events?.length ?? 0}</dd></div>
      <div><dt>Saga calls</dt><dd>{turn.saga_calls?.length ?? 0}</dd></div>
    </dl>
  );
}

function TimelineItem({ entry }: { entry: TimelineEntry }) {
  const tLabel = formatRelative(entry.tMs);
  if (entry.kind === "event") {
    const event = entry.event;
    const id = String(event.id || event.call_id || "");
    return (
      <details className={`turn-event turn-event--${eventTone(event)}`} open>
        <summary>
          <span>{eventTitle(event, entry.index)}</span>
          {id ? <code>{id.slice(0, 18)}</code> : null}
          {tLabel ? <small>{tLabel}</small> : null}
        </summary>
        <CodeBlock code={eventBody(event) || "(empty)"} language="json" />
      </details>
    );
  }
  if (entry.kind === "saga") {
    const call = entry.call;
    const body = [
      call.error ? `error: ${call.error}` : "",
      `args:\n${stringify(call.args) || "(empty)"}`,
      `result:\n${stringify(call.result) || "(empty)"}`
    ].filter(Boolean).join("\n\n");
    return (
      <details className={`turn-event turn-event--${call.error ? "danger" : "neutral"}`} open>
        <summary>
          <span>Saga {call.call_type || "unknown"}</span>
          {call.latency_ms !== null && call.latency_ms !== undefined ? <small>{Math.round(call.latency_ms)}ms</small> : null}
          {tLabel ? <small>{tLabel}</small> : null}
        </summary>
        <CodeBlock code={body} language="json" />
      </details>
    );
  }
  return (
    <details className="turn-event turn-event--info" open>
      <summary>
        <span>Mid-turn user message</span>
        {tLabel ? <small>{tLabel}</small> : null}
      </summary>
      <CodeBlock code={entry.text || "(empty)"} />
    </details>
  );
}

function MetadataSection({ turn }: { turn: TurnRecord }) {
  const extra = Object.fromEntries(
    Object.entries(turn).filter(([key]) => !KNOWN_KEYS.has(key))
  );
  const hasUsage = isRecord(turn.usage) && Object.keys(turn.usage).length > 0;
  const hasExtra = Object.keys(extra).length > 0;
  if (!hasUsage && !hasExtra) {
    return <p className="muted-copy">No metadata recorded for this turn.</p>;
  }
  return (
    <div className="turn-stack">
      {hasUsage ? <CodeBlock title="Usage" code={stringify(turn.usage)} language="json" /> : null}
      {hasExtra ? <CodeBlock title="Additional fields" code={stringify(extra)} language="json" /> : null}
    </div>
  );
}

export function TurnDetail({ turn }: { turn?: TurnRecord }) {
  if (!turn) {
    return (
      <Panel title="Turn detail">
        <div className="ui-state">
          <h3>Select a turn</h3>
          <p>Recent turns appear on the left when records are available.</p>
        </div>
      </Panel>
    );
  }
  const timeline = buildTurnTimeline(turn);
  return (
    <Panel
      className="turn-detail"
      title={turn.turn_id || "Unknown turn"}
      subtitle={`${formatTime(turn.ts)} · ${turn.error ? "errored" : "completed or in progress"}`}
    >
      <TurnSummary turn={turn} />
      {turn.error ? (
        <details className="turn-section turn-section--error" open>
          <summary>Error</summary>
          <CodeBlock code={turn.error} />
        </details>
      ) : null}
      <details className="turn-section" open>
        <summary>Input</summary>
        <CodeBlock code={turn.input || "(empty)"} />
      </details>
      <details className="turn-section" open>
        <summary>Output</summary>
        <CodeBlock code={turn.output || "(empty)"} />
      </details>
      <details className="turn-section" open>
        <summary>Events and calls</summary>
        {timeline.length ? (
          <div className="turn-stack">
            {timeline.map((entry) => (
              <TimelineItem
                entry={entry}
                key={`${entry.kind}:${entry.index}:${entry.tMs ?? "untimed"}`}
              />
            ))}
          </div>
        ) : (
          <div className="ui-state">
            <h3>No events recorded</h3>
            <p>This turn payload has no reasoning, tool call, tool result, feedback, or saga events.</p>
          </div>
        )}
      </details>
      <details className="turn-section">
        <summary>Metadata</summary>
        <MetadataSection turn={turn} />
      </details>
    </Panel>
  );
}

function rowText(turn: TurnRecord): string {
  return [turn.input, turn.output, turn.error, ...(turn.injected_inputs ?? []).map((item) => (
    typeof item === "string" ? item : item.text
  ))].filter(Boolean).join(" ").toLowerCase();
}

export function TurnViewerRoute() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("turn") || "";
  const [turns, setTurns] = React.useState<TurnRecord[]>([]);
  const [query, setQuery] = React.useState("");
  const [trigger, setTrigger] = React.useState("all");
  const [hidePollers, setHidePollers] = React.useState(true);

  const initial = useQuery({
    queryKey: ["turns", "recent"],
    queryFn: async () => {
      const envelope = await listTurns({ limit: PAGE_SIZE });
      return envelope.data.turns.slice().reverse();
    }
  });

  React.useEffect(() => {
    if (initial.data) setTurns(initial.data);
  }, [initial.data]);

  const loadOlder = useMutation({
    mutationFn: async () => {
      const oldest = turns.at(-1)?.turn_id;
      if (!oldest) return [];
      const envelope = await listTurns({ before: oldest, limit: PAGE_SIZE });
      return envelope.data.turns.slice().reverse();
    },
    onSuccess: (older) => {
      setTurns((current) => [...current, ...older.filter((turn) => !current.some((t) => t.turn_id === turn.turn_id))]);
    }
  });

  const refreshNew = useMutation({
    mutationFn: async () => {
      const newest = turns[0]?.turn_id;
      if (!newest) {
        const envelope = await listTurns({ limit: PAGE_SIZE });
        return envelope.data.turns.slice().reverse();
      }
      const envelope = await listTurns({ after: newest });
      return envelope.data.turns.slice().reverse();
    },
    onSuccess: (newer) => {
      setTurns((current) => [...newer.filter((turn) => !current.some((t) => t.turn_id === turn.turn_id)), ...current]);
    }
  });

  const triggers = React.useMemo(() => (
    Array.from(new Set(turns.map((turn) => String(turn.trigger || "unknown")))).sort()
  ), [turns]);
  const filtered = turns.filter((turn) => {
    if (trigger !== "all" && turn.trigger !== trigger) return false;
    if (hidePollers && trigger === "all" && turn.trigger === "poller") return false;
    if (query.trim() && !rowText(turn).includes(query.trim().toLowerCase())) return false;
    return true;
  });
  const selected = turns.find((turn) => turn.turn_id === selectedId) ?? filtered[0];

  function selectTurn(turn: TurnRecord) {
    const next = new URLSearchParams(params);
    if (turn.turn_id) next.set("turn", turn.turn_id);
    else next.delete("turn");
    setParams(next);
  }

  return (
    <div className="turn-viewer">
      <header className="turn-viewer__header">
        <div>
          <p className="ui-eyebrow">Turn viewer</p>
          <h1>Turns</h1>
          <p>Browse recent records and inspect messages, reasoning, tool activity, feedback, and metadata.</p>
        </div>
        <div className="turn-viewer__actions">
          <Button disabled={refreshNew.isPending} onClick={() => refreshNew.mutate()} type="button">
            Refresh
          </Button>
          <Button disabled={!turns.length || loadOlder.isPending} onClick={() => loadOlder.mutate()} type="button">
            Load older
          </Button>
        </div>
      </header>

      <div className="turn-viewer__layout">
        <Panel
          className="turn-list-panel"
          title="Recent turns"
          subtitle={`${filtered.length} visible · ${turns.length} loaded`}
        >
          <div className="turn-controls">
            <TextInput
              aria-label="Search turns"
              placeholder="Search input, output, injected messages"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <label className="turn-checkbox">
              <input
                checked={hidePollers}
                type="checkbox"
                onChange={(event) => setHidePollers(event.target.checked)}
              />
              <span>Hide pollers</span>
            </label>
          </div>
          <div className="turn-trigger-tabs" role="list">
            <Button
              aria-pressed={trigger === "all"}
              type="button"
              variant={trigger === "all" ? "primary" : "secondary"}
              onClick={() => setTrigger("all")}
            >
              All
            </Button>
            {triggers.map((item) => (
              <Button
                aria-pressed={trigger === item}
                key={item}
                type="button"
                variant={trigger === item ? "primary" : "secondary"}
                onClick={() => setTrigger(item)}
              >
                {item}
              </Button>
            ))}
          </div>
          {initial.isLoading ? <LoadingState label="Loading turns" /> : null}
          {initial.isError ? (
            <ErrorState title="Could not load turns">
              {initial.error instanceof Error ? initial.error.message : String(initial.error)}
            </ErrorState>
          ) : null}
          {!initial.isLoading && !initial.isError && filtered.length === 0 ? (
            <div className="ui-state">
              <h3>{turns.length ? "No matching turns" : "No turns recorded"}</h3>
              <p>{turns.length ? "Clear the search or trigger filter." : "The turn log is missing or empty."}</p>
            </div>
          ) : null}
          {filtered.length ? (
            <div className="turn-list" role="list">
              {filtered.map((turn) => (
                <button
                  className={`turn-row${turn.turn_id === selected?.turn_id ? " turn-row--selected" : ""}`}
                  key={turn.turn_id || `${turn.ts}:${turn.input}`}
                  onClick={() => selectTurn(turn)}
                  type="button"
                >
                  <span className="turn-row__top">
                    <strong>{formatTime(turn.ts)}</strong>
                    <Badge tone={turn.error ? "danger" : "neutral"}>{turn.trigger || "unknown"}</Badge>
                  </span>
                  <span className="turn-row__meta">
                    {turn.channel_id || "no channel"} · {formatDuration(turn.duration_ms)} · {turn.events?.length ?? 0} events
                  </span>
                  <span className="turn-row__text">{turn.input || turn.output || turn.error || "(empty turn)"}</span>
                </button>
              ))}
            </div>
          ) : null}
          {loadOlder.isError ? (
            <ErrorState title="Could not load older turns">
              {loadOlder.error instanceof Error ? loadOlder.error.message : String(loadOlder.error)}
            </ErrorState>
          ) : null}
        </Panel>
        <TurnDetail turn={selected} />
      </div>
    </div>
  );
}
