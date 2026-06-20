import React from "react";
import { Link } from "react-router-dom";
import type { SagaCall, TurnEvent, TurnRecord } from "./api";
import { drilldownHref } from "./routeState";
import { Badge, CodeBlock, EmptyState, ErrorState, Panel } from "./ui";
import { TriggerPill } from "./routes/triggerPill";
import { useUiState } from "./uiState";
import {
  eventLabel,
  eventTone,
  formatDuration,
  formatRelativeMs,
  formatTurnTime,
  safeTurn,
  stringify,
  type SafeTurn
} from "./routes/turnsViewModel";

type SectionKey =
  | "messages"
  | "timeline"
  | "feedback"
  | "related-context"
  | "events"
  | "metadata";

function toneForEvent(event: TurnEvent): "neutral" | "info" | "success" | "warning" | "danger" {
  const tone = eventTone(event);
  if (tone === "danger") return "danger";
  if (tone === "success") return "success";
  if (tone === "feedback") return "warning";
  return tone === "neutral" ? "neutral" : "info";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return stringify(value);
}

function eventName(event: TurnEvent): string {
  return typeof event.name === "string" && event.name ? event.name : eventLabel(event);
}

function isFeedbackEvent(event: TurnEvent): boolean {
  const type = event.type.toLowerCase();
  return type.includes("feedback") || type.includes("algedonic") || type.includes("reaction");
}

function isRelatedContextKey(key: string): boolean {
  const normalized = key.toLowerCase();
  return normalized.includes("context") || normalized.includes("memory") || normalized.includes("related");
}

function placeholderLink(event: TurnEvent): string {
  for (const key of ["href", "url", "link", "path", "artifact_url", "offload_url"]) {
    const value = event[key];
    if (typeof value === "string" && value) return value;
  }
  const source = isRecord(event.result) ? event.result : isRecord(event.content) ? event.content : null;
  if (source) {
    for (const key of ["href", "url", "link", "path", "artifact_url", "offload_url"]) {
      const value = source[key];
      if (typeof value === "string" && value) return value;
    }
  }
  return "";
}

function resultPlaceholder(event: TurnEvent): string {
  const flags = [
    event.redacted,
    event.is_redacted,
    event.offloaded,
    event.is_offloaded,
    event.missing,
    event.truncated
  ];
  const content = event.content;
  const result = event.result;
  if (flags.some(Boolean)) {
    if (event.redacted || event.is_redacted) return "Result redacted.";
    if (event.offloaded || event.is_offloaded) return "Result offloaded.";
    if (event.missing) return "Result missing.";
    return "Result truncated.";
  }
  if (content === undefined && result === undefined) return "Result content missing.";
  if (typeof content === "string" && content.length > 12000) return "Result is large; showing preview.";
  return "";
}

