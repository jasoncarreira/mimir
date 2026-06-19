import React from "react";
import type { TurnEventBase } from "./api/generated/contracts";
import { useLiveEvents } from "./live-events";
import { Badge, EmptyState, Panel } from "./ui";

// github #572: the chat's right panel used to duplicate message history (already
// shown in the main timeline). It now shows live process visibility instead —
// the current turn's status and a feed of recent agent activity (tool calls,
// reasoning, lifecycle), sourced from the turn.event / turn.lifecycle live
// stream. Note: useLiveEvents exposes only the latest event, so the feed is a
// best-effort rolling buffer of events observed while mounted, not a complete
// transcript (that lives in the Turns viewer).

const MAX_ITEMS = 25;

interface ActivityItem {
  id: string;
  turnId: string;
  type: string;
  label: string;
  ts: string;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function truncate(value: string, max = 90): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

// Pull a human label out of a turn event's open-ended payload (tool name,
// reasoning/message snippet, role); fall back to nothing and let the type carry.
function describeTurnEvent(event: TurnEventBase): { type: string; label: string } {
  const type = asString(event.type) ?? "event";
  const name = asString(event.name) ?? asString(event.tool) ?? asString(event.tool_name);
  const content = asString(event.content) ?? asString(event.text) ?? asString(event.summary);
  const label = name ?? (content ? truncate(content) : asString(event.role) ?? "");
  return { type, label };
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
  const { status, lastEvent } = useLiveEvents();
  const [items, setItems] = React.useState<ActivityItem[]>([]);
  const [turn, setTurn] = React.useState<{ id: string; phase: "started" | "finished" | "failed" } | null>(null);
  const seenId = React.useRef<string>("");

  React.useEffect(() => {
    if (!lastEvent || lastEvent.id === seenId.current) return;
    seenId.current = lastEvent.id;
    const event = lastEvent.event;
    const ts = lastEvent.ts ?? "";
    if (event.kind === "turn.lifecycle") {
      setTurn({ id: event.turn_id, phase: event.phase });
      setItems((current) => [
        { id: lastEvent.id, turnId: event.turn_id, type: `turn ${event.phase}`, label: event.error ?? "", ts },
        ...current
      ].slice(0, MAX_ITEMS));
    } else if (event.kind === "turn.event") {
      const { type, label } = describeTurnEvent(event.event);
      setItems((current) => [
        { id: lastEvent.id, turnId: event.turn_id, type, label, ts },
        ...current
      ].slice(0, MAX_ITEMS));
    }
    // chat.message / chat.reaction live in the conversation timeline — skip here.
  }, [lastEvent]);

  const statusTone = status === "open"
    ? "success"
    : status === "error"
      ? "danger"
      : status === "closed"
        ? "neutral"
        : "info";

  const subtitle = turn?.phase === "started"
    ? `Working on turn ${turn.id}`
    : turn?.phase === "failed"
      ? `Last turn failed (${turn.id})`
      : turn
        ? "Idle — last turn finished"
        : "Waiting for agent activity";

  return (
    <Panel
      actions={<Badge tone={statusTone}>{status}</Badge>}
      aria-label="Live activity"
      className="live-activity"
      subtitle={subtitle}
      title="Live Activity"
    >
      {items.length ? (
        <ol className="live-activity__feed" aria-label="Recent agent activity">
          {items.map((item) => (
            <li className="live-activity__item" key={item.id}>
              <span className="live-activity__type">{item.type}</span>
              {item.label ? <span className="live-activity__label">{item.label}</span> : null}
              <time className="live-activity__time">{clockTime(item.ts)}</time>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState title="No recent activity" />
      )}
    </Panel>
  );
}
