import { useQuery } from "@tanstack/react-query";
import React from "react";
import { useSearchParams } from "react-router-dom";
import { listTurns, type SagaCall, type TurnEvent } from "../api";
import {
  Badge,
  Button,
  CodeBlock,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";
import {
  buildTimeline,
  eventLabel,
  eventTone,
  filterTurns,
  formatDuration,
  formatRelativeMs,
  formatTurnTime,
  safeTurns,
  stringify,
  type SafeTurn,
  type TriggerFilter
} from "./turnsViewModel";

const PAGE_SIZE = 200;
const triggerFilters: Array<{ id: TriggerFilter; label: string }> = [
  { id: "all", label: "All" },
  { id: "user_message", label: "User" },
  { id: "scheduled_tick", label: "Heartbeat" },
  { id: "saga_session_end", label: "Synthesis" },
  { id: "poller", label: "Poller" },
  { id: "claude_code_spawn", label: "Spawn" },
  { id: "shell_job_complete", label: "Job" }
];

function useTurnPages() {
  const [turns, setTurns] = React.useState<SafeTurn[]>([]);
  const [allOlderLoaded, setAllOlderLoaded] = React.useState(false);
  const [loadError, setLoadError] = React.useState<Error | null>(null);
  const [loadingOlder, setLoadingOlder] = React.useState(false);
  const newestId = turns[0]?.turn_id;
  const oldestId = turns[turns.length - 1]?.turn_id;

  const initial = useQuery({
    queryKey: ["turns", "initial", PAGE_SIZE],
    queryFn: async () => {
      const envelope = await listTurns({ limit: PAGE_SIZE }, { cache: "no-store" });
      return envelope.data.turns;
    },
    refetchInterval: newestId ? false : 5000
  });

  React.useEffect(() => {
    if (!initial.data) return;
    const page = safeTurns(initial.data).reverse();
    setTurns(page);
    setAllOlderLoaded(page.length < PAGE_SIZE);
    setLoadError(null);
  }, [initial.data]);

  React.useEffect(() => {
    if (!newestId) return;
    const id = window.setInterval(() => {
      listTurns({ after: newestId }, { cache: "no-store" })
        .then((envelope) => {
          const fresh = safeTurns(envelope.data.turns).reverse();
          if (fresh.length) {
            setTurns((current) => [
              ...fresh.filter((turn) => !current.some((existing) => existing.turn_id === turn.turn_id)),
              ...current
            ]);
          }
          setLoadError(null);
        })
        .catch((error) => setLoadError(error instanceof Error ? error : new Error(String(error))));
    }, 5000);
    return () => window.clearInterval(id);
  }, [newestId]);

  async function loadOlder() {
    if (!oldestId || allOlderLoaded || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const envelope = await listTurns({ before: oldestId, limit: PAGE_SIZE }, { cache: "no-store" });
      const older = safeTurns(envelope.data.turns).reverse();
      setAllOlderLoaded(older.length < PAGE_SIZE);
      setTurns((current) => [
        ...current,
        ...older.filter((turn) => !current.some((existing) => existing.turn_id === turn.turn_id))
      ]);
      setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof Error ? error : new Error(String(error)));
    } finally {
      setLoadingOlder(false);
    }
  }

  return {
    turns,
    isLoading: initial.isLoading,
    isError: initial.isError,
    initialError: initial.error,
    loadError,
    loadingOlder,
    allOlderLoaded,
    loadOlder,
    refetch: initial.refetch
  };
}

function TurnBadge({ trigger, kind }: { trigger: string; kind?: string | null }) {
  return (
    <span className="turn-badges">
      <Badge tone={trigger === "unknown" ? "warning" : "neutral"}>{trigger || "unknown"}</Badge>
      {kind && kind !== trigger ? <Badge tone="info">{kind}</Badge> : null}
    </span>
  );
}

function truncate(value: string, fallback = "-") {
  const trimmed = value.trim();
  if (!trimmed) return fallback;
  return trimmed.length > 140 ? `${trimmed.slice(0, 140)}...` : trimmed;
}

