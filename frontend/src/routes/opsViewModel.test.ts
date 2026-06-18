import { describe, expect, it } from "vitest";
import { opsDashboardFixture } from "../fixtures/api";
import {
  buildOpsSummaryMetrics,
  formatCost,
  formatPercent,
  mapToRows,
  quotaRows,
  safeOpsDashboardData,
  schedulerEventRows,
  tokenUsageRows
} from "./opsViewModel";

describe("ops view-model helpers", () => {
  it("builds representative happy-path rows from the ops fixture", () => {
    const summary = buildOpsSummaryMetrics(opsDashboardFixture.summary);
    expect(summary).toContainEqual({
      key: "failures",
      label: "Failures",
      value: 1,
      tone: "danger"
    });
    expect(summary).toContainEqual({
      key: "messages_sent",
      label: "Messages sent",
      value: 2,
      tone: "neutral"
    });

    expect(quotaRows(opsDashboardFixture.usage_history)).toEqual([
      {
        provider: "codex_plus",
        window: "seven_day",
        points: 1,
        latestUtilization: 0.42
      }
    ]);
    expect(tokenUsageRows(opsDashboardFixture.token_usage_history)).toEqual([
      {
        date: "2026-06-17",
        turns: 1,
        input: 1200,
        cacheCreation: 200,
        cacheRead: 800,
        output: 300,
        cost: null
      }
    ]);
    expect(schedulerEventRows(opsDashboardFixture)).toEqual([
      { key: "event_queued", value: 3 }
    ]);
  });

  it("sorts numeric map rows by descending value then key", () => {
    expect(mapToRows({ beta: 2, alpha: 2, gamma: 3 })).toEqual([
      { key: "gamma", value: 3 },
      { key: "alpha", value: 2 },
      { key: "beta", value: 2 }
    ]);
  });

  it("normalizes partial or drifted payloads instead of throwing", () => {
    const safe = safeOpsDashboardData({
      generated_at: "2026-06-18T00:00:00Z",
      summary: { total_events: 4, failures: Number.NaN, ignored: "5" },
      by_event: undefined,
      shell_jobs: { spawned: 2, spawn_by_channel: { web: 2, bad: "x" } },
      tools: [{ tool: "grep", calls: 3, errors: "bad", failure_rate: Number.NaN }],
      token_usage_history: [{ date: "2026-06-18", turn_count: "bad", output_tokens: 5 }],
      chainlink_issues: { available: true, issues: [{ id: 530, title: "Port ops" }] }
    });

    expect(safe.summary).toEqual({ total_events: 4 });
    expect(safe.by_event).toEqual({});
    expect(safe.queued_by_trigger).toEqual({});
    expect(safe.resolution_paths).toEqual({});
    expect(safe.shell_jobs).toEqual({
      spawned: 2,
      routed: 0,
      no_channel: 0,
      enqueue_failed: 0,
      spawn_by_channel: { web: 2 }
    });
    expect(safe.tools).toEqual([
      { tool: "grep", calls: 3, errors: 0, failure_rate: null, avg_duration_ms: 0 }
    ]);
    expect(safe.timeseries).toEqual([]);
    expect(safe.recent_failures).toEqual([]);
    expect(safe.backlog).toEqual([]);
    expect(safe.chainlink_issues).toMatchObject({
      available: true,
      issues: [{ id: 530, title: "Port ops" }],
      truncated: false
    });

    expect(tokenUsageRows(safe.token_usage_history)).toEqual([
      {
        date: "2026-06-18",
        turns: 0,
        input: 0,
        cacheCreation: 0,
        cacheRead: 0,
        output: 5,
        cost: null
      }
    ]);
    expect(quotaRows(undefined)).toEqual([]);
    expect(schedulerEventRows(safe)).toEqual([]);
  });

  it("renders invalid numeric display values as unavailable", () => {
    expect(formatPercent(null)).toBe("n/a");
    expect(formatPercent(Number.NaN)).toBe("n/a");
    expect(formatPercent(0.125)).toBe("12.5%");
    expect(formatCost(null)).toBe("n/a");
    expect(formatCost(Number.POSITIVE_INFINITY)).toBe("n/a");
    expect(formatCost(1.23456)).toBe("$1.2346");
  });
});
