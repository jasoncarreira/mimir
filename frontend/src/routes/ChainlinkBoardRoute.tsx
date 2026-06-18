import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getChainlinkBoard, type ChainlinkBoardIssue } from "../api";
import { drilldownHref } from "../routeState";
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
  issueMatchesFilters,
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

function WorklinkPanel({ issue }: { issue: ChainlinkBoardIssue }) {
  const worklink = issue.worklink;
  if (!worklink) {
    return (
      <div className="chainlink-drawer-section">
        <h3>Worklink</h3>
        <p className="app-copy">No Worklink evidence found for this issue.</p>
      </div>
    );
  }
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
        {worklink.evidence_href ? <a href={worklink.evidence_href}>Evidence JSON</a> : null}
        {worklink.transcript_href ? <a href={worklink.transcript_href}>Run transcript</a> : null}
        {worklink.pr_url ? <a href={worklink.pr_url}>Review PR</a> : null}
      </div>
      {worklink.tests ? <CodeBlock code={JSON.stringify(worklink.tests, null, 2)} language="json" title="Tests" /> : null}
    </div>
  );
}

function IssueDrawer({
  issue,
  issues,
  onClose
}: {
  issue: ChainlinkBoardIssue | null;
  issues: ChainlinkBoardIssue[];
  onClose: () => void;
}) {
  const byId = React.useMemo(() => new Map(issues.map((item) => [item.id, item])), [issues]);
  return (
    <Drawer open={Boolean(issue)} title={issue ? `#${issue.id} ${issue.title}` : "Issue"} onClose={onClose}>
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
  const query = useQuery({
    queryKey: ["chainlink-board"],
    queryFn: async () => (await getChainlinkBoard({ cache: "no-store" })).data
  });
  const [filters, setFilters] = React.useState<ChainlinkBoardFilters>({
    label: searchParams.get("label") || "",
    status: searchParams.get("status") || "",
    priority: searchParams.get("priority") || ""
  });
  const board = React.useMemo(() => safeChainlinkBoardData(query.data), [query.data]);
  const visibleIssues = React.useMemo(
    () => board.issues.filter((issue) => issueMatchesFilters(issue, filters)),
    [board.issues, filters]
  );
  const visibleById = React.useMemo(() => new Map(visibleIssues.map((issue) => [issue.id, issue])), [visibleIssues]);
  const rootIssues = React.useMemo(
    () => board.roots.map((id) => visibleById.get(id)).filter((issue): issue is ChainlinkBoardIssue => Boolean(issue)),
    [board.roots, visibleById]
  );
  const selectedIssueId = Number.parseInt(searchParams.get("issue") || "", 10);
  const selected = Number.isFinite(selectedIssueId)
    ? board.issues.find((issue) => issue.id === selectedIssueId) ?? null
    : null;

  React.useEffect(() => {
    setFilters({
      label: searchParams.get("label") || "",
      status: searchParams.get("status") || "",
      priority: searchParams.get("priority") || ""
    });
  }, [searchParams]);

  function selectIssue(issue: ChainlinkBoardIssue | null) {
    const params = new URLSearchParams(searchParams);
    if (issue) params.set("issue", String(issue.id));
    else params.delete("issue");
    setSearchParams(params);
  }

  function setFilter(key: keyof ChainlinkBoardFilters, value: string) {
    setFilters((prior) => ({ ...prior, [key]: value }));
    const params = new URLSearchParams(searchParams);
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
            {board.truncated ? ` | showing ${board.issues.length} of ${board.total_count}` : ""}
          </p>
        </div>
        <div className="chainlink-actions">
          <Button disabled={query.isFetching} onClick={() => void query.refetch()} type="button">
            {query.isFetching ? "Refreshing" : "Refresh"}
          </Button>
          <a className="ui-button ui-button--secondary" href="/api/v1/chainlink-board">JSON</a>
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
              <Button type="button" onClick={() => {
                setFilters({ label: "", status: "", priority: "" });
                const params = new URLSearchParams(searchParams);
                params.delete("label");
                params.delete("status");
                params.delete("priority");
                setSearchParams(params);
              }}>Clear</Button>
            </div>
          </Panel>
          <Panel title="Parent Trees" subtitle="Root issues and visible subissue progress from Chainlink.">
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
          <Panel title="Dependency Edges" subtitle="Parent/child and blocking edges from Chainlink.">
            {board.edges.length ? (
              <div className="chainlink-edge-list">
                {board.edges.slice(0, 80).map((edge, index) => (
                  <span key={`${edge.from}-${edge.to}-${edge.kind}-${index}`}>#{edge.from} {edge.kind} #{edge.to}</span>
                ))}
              </div>
            ) : (
              <EmptyState title="No dependency edges" />
            )}
          </Panel>
        </>
      ) : null}
      {searchParams.get("issue") && !selected && board.available ? (
        <ErrorState title="Issue not found">No loaded Chainlink issue matches #{searchParams.get("issue")}.</ErrorState>
      ) : null}
      <IssueDrawer issue={selected} issues={board.issues} onClose={() => selectIssue(null)} />
    </div>
  );
}
