import type { JsonObject } from "../api/generated/contracts";
import type { SchedulerDashboardResponse } from "../api/scheduler";

export type DueWindowOption = {
  value: string;
  label: string;
};

export const dueWindowOptions: DueWindowOption[] = [
  { value: "all", label: "All active" },
  { value: "overdue", label: "Overdue" },
  { value: "today", label: "Today" },
  { value: "7d", label: "Next 7d" },
  { value: "30d", label: "Next 30d" },
  { value: "later", label: "Later" },
  { value: "unanchored", label: "Unanchored" }
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function recordArrayFrom(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function stringFrom(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableStringFrom(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function numberFrom(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function boolFrom(value: unknown): boolean {
  return typeof value === "boolean" ? value : false;
}

function stringArrayFrom(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}

function jsonObjectFrom(value: unknown): JsonObject {
  return isRecord(value) ? value as JsonObject : {};
}

export type SafeSchedulerDashboardData = SchedulerDashboardResponse;

function normalizeRun(value: Record<string, unknown>) {
  return {
    id: stringFrom(value.id, stringFrom(value.name, "unknown")),
    name: stringFrom(value.name, "unknown"),
    kind: stringFrom(value.kind) === "poller" ? "poller" as const : "schedule" as const,
    cron: nullableStringFrom(value.cron),
    time_of_day: nullableStringFrom(value.time_of_day),
    next_run_at: nullableStringFrom(value.next_run_at),
    last_run_at: nullableStringFrom(value.last_run_at),
    channel: nullableStringFrom(value.channel),
    deliver: nullableStringFrom(value.deliver),
    priority: stringFrom(value.priority, "normal"),
    prompt_source: stringFrom(value.prompt_source, "unknown"),
    recent_result: nullableStringFrom(value.recent_result),
    recent_error: nullableStringFrom(value.recent_error),
    suppression_reason: nullableStringFrom(value.suppression_reason),
    suppression_severity: nullableStringFrom(value.suppression_severity),
    manifest_path: nullableStringFrom(value.manifest_path),
    pass_env: stringArrayFrom(value.pass_env),
    env_required: stringArrayFrom(value.env_required),
    config: jsonObjectFrom(value.config)
  };
}

function normalizeCommitment(value: Record<string, unknown>) {
  return {
    id: stringFrom(value.id, "unknown"),
    text: stringFrom(value.text),
    status: stringFrom(value.status, "pending"),
    kind: stringFrom(value.kind, "open_loop"),
    sensitivity: stringFrom(value.sensitivity, "routine"),
    channel: nullableStringFrom(value.channel),
    recipient_identity: nullableStringFrom(value.recipient_identity),
    due_window_start: nullableStringFrom(value.due_window_start),
    due_window_end: nullableStringFrom(value.due_window_end),
    due_window_hint: nullableStringFrom(value.due_window_hint),
    due_bucket: stringFrom(value.due_bucket, "unanchored"),
    attempts: numberFrom(value.attempts),
    snooze_count: numberFrom(value.snooze_count),
    snoozed_until: nullableStringFrom(value.snoozed_until),
    suggested_reminder: stringFrom(value.suggested_reminder),
    source_turn_id: nullableStringFrom(value.source_turn_id)
  };
}

export function safeSchedulerDashboardData(data: unknown): SafeSchedulerDashboardData {
  const source = isRecord(data) ? data : {};
  const actions = isRecord(source.actions) ? source.actions : {};
  return {
    generated_at: stringFrom(source.generated_at),
    available: boolFrom(source.available),
    due_window: stringFrom(source.due_window, "all"),
    schedules: recordArrayFrom(source.schedules).map(normalizeRun),
    pollers: recordArrayFrom(source.pollers).map(normalizeRun),
    commitments: recordArrayFrom(source.commitments).map(normalizeCommitment),
    actions: {
      mutations_enabled: boolFrom(actions.mutations_enabled),
      policy: stringFrom(actions.policy),
      deferred: Array.isArray(actions.deferred) ? actions.deferred.map((item) => String(item)) : []
    }
  };
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "n/a";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function runStateTone(run: { recent_error?: string | null; suppression_reason?: string | null; recent_result?: string | null }) {
  if (run.recent_error) return "danger" as const;
  if (run.suppression_reason) return "warning" as const;
  return "success" as const;
}

export function runStateLabel(run: { recent_error?: string | null; suppression_reason?: string | null; recent_result?: string | null }) {
  if (run.recent_error) return "error";
  if (run.suppression_reason) return "suppressed";
  if (run.recent_result) return "ok";
  return "configured";
}