function stringField(source: unknown, keys: string[]): string {
  if (!isRecord(source)) return "";
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function sagaAtomId(call: SagaCall): string {
  return (
    stringField(call.result, ["atom_id", "atom", "id", "target_id"])
    || stringField(call.args, ["atom_id", "atom", "id", "target_id"])
  );
}

function PreviewText({ value, empty = "(empty)" }: { value: unknown; empty?: string }) {
  const text = asText(value);
  const display = text.length > 12000 ? `${text.slice(0, 12000)}\n\n[truncated preview]` : text;
  return <p className="turn-prewrap">{display || empty}</p>;
}

function ResultBody({ event }: { event: TurnEvent }) {
  const placeholder = resultPlaceholder(event);
  const link = placeholderLink(event);
  const value = event.content ?? event.result;

  return (
    <div className="turn-result-body">
      {placeholder ? (
        <p className="turn-placeholder">
          {placeholder}
          {link ? <> <a href={link}>{link}</a></> : null}
        </p>
      ) : null}
      {value !== undefined ? <PreviewText value={value} /> : null}
      {event.error ? <ErrorState title="Tool result error">{String(event.error)}</ErrorState> : null}
      {value === undefined && !placeholder ? <EmptyState title="No result payload" /> : null}
    </div>
  );
}

function EventBody({ event }: { event: TurnEvent }) {
  if (event.type === "reasoning") return <PreviewText value={event.content} />;
  if (event.type === "tool_call") return <CodeBlock code={stringify(event.args ?? {})} language="json" />;
  if (event.type === "tool_result") return <ResultBody event={event} />;
  if (isFeedbackEvent(event)) return <CodeBlock code={stringify(event)} language="json" />;
  return <CodeBlock code={stringify(event)} language="json" />;
}

function EventCard({ event, index }: { event: TurnEvent; index: number }) {
  const id = typeof event.id === "string" ? event.id.slice(0, 12) : "";
  const tone = eventTone(event);
  return (
    <div className={`turn-event-card turn-event-card--${tone}`} data-event-tone={tone}>
      <div className="turn-event-title">
        <Badge tone={toneForEvent(event)}>{eventLabel(event)}</Badge>
        <span>#{index + 1}</span>
        {event.type === "tool_call" || event.type === "tool_result" ? <strong>{eventName(event)}</strong> : null}
        {id ? <code>{id}</code> : null}
        {formatRelativeMs(event.t_ms) ? <small>{formatRelativeMs(event.t_ms)}</small> : null}
      </div>
      <EventBody event={event} />
    </div>
  );
}

function SagaCard({ call, index, turnId }: { call: SagaCall; index: number; turnId: string }) {
  const atomId = sagaAtomId(call);
  return (
    <div className="turn-event-card">
      <div className="turn-event-title">
        <Badge tone={call.error ? "danger" : "info"}>{call.call_type || "saga"}</Badge>
        <span>#{index + 1}</span>
        {typeof call.latency_ms === "number" ? <small>{Math.round(call.latency_ms)}ms</small> : null}
        {formatRelativeMs(call.t_ms) ? <small>{formatRelativeMs(call.t_ms)}</small> : null}
        <Link to={drilldownHref("/saga", {
          tab: atomId ? "atoms" : "search",
          turn: turnId,
          atom: atomId || undefined,
          target: `saga-call-${index + 1}`
        })}>
          SAGA
        </Link>
      </div>
      {call.error ? <ErrorState title="Saga call error">{call.error}</ErrorState> : null}
      <CodeBlock code={`args:\n${stringify(call.args ?? {})}\n\nresult:\n${stringify(call.result ?? {})}`} language="json" />
    </div>
  );
}

function CollapsibleSection({
  routeKey,
  turnId,
  section,
  title,
  count,
  defaultOpen = true,
  children
}: {
  routeKey: string;
  turnId: string;
  section: SectionKey;
  title: React.ReactNode;
  count?: number;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const id = `${routeKey}:${turnId}:${section}`;
  const stored = useUiState((state) => state.collapsedRegions[id]);
  const setCollapsed = useUiState((state) => state.setCollapsedRegion);
  const collapsed = stored ?? !defaultOpen;
  const contentId = `${id.replace(/[^a-zA-Z0-9_-]/g, "-")}-content`;

  return (
    <section className="turn-detail-block">
      <button
        aria-controls={contentId}
        aria-expanded={!collapsed}
        className="turn-detail-block__button"
        onClick={() => setCollapsed(id, !collapsed)}
        type="button"
      >
        <span>{title}</span>
        {typeof count === "number" ? <Badge>{count}</Badge> : null}
        <span aria-hidden="true">{collapsed ? "+" : "-"}</span>
      </button>
      <div hidden={collapsed} id={contentId}>
        {children}
      </div>
    </section>
  );
}

function MessageBlocks({ turn }: { turn: SafeTurn }) {
  return (
    <div className="turn-message-stack">
      {turn.input ? (
        <div>
          <h3>Input</h3>
          <PreviewText value={turn.input} />
        </div>
      ) : null}
      {turn.output ? (
        <div>
          <h3>Output</h3>
          <PreviewText value={turn.output} />
        </div>
      ) : null}
      {turn.error ? <ErrorState title="Turn error">{turn.error}</ErrorState> : null}
      {turn.injected_inputs.map((item, index) => (
        <div key={index}>
          <h3>Injected input {formatRelativeMs(item.t_ms) ? <small>{formatRelativeMs(item.t_ms)}</small> : null}</h3>
          <PreviewText value={item.text} />
        </div>
      ))}
    </div>
  );
}

function relatedContextEntries(turn: SafeTurn): Array<[string, unknown]> {
  return Object.entries(turn.metadata).filter(([key]) => isRelatedContextKey(key));
}

export function TurnDetailsPanel({
  turn,
  routeKey,
  emptyTitle = "No turn selected"
}: {
  turn?: TurnRecord | null;
  routeKey: string;
  emptyTitle?: React.ReactNode;
}) {
  if (!turn) {
    return (
      <Panel title="Selected Turn">
        <EmptyState title={emptyTitle} />
      </Panel>
    );
  }

  const normalized = safeTurn(turn);
  // `events` is recorded in canonical emission order; render that order directly
  // so the Timeline mirrors what happened instead of re-sorting by optional t_ms.
  const timelineEvents = normalized.events.filter((event) => (
    event.type === "reasoning" || event.type === "tool_call" || event.type === "tool_result"
  ));
  const feedback = normalized.events.filter(isFeedbackEvent);
  const known = new Set([...timelineEvents, ...feedback]);
  const otherEvents = normalized.events.filter((event) => !known.has(event));
  const contextEntries = relatedContextEntries(normalized);
  const turnId = normalized.turn_id;

  return (
    <aside className="turn-detail" aria-label="Selected turn detail">
      <Panel
        title="Selected Turn"
        subtitle={turnId}
        actions={<TriggerPill trigger={normalized.trigger} />}
      >
        <dl className="facts-grid facts-grid--compact">
          <div><dt>Time</dt><dd>{formatTurnTime(normalized.ts)}</dd></div>
          <div><dt>Channel</dt><dd>{normalized.channel_id || "-"}</dd></div>
          <div><dt>Kind</dt><dd>{normalized.kind || "-"}</dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(normalized.duration_ms)}</dd></div>
          <div><dt>Events</dt><dd>{normalized.events.length}</dd></div>
          <div><dt>Saga</dt><dd>{normalized.saga_calls.length}</dd></div>
        </dl>
      </Panel>

      {(normalized.input || normalized.output || normalized.error || normalized.injected_inputs.length) ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="messages" title="Messages">
          <MessageBlocks turn={normalized} />
        </CollapsibleSection>
      ) : null}

      {timelineEvents.length ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="timeline" title="Timeline" count={timelineEvents.length}>
          <div className="turn-event-stack">
            {timelineEvents.map((event, index) => <EventCard event={event} index={index} key={`${event.type}-${event.id ?? "event"}-${index}`} />)}
          </div>
        </CollapsibleSection>
      ) : null}

      {feedback.length ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="feedback" title="Feedback" count={feedback.length}>
          <div className="turn-event-stack">
            {feedback.map((event, index) => <EventCard event={event} index={index} key={index} />)}
          </div>
        </CollapsibleSection>
      ) : null}

      {(normalized.saga_calls.length || contextEntries.length || normalized.injected_inputs.length) ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="related-context" title="Related context" count={normalized.saga_calls.length + contextEntries.length + normalized.injected_inputs.length} defaultOpen={false}>
          <div className="turn-event-stack">
            {normalized.saga_calls.map((call, index) => <SagaCard call={call} index={index} key={`saga-${index}`} turnId={turnId} />)}
            {contextEntries.map(([key, value]) => (
              <CodeBlock code={stringify(value)} key={key} language="json" title={key} />
            ))}
            {normalized.injected_inputs.map((item, index) => (
              <div className="turn-event-card" key={`injected-${index}`}>
                <div className="turn-event-title">
                  <Badge tone="warning">Mid-turn input</Badge>
                  {formatRelativeMs(item.t_ms) ? <small>{formatRelativeMs(item.t_ms)}</small> : null}
                </div>
                <PreviewText value={item.text} />
              </div>
            ))}
          </div>
        </CollapsibleSection>
      ) : null}

      {otherEvents.length ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="events" title="Other events" count={otherEvents.length} defaultOpen={false}>
          <div className="turn-event-stack">
            {otherEvents.map((event, index) => <EventCard event={event} index={index} key={index} />)}
          </div>
        </CollapsibleSection>
      ) : null}

      {Object.keys(normalized.metadata).length ? (
        <CollapsibleSection routeKey={routeKey} turnId={turnId} section="metadata" title="Metadata" count={Object.keys(normalized.metadata).length} defaultOpen={false}>
          <CodeBlock code={stringify(normalized.metadata)} language="json" />
        </CollapsibleSection>
      ) : null}

      {!timelineEvents.length && !normalized.input && !normalized.output ? <EmptyState title="No detail blocks recorded for this turn" /> : null}
    </aside>
  );
}
