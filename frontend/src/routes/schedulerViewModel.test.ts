import { describe, expect, it } from "vitest";
import {
  dueWindowOptions,
  formatDateTime,
  runStateLabel,
  runStateTone,
  safeSchedulerDashboardData
} from "./schedulerViewModel";

describe("scheduler view-model helpers", () => {
  it("normalizes partial scheduler payloads", () => {
    const safe = safeSchedulerDashboardData({
      generated_at: "2026-06-18T12:00:00Z",
      available: true,
      due_window: "7d",
      schedules: [{
        name: "heartbeat",
        priority: "low",
        prompt_source: "file:heartbeat.md",
        recent_result: "scheduled_tick"
      }],
      pollers: [{
        name: "github",
        kind: "poller",
        cron: "*/5 * * * *",
        channel: "poller:github",
        recent_error: "exit=1"
      }],
      commitments: [{
        id: "c-1",
        text: "Review PR",
        due_bucket: "today",
        snooze_count: 2
      }],
      actions: {
        mutations_enabled: false,
        policy: "read-only",
        deferred: ["trigger", "complete"]
      }
    });

    expect(safe.schedules[0]).toMatchObject({
      id: "heartbeat",
      kind: "schedule",
      priority: "low",
      prompt_source: "file:heartbeat.md"
    });
    expect(safe.pollers[0]).toMatchObject({
      id: "github",
      kind: "poller",
      recent_error: "exit=1"
    });
    expect(safe.commitments[0]).toMatchObject({
      id: "c-1",
      status: "pending",
      snooze_count: 2
    });
    expect(safe.actions.deferred).toEqual(["trigger", "complete"]);
  });

  it("exposes due-window filter choices", () => {
    expect(dueWindowOptions.map((option) => option.value)).toEqual([
      "all",
      "overdue",
      "today",
      "7d",
      "30d",
      "later",
      "unanchored"
    ]);
  });

  it("formats state labels and tones from recent run fields", () => {
    expect(runStateLabel({ recent_error: "boom" })).toBe("error");
    expect(runStateTone({ recent_error: "boom" })).toBe("danger");
    expect(runStateLabel({ suppression_reason: "quota" })).toBe("suppressed");
    expect(runStateTone({ suppression_reason: "quota" })).toBe("warning");
    expect(runStateLabel({ recent_result: "ok" })).toBe("ok");
    expect(runStateTone({ recent_result: "ok" })).toBe("success");
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });
});
