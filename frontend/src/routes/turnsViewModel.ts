import type { InjectedInput, SagaCall, TurnEvent, TurnRecord } from "../api";

export type TriggerFilter =
  | "all"
  | "user_message"
  | "scheduled_tick"
  | "saga_session_end"
  | "poller"
  | "claude_code_spawn"
  | "shell_job_complete";

export type SafeTurn = Omit<TurnRecord, "events" | "saga_calls" | "injected_inputs"> & {
  turn_id: string;
  ts: string;
  trigger: string;
  kind: string | null;
  channel_id: string | null;
  input: string;
  output: string;
  error: string | null;
  duration_ms: number | null;
  events: TurnEvent[];
  saga_calls: SagaCall[];
  injected_inputs: InjectedInput[];
  metadata: Record<string, unknown>;
};

const knownTopLevel = new Set([
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
  "metadata"
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringFrom(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableStringFrom(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function nullableNumberFrom(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function eventArrayFrom(value: unknown): TurnEvent[] {
  return Array.isArray(value) ? value.filter(isRecord).map((event) => ({ ...event, type: stringFrom(event.type, "unknown") })) : [];
}

function sagaArrayFrom(value: unknown): SagaCall[] {
  return Array.isArray(value) ? value.filter(isRecord).map((call) => ({ ...call })) : [];
}

export function normalizeInjectedInputs(value: unknown): InjectedInput[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return { t_ms: null, text: item };
    if (!isRecord(item)) return { t_ms: null, text: "" };
    return {
      t_ms: nullableNumberFrom(item.t_ms),
      text: stringFrom(item.text)
    };
  }).filter((item) => item.text);
}

export function safeTurn(record: TurnRecord, index = 0): SafeTurn {
  const source = isRecord(record) ? record : {};
  const metadata = {
    ...(isRecord(source.metadata) ? source.metadata : {}),
    ...Object.fromEntries(Object.entries(source).filter(([key]) => !knownTopLevel.has(key)))
  };
  return {
    ...(source as TurnRecord),
    turn_id: stringFrom(source.turn_id, `turn-${index + 1}`),
    ts: stringFrom(source.ts),
    trigger: stringFrom(source.trigger, "unknown"),
    kind: nullableStringFrom(source.kind),
    channel_id: nullableStringFrom(source.channel_id),
    input: stringFrom(source.input),
    output: stringFrom(source.output),
    error: nullableStringFrom(source.error),
    duration_ms: nullableNumberFrom(source.duration_ms),
    events: eventArrayFrom(source.events),
    saga_calls: sagaArrayFrom(source.saga_calls),
    injected_inputs: normalizeInjectedInputs(source.injected_inputs),
    metadata
  };
}

export function safeTurns(records: TurnRecord[] | undefined): SafeTurn[] {
  return (Array.isArray(records) ? records : []).map(safeTurn);
}

export function filterTurns(
  turns: SafeTurn[],
  { trigger, hidePollers, query }: { trigger: TriggerFilter; hidePollers: boolean; query: string }
): SafeTurn[] {
  const q = query.trim().toLowerCase();
  return turns.filter((turn) => {
    if (trigger !== "all" && turn.trigger !== trigger) return false;
    if (hidePollers && trigger === "all" && turn.trigger === "poller") return false;
    if (!q) return true;
    return [turn.input, turn.output, turn.error ?? "", ...turn.injected_inputs.map((input) => input.text)]
      .some((value) => value.toLowerCase().includes(q));
  });
}

export function formatTurnTime(ts: string): string {
  if (!ts) return "-";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(date);
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "-";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

export function formatRelativeMs(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  return ms < 10000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function eventLabel(event: TurnEvent): string {
  if (event.type === "reasoning") return "Reasoning";
  if (event.type === "tool_call") return stringFrom(event.name, "Tool call");
  if (event.type === "tool_result") return event.is_error ? "Tool result error" : "Tool result";
  if (event.type.includes("feedback") || event.type.includes("algedonic")) return "Feedback";
  return event.type || "Unknown event";
}

export function eventTone(event: TurnEvent): "reasoning" | "tool" | "success" | "danger" | "feedback" | "neutral" {
  if (event.type === "reasoning") return "reasoning";
  if (event.type === "tool_call") return "tool";
  if (event.type === "tool_result") return event.is_error ? "danger" : "success";
  if (event.type.includes("feedback") || event.type.includes("algedonic")) return "feedback";
  return "neutral";
}


export function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
