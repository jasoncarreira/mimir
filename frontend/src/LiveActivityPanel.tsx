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
  const listRef = React.useRef<HTMLUListElement | null>(null);
  // Stick-to-bottom: keep the newest span (at the bottom) in view as it fills
  // in, but stop following the moment the user scrolls up to read an older one.
  const pinnedRef = React.useRef(true);

  const latest = spans.length ? spans[spans.length - 1] : null;
  const latestKey = latest?.key ?? null;

  // Auto-follow: whenever a new span arrives it becomes the open one (the others
  // collapse). A manual click (below) stays put until the next span arrives.
  React.useEffect(() => {
    if (latestKey) setOpenKey(latestKey);
  }, [latestKey]);

  // Re-pin to the bottom as spans arrive / the latest detail grows.
  const scrollSignal = `${spans.length}:${latest?.detail.length ?? 0}:${openKey ?? ""}`;
  React.useEffect(() => {
    const el = listRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [scrollSignal]);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  const toggle = (key: string) => setOpenKey((current) => (current === key ? null : key));

  const statusTone = status === "open" ? "success" : status === "error" ? "danger" : "info";

  const subtitle = latest && !latest.done
    ? `Working — ${spanTitle(latest)}`
    : spans.length
      ? "Idle — last turn finished"
      : "Waiting for agent activity";

  // Oldest → newest, so the newest span lands at the bottom (the stick-to-bottom
  // effect keeps it in view as it fills in).
  const ordered = spans;

  return (
    <Panel
      actions={<Badge tone={statusTone}>{status}</Badge>}
      aria-label="Field log"
      className="live-activity"
      subtitle={subtitle}
      title="Field Log"
    >
      {ordered.length ? (
        <ul
          aria-label="Recent agent activity"
          className="live-activity__accordion"
          onScroll={onScroll}
          ref={listRef}
        >
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
