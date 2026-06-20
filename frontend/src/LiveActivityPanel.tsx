import React from "react";
import { useTurnSpans, type TurnSpan } from "./turn-spans";
import { Badge, EmptyState, Panel } from "./ui";

// github #572 / chainlink #583: the chat's right panel shows live process
// visibility — the current turn's spans (reasoning, tool calls, results) as a
// progressive accordion sourced from the live turn-event bus. Each span fills in
// as its chunk deltas arrive; the latest span is open by default and the others
// collapse (re-openable). Spans persist after the turn ends until the next turn
// starts, so the last turn's trace stays inspectable while idle.

function spanTitle(span: TurnSpan): string {
  switch (span.type) {
    case "reasoning":
      return "Reasoning";
    case "text":
      return "Response";
    case "tool_call":
      return span.toolName || "Tool call";
    case "tool_result":
      return span.toolName ? `${span.toolName} result` : "Tool result";
    default:
      return span.type;
  }
}

// Reuse the existing type-pill color scheme (tool_call/tool_result/reasoning).
function spanTypeAttr(span: TurnSpan): string {
  return span.type;
}

function spanStateAttr(span: TurnSpan): "active" | "done" | "error" {
  if (!span.done) return "active";
  return span.status === "error" ? "error" : "done";
}

function clockTime(ts: string): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(date);
}

export function LiveActivityPanel() {
  const { spans, status } = useTurnSpans();
  const [openKey, setOpenKey] = React.useState<string | null>(null);

  // Auto-follow: whenever a new span arrives it becomes the open one (the others
  // collapse). A manual click (below) stays put until the next span arrives.
  const latestKey = spans.length ? spans[spans.length - 1].key : null;
  React.useEffect(() => {
    if (latestKey) setOpenKey(latestKey);
  }, [latestKey]);

  const toggle = (key: string) => setOpenKey((current) => (current === key ? null : key));

  const statusTone = status === "open" ? "success" : status === "error" ? "danger" : "info";

  const latest = spans.length ? spans[spans.length - 1] : null;
  const subtitle = latest && !latest.done
    ? `Working — ${spanTitle(latest)}`
    : spans.length
      ? "Idle — last turn finished"
      : "Waiting for agent activity";

  // Newest-first so the active span stays at the top, in view without scrolling.
  const ordered = spans.slice().reverse();

  return (
    <Panel
      actions={<Badge tone={statusTone}>{status}</Badge>}
      aria-label="Field log"
      className="live-activity"
      subtitle={subtitle}
      title="Field Log"
    >
      {ordered.length ? (
        <ul className="live-activity__accordion" aria-label="Recent agent activity">
          {ordered.map((span) => {
            const open = openKey === span.key;
            return (
              <li
                className="live-activity__span"
                data-state={spanStateAttr(span)}
                data-type={spanTypeAttr(span)}
                key={span.key}
              >
                <button
                  aria-expanded={open}
                  className="live-activity__span-head"
                  onClick={() => toggle(span.key)}
                  type="button"
                >
                  <span aria-hidden className="live-activity__span-marker" />
                  <span className="live-activity__span-title">{spanTitle(span)}</span>
                  <time className="live-activity__span-time">{clockTime(span.ts)}</time>
                </button>
                {open ? (
                  <div className="live-activity__span-body">
                    {span.detail ? (
                      <pre className="live-activity__span-detail">{span.detail}</pre>
                    ) : (
                      <span className="live-activity__span-pending">
                        {span.done ? "(no output)" : "…"}
                      </span>
                    )}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <EmptyState title="No recent activity" />
      )}
    </Panel>
  );
}
