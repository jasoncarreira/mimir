import type { ChainlinkBoardData, ChainlinkBoardIssue } from "../api";
import { sanitizeHref } from "../routeState";

export const chainlinkColumns = ["open", "ready", "blocked", "in-progress", "review", "done"] as const;

export type ChainlinkColumnId = typeof chainlinkColumns[number];

export type ChainlinkBoardFilters = {
  label: string;
  status: string;
  priority: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function recordArrayFrom(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function numberFrom(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringFrom(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function stringArrayFrom(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function numberArrayFrom(value: unknown): number[] {
  return Array.isArray(value)
    ? value.map(numberFrom).filter((item): item is number => item !== null)
    : [];
}

function safeIssue(raw: Record<string, unknown>): ChainlinkBoardIssue | null {
  const id = numberFrom(raw.id);
  if (id === null) return null;
  const progress = isRecord(raw.child_progress) ? raw.child_progress : {};
  const worklink = isRecord(raw.worklink) ? raw.worklink : null;
  return {
    id,
    title: stringFrom(raw.title, `Issue #${id}`),
    status: stringFrom(raw.status, "open"),
    raw_status: stringFrom(raw.raw_status, "open"),
    priority: stringFrom(raw.priority, "normal"),
    labels: stringArrayFrom(raw.labels),
    parent_id: numberFrom(raw.parent_id),
    child_ids: numberArrayFrom(raw.child_ids),
    child_progress: {
      done: numberFrom(progress.done) ?? 0,
      total: numberFrom(progress.total) ?? 0
    },
    blocked_by: numberArrayFrom(raw.blocked_by),
    blocking: numberArrayFrom(raw.blocking),
    updated_at: stringFrom(raw.updated_at),
    created_at: stringFrom(raw.created_at),
    description: stringFrom(raw.description),
    comments: recordArrayFrom(raw.comments).map((comment, index) => ({
      id: stringFrom(comment.id, String(index)),
      author: stringFrom(comment.author),
      created_at: stringFrom(comment.created_at),
      body: stringFrom(comment.body)
    })).filter((comment) => comment.body),
    worklink: worklink ? {
      issue: numberFrom(worklink.issue) ?? id,
      attempt: numberFrom(worklink.attempt) ?? 0,
      backend: stringFrom(worklink.backend),
      status: stringFrom(worklink.status, "unknown"),
      branch: stringFrom(worklink.branch),
      started_at: stringFrom(worklink.started_at),
      finished_at: stringFrom(worklink.finished_at),
      diff_stat: stringFrom(worklink.diff_stat),
      tests: isRecord(worklink.tests) ? worklink.tests : null,
      pr_url: sanitizeHref(stringFrom(worklink.pr_url)) || "",
      blocked_reason: stringFrom(worklink.blocked_reason),
      transcript: stringFrom(worklink.transcript),
      transcript_href: sanitizeHref(stringFrom(worklink.transcript_href)) || "",
      evidence_path: stringFrom(worklink.evidence_path),
      evidence_href: sanitizeHref(stringFrom(worklink.evidence_href)) || ""
    } : null
  };
}

export function safeChainlinkBoardData(data: unknown): ChainlinkBoardData {
  const source = isRecord(data) ? data : {};
  const filters = isRecord(source.filters) ? source.filters : {};
  const issues = recordArrayFrom(source.issues)
    .map(safeIssue)
    .filter((issue): issue is ChainlinkBoardIssue => issue !== null);
  const issueIds = new Set(issues.map((issue) => issue.id));
  const columns = chainlinkColumns.map((status) => ({
    id: status,
    title: status.replace("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()),
    issue_ids: issues.filter((issue) => issue.status === status).map((issue) => issue.id)
  }));

  return {
    available: typeof source.available === "boolean" ? source.available : false,
    error: typeof source.error === "string" ? source.error : null,
    generated_at: stringFrom(source.generated_at),
    columns,
    issues,
    roots: numberArrayFrom(source.roots).filter((id) => issueIds.has(id)),
    edges: recordArrayFrom(source.edges).map((edge) => ({
      from: numberFrom(edge.from) ?? 0,
      to: numberFrom(edge.to) ?? 0,
      kind: stringFrom(edge.kind, "edge")
    })).filter((edge) => issueIds.has(edge.from) && issueIds.has(edge.to)),
    filters: {
      labels: stringArrayFrom(filters.labels),
      statuses: stringArrayFrom(filters.statuses),
      priorities: stringArrayFrom(filters.priorities)
    },
    truncated: typeof source.truncated === "boolean" ? source.truncated : false,
    total_count: numberFrom(source.total_count) ?? issues.length
  };
}

export function issueMatchesFilters(issue: ChainlinkBoardIssue, filters: ChainlinkBoardFilters): boolean {
  if (filters.status && issue.status !== filters.status) return false;
  if (filters.priority && issue.priority !== filters.priority) return false;
  if (filters.label && !issue.labels.includes(filters.label)) return false;
  return true;
}

const COMPLETED_STATUSES = new Set(["done", "closed"]);

// github #569: "open tasks" = anything not finished. Closed/done work is hidden
// by default so the board shows active work.
export function isCompletedStatus(status: string): boolean {
  return COMPLETED_STATUSES.has(status);
}

const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, normal: 2, low: 3 };

function byPriority(a: ChainlinkBoardIssue, b: ChainlinkBoardIssue): number {
  return (PRIORITY_RANK[a.priority] ?? 2) - (PRIORITY_RANK[b.priority] ?? 2) || a.id - b.id;
}

export interface ReadyDependency {
  issue: ChainlinkBoardIssue;
  unlocks: ChainlinkBoardIssue[];
}

export interface BlockedDependency {
  issue: ChainlinkBoardIssue;
  blockers: ChainlinkBoardIssue[];
}

export interface DependencyPartition {
  ready: ReadyDependency[];
  blocked: BlockedDependency[];
}

// github #570: answer "what is blocked, by what, and what unlocks next" instead
// of dumping raw edges. Over active (not done/closed) issues:
//   - blocked: has >=1 unresolved (still-active) blocker → list those blockers.
//   - ready:   not blocked AND blocks >=1 still-active issue → finishing it
//              unlocks that downstream work.
// Issues with no live dependency edges aren't listed — they live in the columns.
export function partitionDependencies(issues: ChainlinkBoardIssue[]): DependencyPartition {
  const byId = new Map(issues.map((issue) => [issue.id, issue]));
  const activeFrom = (ids: number[]): ChainlinkBoardIssue[] =>
    ids
      .map((id) => byId.get(id))
      .filter((item): item is ChainlinkBoardIssue => item !== undefined && !isCompletedStatus(item.status));
  const ready: ReadyDependency[] = [];
  const blocked: BlockedDependency[] = [];
  for (const issue of issues) {
    if (isCompletedStatus(issue.status)) continue;
    const blockers = activeFrom(issue.blocked_by);
    if (blockers.length) {
      blocked.push({ issue, blockers });
      continue;
    }
    const unlocks = activeFrom(issue.blocking);
    if (unlocks.length) ready.push({ issue, unlocks });
  }
  ready.sort((a, b) => byPriority(a.issue, b.issue));
  blocked.sort((a, b) => byPriority(a.issue, b.issue));
  return { ready, blocked };
}

export function formatBoardTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 19).replace("T", " ");
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}
