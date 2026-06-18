import { useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { listSessions, listTurns, type TurnRecord } from "../api";
import type { ConversationSession } from "../api/generated/contracts";
import { drilldownHref } from "../routeState";
import { TurnDetailsPanel } from "../TurnDetailsPanel";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  TextInput
} from "../ui";
import {
  filterTurns,
  formatDuration,
  formatTurnTime,
  safeTurns,
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

const SESSION_PAGE_SIZE = 200;

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

function isoDateOnly(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 10);
}

function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(value);
  }
  const node = document.createElement("textarea");
  node.value = value;
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.appendChild(node);
  node.select();
  document.execCommand("copy");
  document.body.removeChild(node);
  return Promise.resolve();
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

function SessionList({
  sessions,
  selectedId,
  onSelect
}: {
  sessions: ConversationSession[];
  selectedId: string | null;
  onSelect: (session: ConversationSession) => void;
}) {
  if (!sessions.length) return <EmptyState title="No sessions match the current filters" />;
  return (
    <div className="session-list" role="list" aria-label="Sessions">
      {sessions.map((session) => (
        <button
          className={`session-row${selectedId === session.id ? " session-row--selected" : ""}`}
          key={session.id}
          onClick={() => onSelect(session)}
          type="button"
        >
          <span className="session-row__main">
            <strong>{session.channel_id || "unknown channel"}</strong>
            <small>{formatTurnTime(session.last_activity_at || session.ended_at || session.started_at || "")}</small>
            <span>{truncate(session.summary || session.messages[0]?.content_snippet || session.turns[0]?.input_snippet || session.id, "No summary")}</span>
          </span>
          <span className="turn-badges">
            {session.triggers.slice(0, 3).map((trigger) => <Badge key={trigger}>{trigger}</Badge>)}
            {session.synthetic ? <Badge tone="warning">inferred</Badge> : null}
          </span>
          <span>{session.turn_count} turns</span>
          <span>{session.message_count} messages</span>
        </button>
      ))}
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

function SessionDetail({
  session,
  onOpenTurn
}: {
  session: ConversationSession;
  onOpenTurn: (turnId: string) => void;
}) {
  const [copyStatus, setCopyStatus] = React.useState("");

  function sessionUrl() {
    const url = new URL(window.location.href);
    url.searchParams.set("session", session.id);
    if (session.turn_ids[0]) url.searchParams.set("turn", session.turn_ids[0]);
    return url.toString();
  }

  async function copyLink() {
    await copyText(sessionUrl());
    setCopyStatus("link copied");
  }

  async function exportSession() {
    await copyText(JSON.stringify(session, null, 2));
    setCopyStatus("json copied");
  }

  return (
    <div className="turn-detail session-detail" aria-label="Selected session detail">
      <Panel
        title="Selected Session"
        subtitle={session.id}
        actions={
          <>
            <Button onClick={copyLink}>Copy link</Button>
            <Button onClick={exportSession}>Export</Button>
          </>
        }
      >
        <dl className="facts-grid facts-grid--compact">
          <div><dt>Channel</dt><dd>{session.channel_id || "-"}</dd></div>
          <div><dt>Started</dt><dd>{formatTurnTime(session.started_at || "")}</dd></div>
          <div><dt>Last activity</dt><dd>{formatTurnTime(session.last_activity_at || session.ended_at || "")}</dd></div>
          <div><dt>Counts</dt><dd>{session.turn_count} turns / {session.message_count} messages</dd></div>
        </dl>
        {copyStatus ? <p className="session-copy-status">{copyStatus}</p> : null}
      </Panel>

      <DetailsBlock title="Summary">
        <p className="turn-prewrap">{session.summary || "(no summary recorded)"}</p>
      </DetailsBlock>

      <DetailsBlock title={`Unfinished (${session.unfinished.length})`}>
        {session.unfinished.length ? (
          <ul className="session-plain-list">
            {session.unfinished.map((item, index) => <li key={index}>{String(item)}</li>)}
          </ul>
        ) : <EmptyState title="No unfinished items recorded" />}
      </DetailsBlock>

      <DetailsBlock title={`Messages (${session.messages.length})`}>
        {session.messages.length ? (
          <div className="session-message-stack">
            {session.messages.map((message, index) => (
              <div className="session-message" key={`${message.msg_id || message.ts}-${index}`}>
                <small>{formatTurnTime(message.ts)} · {message.kind} · {message.author || "unknown"}</small>
                <p>{message.content}</p>
              </div>
            ))}
          </div>
        ) : <EmptyState title="No chat history messages matched this session" />}
      </DetailsBlock>

      <DetailsBlock title={`Turns (${session.turns.length})`}>
        {session.turns.length ? (
          <div className="session-turn-stack">
            {session.turns.map((turn) => (
              <button className="session-turn-link" key={turn.turn_id} onClick={() => onOpenTurn(turn.turn_id)} type="button">
                <span><Badge>{turn.trigger}</Badge> <code>{turn.turn_id}</code></span>
                <small>{formatTurnTime(turn.ts)}</small>
                <span>{turn.input_snippet || turn.output_snippet || "(empty)"}</span>
              </button>
            ))}
          </div>
        ) : <EmptyState title="No turn records matched this session" />}
      </DetailsBlock>

      <DetailsBlock title={`Related SAGA atoms (${session.related_saga_atoms.length})`}>
        {session.related_saga_atoms.length ? (
          <div className="session-message-stack">
            {session.related_saga_atoms.map((atom) => (
              <div className="session-message" key={atom.id}>
                <small>{atom.memory_type || "atom"} · <code>{atom.id}</code></small>
                <p>{atom.content_preview}</p>
                <Link to={drilldownHref("/saga", { tab: "atoms", atom: atom.id, session: session.id })}>Open atom</Link>
              </div>
            ))}
          </div>
        ) : <EmptyState title="No related SAGA atoms available" />}
      </DetailsBlock>
    </div>
  );
}

export function TurnsRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [trigger, setTrigger] = React.useState<TriggerFilter>((searchParams.get("trigger") as TriggerFilter) || "all");
  const [hidePollers, setHidePollers] = React.useState(false);
  const [query, setQuery] = React.useState(searchParams.get("q") || searchParams.get("filter") || "");
  const [sessionChannel, setSessionChannel] = React.useState(searchParams.get("channel") || "");
  const [sessionTrigger, setSessionTrigger] = React.useState(searchParams.get("trigger") || "");
  const [sessionFrom, setSessionFrom] = React.useState(searchParams.get("from") || "");
  const [sessionTo, setSessionTo] = React.useState(searchParams.get("to") || "");
  const [sessionQuery, setSessionQuery] = React.useState(searchParams.get("q") || searchParams.get("filter") || "");
  const { turns, isLoading, isError, initialError, loadError, loadingOlder, allOlderLoaded, loadOlder, refetch } = useTurnPages();
  const selectedId = searchParams.get("turn");
  const selectedSessionId = searchParams.get("session");
  const channelParam = searchParams.get("channel") || "";
  const eventParam = searchParams.get("event") || "";
  const sessionsQuery = useQuery({
    queryKey: ["sessions", SESSION_PAGE_SIZE, sessionChannel, sessionTrigger, sessionFrom, sessionTo, sessionQuery],
    queryFn: async () => {
      const envelope = await listSessions({
        limit: SESSION_PAGE_SIZE,
        q: sessionQuery,
        channel: sessionChannel,
        trigger: sessionTrigger,
        from: sessionFrom,
        to: sessionTo
      }, { cache: "no-store" });
      return envelope.data;
    },
    refetchInterval: 10000
  });
  const visibleTurns = React.useMemo(
    () => filterTurns(turns, { trigger, hidePollers, query }).filter((turn) => (
      (!channelParam || turn.channel_id === channelParam)
      && (!eventParam || turn.events.some((event) => event.type === eventParam || event.name === eventParam))
    )),
    [turns, trigger, hidePollers, query, channelParam, eventParam]
  );
  const selectedTurn = selectedId
    ? turns.find((turn) => turn.turn_id === selectedId) ?? null
    : visibleTurns[0] ?? null;
  const selectedTurnQuery = useQuery<TurnRecord | null>({
    queryKey: ["turn", selectedTurn?.turn_id ?? ""],
    queryFn: async () => selectedTurn,
    enabled: false,
    initialData: selectedTurn
  });
  const detailTurn = selectedTurnQuery.data ?? selectedTurn;
  const sessions = sessionsQuery.data?.sessions ?? [];
  const selectedSession = selectedSessionId
    ? sessions.find((session) => session.id === selectedSessionId) ?? null
    : sessions[0] ?? null;

  React.useEffect(() => {
    if (selectedId || !selectedTurn) return;
    const next = new URLSearchParams(searchParams);
    next.set("turn", selectedTurn.turn_id);
    setSearchParams(next, { replace: true });
  }, [searchParams, selectedId, selectedTurn, setSearchParams]);

  React.useEffect(() => {
    setQuery(searchParams.get("q") || searchParams.get("filter") || "");
    const nextTrigger = (searchParams.get("trigger") as TriggerFilter) || "all";
    setTrigger(triggerFilters.some((item) => item.id === nextTrigger) ? nextTrigger : "all");
    setSessionChannel(searchParams.get("channel") || "");
    setSessionTrigger(searchParams.get("trigger") || "");
    setSessionFrom(searchParams.get("from") || "");
    setSessionTo(searchParams.get("to") || "");
    setSessionQuery(searchParams.get("q") || searchParams.get("filter") || "");
  }, [searchParams]);

  React.useEffect(() => {
    if (!selectedTurn) return;
    queryClient.setQueryData(["turn", selectedTurn.turn_id], selectedTurn);
  }, [queryClient, selectedTurn]);

  function selectTurn(turn: SafeTurn) {
    const next = new URLSearchParams(searchParams);
    next.set("turn", turn.turn_id);
    setSearchParams(next);
  }

  function selectSession(session: ConversationSession) {
    const next = new URLSearchParams(searchParams);
    next.set("session", session.id);
    if (session.turn_ids[0]) next.set("turn", session.turn_ids[0]);
    setSearchParams(next);
  }

  function openSessionTurn(turnId: string) {
    const next = new URLSearchParams(searchParams);
    next.set("turn", turnId);
    setSearchParams(next);
    const node = document.getElementById("turn-browser-panel");
    node?.scrollIntoView({ block: "start" });
  }

  function setRouteValue(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
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

      <div className="content-layout turns-layout">
        <section aria-label="Turn browser" className="content-layout__main">
          <Panel title="Browse Turns" subtitle={allOlderLoaded ? "Newest loaded turn records." : "Newest records loaded first. Load older pages as needed."}>
            <div className="turns-controls">
              <div className="turn-filter-tabs" role="tablist" aria-label="Trigger filter">
                {triggerFilters.map((filter) => (
                  <button
                    aria-selected={trigger === filter.id}
                    className="turn-filter-tab"
                    key={filter.id}
                    onClick={() => {
                      setTrigger(filter.id);
                      setRouteValue("trigger", filter.id === "all" ? "" : filter.id);
                    }}
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
            {selectedId && !selectedTurn && !isLoading && !isError ? (
              <ErrorState title="Turn not found">No loaded turn matches {selectedId}. Load older turns or adjust the URL filters.</ErrorState>
            ) : null}
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

          <Panel
            title="Browse Sessions"
            subtitle="Conversation-level grouping over turns, chat history, and SAGA summaries."
          >
            <div className="turns-controls turns-controls--sessions">
              <TextInput
                aria-label="Search session messages and outputs"
                onChange={(event) => setSessionQuery(event.currentTarget.value)}
                placeholder="Search messages / output"
                value={sessionQuery}
              />
              <select
                aria-label="Filter sessions by channel"
                className="ui-input"
                onChange={(event) => {
                  setSessionChannel(event.currentTarget.value);
                  setRouteValue("channel", event.currentTarget.value);
                }}
                value={sessionChannel}
              >
                <option value="">All channels</option>
                {(sessionsQuery.data?.channels ?? []).map((channel) => <option key={channel} value={channel}>{channel}</option>)}
              </select>
              <select
                aria-label="Filter sessions by trigger"
                className="ui-input"
                onChange={(event) => {
                  setSessionTrigger(event.currentTarget.value);
                  setRouteValue("trigger", event.currentTarget.value);
                }}
                value={sessionTrigger}
              >
                <option value="">All triggers</option>
                {(sessionsQuery.data?.triggers ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <TextInput
                aria-label="From date"
                onChange={(event) => {
                  setSessionFrom(event.currentTarget.value);
                  setRouteValue("from", event.currentTarget.value);
                }}
                type="date"
                value={isoDateOnly(sessionFrom)}
              />
              <TextInput
                aria-label="To date"
                onChange={(event) => {
                  setSessionTo(event.currentTarget.value);
                  setRouteValue("to", event.currentTarget.value);
                }}
                type="date"
                value={isoDateOnly(sessionTo)}
              />
            </div>
            {sessionsQuery.isLoading ? <LoadingState label="Loading sessions" /> : null}
            {sessionsQuery.isError ? (
              <ErrorState title="Could not load sessions">
                {sessionsQuery.error instanceof Error ? sessionsQuery.error.message : String(sessionsQuery.error)}
              </ErrorState>
            ) : null}
            {!sessionsQuery.isLoading && !sessionsQuery.isError ? (
              selectedSessionId && !selectedSession ? (
                <ErrorState title="Session not found">No loaded session matches {selectedSessionId}. Adjust filters or the time range.</ErrorState>
              ) : (
                <SessionList sessions={sessions} selectedId={selectedSession?.id ?? null} onSelect={selectSession} />
              )
            ) : null}
          </Panel>
        </section>
        <aside aria-label="Details panel" className="content-layout__details" id="details-panel-host">
          {selectedSession ? <SessionDetail session={selectedSession} onOpenTurn={openSessionTurn} /> : null}
          <div id="turn-browser-panel" />
          <TurnDetailsPanel
            emptyTitle={turns.length ? "No turn selected" : "No turns recorded yet"}
            routeKey="turns"
            turn={detailTurn}
          />
        </aside>
      </div>
    </div>
  );
}
