import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { chainlinkBoardHref, getChainlinkBoard, type ChainlinkBoardIssue } from "../api";
import { drilldownHref, sanitizeHref } from "../routeState";
import {
  Badge,
  Button,
  CodeBlock,
  Drawer,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel
} from "../ui";
import {
  formatBoardTime,
  partitionDependencies,
  safeChainlinkBoardData,
  type ChainlinkBoardFilters
} from "./chainlinkBoardViewModel";

const statusTone: Record<string, React.ComponentProps<typeof Badge>["tone"]> = {
  open: "neutral",
  ready: "info",
  blocked: "danger",
  "in-progress": "warning",
  review: "warning",
  done: "success"
};

const priorityTone: Record<string, React.ComponentProps<typeof Badge>["tone"]> = {
  high: "danger",
  medium: "warning",
  low: "neutral",
  normal: "neutral"
};

function SelectFilter({
  label,
  value,
  options,
  onChange
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="chainlink-filter">
      <span>{label}</span>
      <select className="ui-input" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">All</option>
        {options.map((option) => (
          <option key={option} value={option}>{option}</option>
        ))}
      </select>
    </label>
  );
}

function IssueCard({
  issue,
  onOpen
}: {
  issue: ChainlinkBoardIssue;
  onOpen: (issue: ChainlinkBoardIssue) => void;
}) {
  const blockers = issue.blocked_by.length;
  const progress = issue.child_progress.total
    ? `${issue.child_progress.done}/${issue.child_progress.total}`
    : "";
  return (
    <button className="chainlink-card" onClick={() => onOpen(issue)} type="button">
      <span className="chainlink-card__title">#{issue.id} {issue.title}</span>
      <span className="chainlink-card__badges">
        <Badge tone={priorityTone[issue.priority] ?? "neutral"}>{issue.priority}</Badge>
        {issue.worklink ? <Badge tone={statusTone[issue.worklink.status] ?? "info"}>attempt {issue.worklink.attempt}</Badge> : null}
        {blockers ? <Badge tone="danger">{blockers} blockers</Badge> : null}
      </span>
      {issue.labels.length ? (
        <span className="chainlink-labels">
          {issue.labels.slice(0, 4).map((label) => <span key={label}>{label}</span>)}
        </span>
      ) : null}
      <span className="chainlink-card__meta">
        {progress ? <span>{progress} subissues</span> : <span>{issue.child_ids.length ? `${issue.child_ids.length} subissues` : "leaf"}</span>}
        <span>{formatBoardTime(issue.updated_at) || "not updated"}</span>
      </span>
    </button>
  );
}

function IssueLinks({
  title,
  ids,
  byId
}: {
  title: string;
  ids: number[];
  byId: Map<number, ChainlinkBoardIssue>;
}) {
  return (
    <div className="chainlink-drawer-section">
      <h3>{title}</h3>
      {ids.length ? (
        <ul className="chainlink-link-list">
          {ids.map((id) => {
            const issue = byId.get(id);
            return <li key={id}>#{id}{issue ? ` ${issue.title}` : ""}</li>;
          })}
        </ul>
      ) : (
        <p className="app-copy">None</p>
      )}
    </div>
  );
}

function TreeNode({
  issue,
  byId,
  depth = 0
}: {
  issue: ChainlinkBoardIssue;
  byId: Map<number, ChainlinkBoardIssue>;
  depth?: number;
}) {
  const children = issue.child_ids
    .map((id) => byId.get(id))
    .filter((child): child is ChainlinkBoardIssue => Boolean(child));
  return (
    <li>
      <div className="chainlink-tree-row" style={{ "--tree-depth": depth } as React.CSSProperties}>
        <span>#{issue.id} {issue.title}</span>
        <Badge tone={statusTone[issue.status] ?? "neutral"}>{issue.status}</Badge>
        {issue.child_progress.total ? (
          <small>{issue.child_progress.done}/{issue.child_progress.total}</small>
        ) : null}
      </div>
      {children.length ? (
        <ol>
          {children.map((child) => <TreeNode byId={byId} depth={depth + 1} issue={child} key={child.id} />)}
        </ol>
      ) : null}
    </li>
  );
}