function TurnList({
  turns,
  selectedId,
  onSelect,
  onLoadOlder,
  loadingOlder,
  allOlderLoaded
}: {
  turns: SafeTurn[];
  selectedId: string | null;
  onSelect: (turn: SafeTurn) => void;
  onLoadOlder: () => void;
  loadingOlder: boolean;
  allOlderLoaded: boolean;
}) {
  if (!turns.length) return <EmptyState title="No turns match the current filter" />;
  return (
    <div className="turn-list">
      <div className="turn-list__head" role="row">
        <span>Time</span>
        <span>Trigger</span>
        <span>Channel</span>
        <span>Events</span>
        <span>Duration</span>
        <span>Input</span>
        <span>Output</span>
      </div>
      <div role="list" aria-label="Turns">
        {turns.map((turn) => (
          <button
            className={`turn-row${selectedId === turn.turn_id ? " turn-row--selected" : ""}${turn.error ? " turn-row--error" : ""}`}
            key={turn.turn_id}
            onClick={() => onSelect(turn)}
            type="button"
          >
            <span className="turn-row__time">{formatTurnTime(turn.ts)}</span>
            <span><TurnBadge trigger={turn.trigger} kind={turn.kind} /></span>
            <span>{turn.channel_id ? <Badge>{turn.channel_id}</Badge> : "-"}</span>
            <span>{turn.events.length}</span>
            <span>{formatDuration(turn.duration_ms)}</span>
            <span>{truncate(turn.input)}</span>
            <span>{truncate(turn.error || turn.output)}</span>
          </button>
        ))}
      </div>
      <div className="turn-list__footer">
        {allOlderLoaded ? <span>All loaded turns are visible.</span> : (
          <Button disabled={loadingOlder} onClick={onLoadOlder}>
            {loadingOlder ? "Loading..." : "Load older"}
          </Button>
        )}
      </div>
    </div>
  );
}

function DetailsBlock({
  title,
  children,
  defaultOpen = true
}: {
  title: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details className="turn-detail-block" open={defaultOpen}>
      <summary>{title}</summary>
      <div>{children}</div>
    </details>
  );
}

function EventBody({ event }: { event: TurnEvent }) {
  if (event.type === "reasoning") return <p className="turn-prewrap">{String(event.content ?? "") || "(empty)"}</p>;
  if (event.type === "tool_call") return <CodeBlock code={stringify(event.args ?? {})} language="json" />;
  if (event.type === "tool_result") return <p className="turn-prewrap">{String(event.content ?? "") || "(empty)"}</p>;
  return <CodeBlock code={stringify(event)} language="json" />;
}

function EventCard({ event, index }: { event: TurnEvent; index: number }) {
  const tone = eventTone(event);
  const id = typeof event.id === "string" ? event.id.slice(0, 12) : "";
  return (
    <DetailsBlock
      title={
        <span className="turn-event-title">
          <Badge tone={tone === "danger" ? "danger" : tone === "success" ? "success" : tone === "feedback" ? "warning" : "info"}>
            {eventLabel(event)}
          </Badge>
          <span>#{index + 1}</span>
          {id ? <code>{id}</code> : null}
          {formatRelativeMs(event.t_ms) ? <small>{formatRelativeMs(event.t_ms)}</small> : null}
        </span>
      }
    >
      <EventBody event={event} />
    </DetailsBlock>
  );
}

function SagaCard({ call, index }: { call: SagaCall; index: number }) {
  return (
    <DetailsBlock
      title={
        <span className="turn-event-title">
          <Badge tone={call.error ? "danger" : "info"}>{call.call_type || "saga"}</Badge>
          <span>#{index + 1}</span>
          {typeof call.latency_ms === "number" ? <small>{Math.round(call.latency_ms)}ms</small> : null}
          {formatRelativeMs(call.t_ms) ? <small>{formatRelativeMs(call.t_ms)}</small> : null}
        </span>
      }
    >
      {call.error ? <ErrorState title="Saga call error">{call.error}</ErrorState> : null}
      <CodeBlock code={`args:\n${stringify(call.args ?? {})}\n\nresult:\n${stringify(call.result ?? {})}`} language="json" />
    </DetailsBlock>
  );
}

function TurnDetail({ turn }: { turn: SafeTurn }) {
  const timeline = buildTimeline(turn);
  const separateSaga = turn.saga_calls.length > 0 && !timeline.some((entry) => entry.kind === "saga");

  return (
    <aside className="turn-detail" aria-label="Selected turn detail">
      <Panel
        title="Selected Turn"
        subtitle={turn.turn_id}
        actions={<TurnBadge trigger={turn.trigger} kind={turn.kind} />}
      >
        <dl className="facts-grid facts-grid--compact">
          <div><dt>Time</dt><dd>{formatTurnTime(turn.ts)}</dd></div>
          <div><dt>Channel</dt><dd>{turn.channel_id || "-"}</dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(turn.duration_ms)}</dd></div>
        </dl>
      </Panel>

      <DetailsBlock title="Input">
        <p className="turn-prewrap">{turn.input || "(empty)"}</p>
      </DetailsBlock>

      {turn.error ? (
        <DetailsBlock title="Error">
          <ErrorState title="Turn error">{turn.error}</ErrorState>
        </DetailsBlock>
      ) : null}

      {separateSaga ? (
        <DetailsBlock title={`Saga calls (${turn.saga_calls.length})`}>
          <div className="turn-event-stack">
            {turn.saga_calls.map((call, index) => <SagaCard call={call} index={index} key={index} />)}
          </div>
        </DetailsBlock>
      ) : null}

      <DetailsBlock title={`Events (${turn.events.length}${turn.saga_calls.length && !separateSaga ? `, ${turn.saga_calls.length} saga` : ""})`}>
        {timeline.length ? (
          <div className="turn-event-stack">
            {timeline.map((entry) => {
              if (entry.kind === "event") return <EventCard event={entry.item} index={entry.index} key={`event-${entry.index}`} />;
              if (entry.kind === "saga") return <SagaCard call={entry.item} index={entry.index} key={`saga-${entry.index}`} />;
              return (
                <DetailsBlock
                  title={<span className="turn-event-title"><Badge tone="warning">Mid-turn input</Badge>{formatRelativeMs(entry.item.t_ms) ? <small>{formatRelativeMs(entry.item.t_ms)}</small> : null}</span>}
                  key={`injected-${entry.index}`}
                >
                  <p className="turn-prewrap">{entry.item.text}</p>
                </DetailsBlock>
              );
            })}
          </div>
        ) : (
          <EmptyState title="No events recorded for this turn" />
        )}
      </DetailsBlock>

      {Object.keys(turn.metadata).length ? (
        <DetailsBlock title="Metadata" defaultOpen={false}>
          <CodeBlock code={stringify(turn.metadata)} language="json" />
        </DetailsBlock>
      ) : null}

      <DetailsBlock title="Output">
        <p className="turn-prewrap">{turn.output || "(empty)"}</p>
      </DetailsBlock>
    </aside>
  );
}

