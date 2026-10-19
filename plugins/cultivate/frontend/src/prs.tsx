import React from "./react";
import { UI, request, safeLink } from "./host";

interface PullRequest {
  number: number; title: string; url: string; author: string;
  created_at: string; review_decision: string; is_draft: boolean;
}
interface Board {
  available: boolean; error: string | null; repo: string | null;
  truncated: boolean; total_count: number; pull_requests: PullRequest[];
}

export function PullRequests() {
  const [board, setBoard] = React.useState<Board | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const refresh = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try { setBoard(await request<Board>("/pr-board")); }
    catch (cause) { setError(String(cause)); }
    finally { setLoading(false); }
  }, []);
  React.useEffect(() => { void refresh(); }, [refresh]);
  return <div className="cultivate" data-testid="cultivate-pr-panel">
    <UI.Panel title="Open PRs" subtitle={board?.repo ?? undefined} actions={<UI.Button disabled={loading} onClick={() => void refresh()}>Refresh PRs</UI.Button>}>
      {loading ? <UI.LoadingState label="Loading open PRs" /> : error ? <UI.ErrorState title="Could not load PRs">{error} Retry with Refresh PRs.</UI.ErrorState>
        : !board?.available ? <UI.EmptyState title="PR view unavailable">{board?.error || "GitHub PR data is not available."}</UI.EmptyState>
          : !board.pull_requests.length ? <UI.EmptyState title="No open PRs">Open pull requests will appear here.</UI.EmptyState>
            : <>
              {board.truncated ? <p role="status">Showing {board.pull_requests.length} of {board.total_count} PRs.</p> : null}
              <UI.DataTable caption="Open GitHub pull requests" columns={[
                { key: "number", header: "PR" }, { key: "title", header: "Title" },
                { key: "state", header: "State" }, { key: "age", header: "Created" }, { key: "link", header: "Link" }
              ]} rows={board.pull_requests.map(pr => ({
                number: `#${pr.number}`, title: pr.title,
                state: pr.is_draft ? "Draft" : pr.review_decision.replaceAll("_", " ").toLowerCase() || "Review pending",
                age: pr.created_at ? new Date(pr.created_at).toLocaleString() : "Not reported",
                link: safeLink(pr.url) ? <a href={safeLink(pr.url)!} target="_blank" rel="noopener noreferrer" aria-label={`Open PR ${pr.number}`}>Open</a> : "n/a"
              }))} />
            </>}
    </UI.Panel>
  </div>;
}