export function WorklinkPanel({ issue }: { issue: ChainlinkBoardIssue }) {
  const worklink = issue.worklink;
  if (!worklink) {
    return (
      <div className="chainlink-drawer-section">
        <h3>Worklink</h3>
        <p className="app-copy">No Worklink evidence found for this issue.</p>
      </div>
    );
  }
  const evidenceHref = sanitizeHref(worklink.evidence_href);
  const transcriptHref = sanitizeHref(worklink.transcript_href);
  const prHref = sanitizeHref(worklink.pr_url);
  return (
    <div className="chainlink-drawer-section">
      <h3>Worklink</h3>
      <dl className="facts-grid facts-grid--compact">
        <div><dt>Status</dt><dd>{worklink.status}</dd></div>
        <div><dt>Attempt</dt><dd>{worklink.attempt}</dd></div>
        <div><dt>Backend</dt><dd>{worklink.backend || "unknown"}</dd></div>
        <div><dt>Branch</dt><dd>{worklink.branch || "none"}</dd></div>
      </dl>
      {worklink.diff_stat ? <p className="app-copy">{worklink.diff_stat}</p> : null}
      {worklink.blocked_reason ? <p className="app-copy">{worklink.blocked_reason}</p> : null}
      <div className="chainlink-artifact-links">
        <Link to={drilldownHref("/turns", { issue: issue.id, filter: `#${issue.id}`, q: String(issue.id) })}>Related turns</Link>
        <Link to={drilldownHref("/ops", { tab: "chainlink", issue: issue.id, filter: `#${issue.id}` })}>Ops signals</Link>
        {evidenceHref ? <a href={evidenceHref}>Evidence JSON</a> : null}
        {transcriptHref ? <a href={transcriptHref}>Run transcript</a> : null}
        {prHref ? <a href={prHref}>Review PR</a> : null}
      </div>
      {worklink.tests ? <CodeBlock code={JSON.stringify(worklink.tests, null, 2)} language="json" title="Tests" /> : null}
    </div>
  );
}