export function TurnsRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [trigger, setTrigger] = React.useState<TriggerFilter>("all");
  const [hidePollers, setHidePollers] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const { turns, isLoading, isError, initialError, loadError, loadingOlder, allOlderLoaded, loadOlder, refetch } = useTurnPages();
  const selectedId = searchParams.get("turn");
  const visibleTurns = React.useMemo(
    () => filterTurns(turns, { trigger, hidePollers, query }),
    [turns, trigger, hidePollers, query]
  );
  const selectedTurn = visibleTurns.find((turn) => turn.turn_id === selectedId) ?? visibleTurns[0] ?? null;

  React.useEffect(() => {
    if (!selectedTurn || selectedId === selectedTurn.turn_id) return;
    const next = new URLSearchParams(searchParams);
    next.set("turn", selectedTurn.turn_id);
    setSearchParams(next, { replace: true });
  }, [searchParams, selectedId, selectedTurn, setSearchParams]);

  function selectTurn(turn: SafeTurn) {
    const next = new URLSearchParams(searchParams);
    next.set("turn", turn.turn_id);
    setSearchParams(next);
  }

  return (
    <div className="turns-route">
      <div className="turns-header-row">
        <div>
          <p className="ui-eyebrow">Turn Viewer</p>
          <h1>Turns</h1>
          <p>{visibleTurns.length === turns.length ? `${turns.length} loaded` : `${visibleTurns.length} / ${turns.length} loaded`}</p>
        </div>
        <div className="turns-header-actions">
          <Badge tone={loadError || isError ? "danger" : "success"}>{loadError || isError ? "stale" : "live"}</Badge>
          <Button onClick={() => refetch()}>Refresh</Button>
        </div>
      </div>

      <Panel title="Browse Turns" subtitle={allOlderLoaded ? "Newest loaded turn records." : "Newest records loaded first. Load older pages as needed."}>
        <div className="turns-controls">
          <div className="turn-filter-tabs" role="tablist" aria-label="Trigger filter">
            {triggerFilters.map((filter) => (
              <button
                aria-selected={trigger === filter.id}
                className="turn-filter-tab"
                key={filter.id}
                onClick={() => setTrigger(filter.id)}
                role="tab"
                type="button"
              >
                {filter.label}
              </button>
            ))}
          </div>
          <label className="turn-checkbox">
            <input checked={hidePollers} onChange={(event) => setHidePollers(event.currentTarget.checked)} type="checkbox" />
            <span>Hide pollers</span>
          </label>
          <TextInput
            aria-label="Search input, output, and injected messages"
            onChange={(event) => setQuery(event.currentTarget.value)}
            placeholder="Search input / output"
            value={query}
          />
        </div>
        {isLoading ? <LoadingState label="Loading turns" /> : null}
        {isError ? (
          <ErrorState title="Could not load turns">
            {initialError instanceof Error ? initialError.message : String(initialError)}
          </ErrorState>
        ) : null}
        {loadError ? <ErrorState title="Turn refresh failed">{loadError.message}</ErrorState> : null}
        {!isLoading && !isError ? (
          <TurnList
            allOlderLoaded={allOlderLoaded}
            loadingOlder={loadingOlder}
            onLoadOlder={loadOlder}
            onSelect={selectTurn}
            selectedId={selectedTurn?.turn_id ?? null}
            turns={visibleTurns}
          />
        ) : null}
      </Panel>

      {selectedTurn ? <TurnDetail turn={selectedTurn} /> : (
        <Panel title="Selected Turn">
          <EmptyState title={turns.length ? "No turn selected" : "No turns recorded yet"} />
        </Panel>
      )}
    </div>
  );
}