function IssueDrawer({
  issue,
  issues,
  selectedId,
  state,
  loading,
  onClose
}: {
  issue: ChainlinkBoardIssue | null;
  issues: ChainlinkBoardIssue[];
  selectedId: number | undefined;
  state: "none" | "loaded" | "unavailable" | "missing";
  loading: boolean;
  onClose: () => void;
}) {
  const byId = React.useMemo(() => new Map(issues.map((item) => [item.id, item])), [issues]);
  return (
    <Drawer open={selectedId !== undefined} title={issue ? `#${issue.id} ${issue.title}` : `Issue #${selectedId}`} onClose={onClose}>
      {loading ? <LoadingState label="Loading issue detail" /> : null}
      {state === "unavailable" ? <ErrorState title="Issue detail unavailable">Chainlink issue #{selectedId} exists, but its detail could not be loaded.</ErrorState> : null}
      {state === "missing" ? <ErrorState title="Issue not found">Chainlink issue #{selectedId} is not in the tracker.</ErrorState> : null}
      {issue ? (
        <div className="chainlink-drawer">
          <div className="chainlink-drawer-section">
            <div className="chainlink-card__badges">
              <Badge tone={statusTone[issue.status] ?? "neutral"}>{issue.status}</Badge>
              <Badge tone={priorityTone[issue.priority] ?? "neutral"}>{issue.priority}</Badge>
            </div>
            {issue.description ? <p className="chainlink-description">{issue.description}</p> : <p className="app-copy">No description.</p>}
          </div>
          <IssueLinks title="Blocked By" ids={issue.blocked_by} byId={byId} />
          <IssueLinks title="Blocking" ids={issue.blocking} byId={byId} />
          <IssueLinks title="Subissues" ids={issue.child_ids} byId={byId} />
          <WorklinkPanel issue={issue} />
          <div className="chainlink-drawer-section">
            <h3>Comments</h3>
            {issue.comments.length ? (
              <ol className="chainlink-comments">
                {issue.comments.map((comment) => (
                  <li key={comment.id}>
                    <div>
                      <strong>{comment.author || "comment"}</strong>
                      <span>{formatBoardTime(comment.created_at)}</span>
                    </div>
                    <p>{comment.body}</p>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="app-copy">No comments in Chainlink detail output.</p>
            )}
          </div>
        </div>
      ) : null}
    </Drawer>
  );
}

export function ChainlinkBoardRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters: ChainlinkBoardFilters = {
    label: searchParams.get("label") || "",
    status: searchParams.get("status") || "",
    priority: searchParams.get("priority") || ""
  };
  const showCompleted = searchParams.get("show_completed") === "true";
  const rawOffset = Number(searchParams.get("offset") || 0);
  const offset = Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  const rawIssue = Number(searchParams.get("issue"));
  const selectedIssueId = Number.isSafeInteger(rawIssue) && rawIssue > 0 ? rawIssue : undefined;
  const params = { ...filters, show_completed: showCompleted, offset, issue: selectedIssueId };
  const query = useQuery({
    queryKey: ["chainlink-board", params],
    queryFn: async ({ signal }) => (await getChainlinkBoard(params, { cache: "no-store", signal })).data
  });
  const board = React.useMemo(() => safeChainlinkBoardData(query.data), [query.data]);
  const visibleIssues = board.issues;
  const dependencies = React.useMemo(() => partitionDependencies(board.issues), [board.issues]);
  const visibleById = React.useMemo(() => new Map(visibleIssues.map((issue) => [issue.id, issue])), [visibleIssues]);
  const rootIssues = React.useMemo(
    () => board.roots.map((id) => visibleById.get(id)).filter((issue): issue is ChainlinkBoardIssue => Boolean(issue)),
    [board.roots, visibleById]
  );
  const selected = board.selected_issue_state === "loaded" && board.selected_issue?.id === selectedIssueId
    ? board.selected_issue : null;
  const nextOffset = board.next_offset;
  const canNext = nextOffset !== null && Number.isSafeInteger(nextOffset)
    && nextOffset > board.offset && nextOffset < board.total_count;

  function setPage(offset: number) {
    const params = new URLSearchParams(searchParams);
    params.set("offset", String(offset));
    setSearchParams(params);
  }

  function selectIssue(issue: ChainlinkBoardIssue | null) {
    const params = new URLSearchParams(searchParams);
    if (issue) params.set("issue", String(issue.id));
    else params.delete("issue");
    setSearchParams(params);
  }

  function setFilter(key: keyof ChainlinkBoardFilters, value: string) {
    const params = new URLSearchParams(searchParams);
    params.delete("offset");
    if (value) params.set(key, value);
    else params.delete(key);
    setSearchParams(params);
  }

  return (
    <div className="chainlink-route">
      <div className="ops-header-row">
        <div>
          <p className="ui-eyebrow">Chainlink / Worklink</p>
          <h1>Kanban Board</h1>
          <p className="app-copy">
            {board.generated_at ? `Generated ${board.generated_at}` : "Read-only lifecycle board"}
            {board.available ? ` | ${board.total_count} matching issues | showing ${board.issues.length ? `${board.offset + 1}-${board.offset + board.issues.length}` : "0"} of ${board.total_count}` : ""}
          </p>
        </div>
        <div className="chainlink-actions">
          <Button disabled={query.isFetching} onClick={() => void query.refetch()} type="button">
            {query.isFetching ? "Refreshing" : "Refresh"}
          </Button>
          <a className="ui-button ui-button--secondary" href={chainlinkBoardHref(params)}>JSON</a>
        </div>
      </div>
      {query.isLoading ? <LoadingState label="Loading Chainlink board" /> : null}
      {query.isError ? (
        <ErrorState title="Chainlink board endpoint failed">
          {query.error instanceof Error ? query.error.message : String(query.error)}
        </ErrorState>
      ) : null}
      {query.data && !board.available ? (
        <ErrorState title="Chainlink unavailable">{board.error || "No Chainlink tracker data is available for this home."}</ErrorState>
      ) : null}
      {board.available ? (
        <>
          <Panel title="Filters" className="chainlink-filter-panel">
            <div className="chainlink-filters">
              <SelectFilter label="Label" value={filters.label} options={board.filters.labels} onChange={(label) => setFilter("label", label)} />
              <SelectFilter label="Status" value={filters.status} options={board.filters.statuses} onChange={(status) => setFilter("status", status)} />
              <SelectFilter label="Priority" value={filters.priority} options={board.filters.priorities} onChange={(priority) => setFilter("priority", priority)} />
              <label className="turn-checkbox">
                <input
                  checked={showCompleted}
                  onChange={(event) => {
                    const params = new URLSearchParams(searchParams);
                    params.set("show_completed", String(event.currentTarget.checked));
                    params.delete("offset");
                    setSearchParams(params);
                  }}
                  type="checkbox"
                />
                <span>Show completed</span>
              </label>
              <Button type="button" onClick={() => {
                const params = new URLSearchParams(searchParams);
                params.delete("show_completed");
                params.delete("offset");
                params.delete("label");
                params.delete("status");
                params.delete("priority");
                setSearchParams(params);
              }}>Clear</Button>
            </div>
            <nav aria-label="Board pages" className="chainlink-actions">
              <Button type="button" disabled={query.isFetching || board.offset <= 0} onClick={() => setPage(Math.max(0, board.offset - 250))}>Previous</Button>
              <Button type="button" disabled={query.isFetching || !canNext} onClick={() => { if (canNext) setPage(nextOffset!); }}>Next</Button>
            </nav>
          </Panel>
          <Panel title="Parent Trees" subtitle="Root issues and subissues on this filtered page; other relatives may be omitted.">
            {rootIssues.length ? (
              <ol className="chainlink-tree">
                {rootIssues.map((issue) => <TreeNode byId={visibleById} issue={issue} key={issue.id} />)}
              </ol>
            ) : (
              <EmptyState title="No visible root issues" />
            )}
          </Panel>
          <div className="chainlink-board" aria-label="Chainlink lifecycle columns">
            {board.columns.map((column) => {
              const issues = column.issue_ids
                .map((id) => visibleById.get(id))
                .filter((issue): issue is ChainlinkBoardIssue => Boolean(issue));
              return (
                <section className="chainlink-column" key={column.id}>
                  <header>
                    <h2>{column.title}</h2>
                    <Badge tone={statusTone[column.id] ?? "neutral"}>{issues.length}</Badge>
                  </header>
                  <div className="chainlink-column__cards">
                    {issues.length ? issues.map((issue) => (
                      <IssueCard issue={issue} key={issue.id} onOpen={selectIssue} />
                    )) : <EmptyState title="No issues" />}
                  </div>
                </section>
              );
            })}
          </div>
          <Panel title="Dependencies" subtitle="Dependencies on this filtered page only. Off-page blockers have unknown status; unlock counts omit off-page issues.">
            {dependencies.ready.length || dependencies.blocked.length ? (
              <div className="chainlink-deps">
                <section className="chainlink-deps__group">
                  <h3>Ready to start <Badge tone="info">{dependencies.ready.length}</Badge></h3>
                  {dependencies.ready.length ? (
                    <ul className="chainlink-deps__list">
                      {dependencies.ready.map(({ issue, unlocks }) => (
                        <li key={issue.id}>
                          <button className="chainlink-deps__issue" onClick={() => selectIssue(issue)} type="button">
                            <span className="chainlink-deps__title">
                              <Badge tone={priorityTone[issue.priority] ?? "neutral"}>{issue.priority}</Badge>
                              #{issue.id} {issue.title}
                            </span>
                            <small>unlocks {unlocks.map((item) => `#${item.id}`).join(", ")}</small>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : <p className="app-copy">Nothing unblocked is waiting on downstream work.</p>}
                </section>
                <section className="chainlink-deps__group">
                  <h3>Blocked <Badge tone="danger">{dependencies.blocked.length}</Badge></h3>
                  {dependencies.blocked.length ? (
                    <ul className="chainlink-deps__list">
                      {dependencies.blocked.map(({ issue, blockers, unknownBlockerIds }) => (
                        <li key={issue.id}>
                          <button className="chainlink-deps__issue" onClick={() => selectIssue(issue)} type="button">
                            <span className="chainlink-deps__title">
                              <Badge tone={priorityTone[issue.priority] ?? "neutral"}>{issue.priority}</Badge>
                              #{issue.id} {issue.title}
                            </span>
                            <small>blocked by {[...blockers.map((item) => `#${item.id} (${item.status})`), ...unknownBlockerIds.map((id) => `#${id} (unknown, outside this page)`)].join(", ")}</small>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : <p className="app-copy">Nothing is blocked.</p>}
                </section>
              </div>
            ) : (
              <EmptyState title="No dependencies between active issues" />
            )}
          </Panel>
        </>
      ) : null}
      <IssueDrawer issue={selected} issues={board.issues} selectedId={selectedIssueId} state={board.selected_issue_state} loading={query.isLoading} onClose={() => selectIssue(null)} />
    </div>
  );
}
