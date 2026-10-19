import React from "./react";
import { UI, request, safeLink, type BadgeTone } from "./host";

interface Run {
  run_id: string; issue_key: string; clearable: boolean; valid: boolean;
  status: string | null; mode: string; branch: string; pr_base: string;
  pr_draft: boolean; lock: "fresh" | "stale" | "absent";
  dead_lock: boolean; lock_session: string | null; pr_url: string | null;
  controller_phase: string; controller_error: string | null; observed_at: string | null;
  sandbox_path: string;
}
interface Detail extends Run {
  gates: Record<string, unknown>; steps: string[]; slices: string[];
  validator: string | null; terminal_result: Record<string, unknown> | null; next?: string | null;
}

const tone: Record<string, BadgeTone> = {
  running: "info", building: "info", completed: "success", merged: "success",
  blocked: "danger", failed: "danger", invalid: "danger", stale: "warning",
  "needs-human": "warning", partial: "warning", fresh: "success"
};

function status(run: Run) {
  return run.controller_phase === "failed" ? "failed" : !run.valid || !run.status ? "unavailable" : run.status;
}

function Fact({ name, value }: { name: string; value: React.ReactNode }) {
  return <div><dt>{name}</dt><dd>{value}</dd></div>;
}

function RunDetails({ runId, back }: { runId: string; back: () => void }) {
  const [run, setRun] = React.useState<Detail | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const refresh = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try { setRun(await request<Detail>(`/factory-run?run_id=${encodeURIComponent(runId)}`)); }
    catch (cause) { setError(String(cause)); }
    finally { setLoading(false); }
  }, [runId]);
  React.useEffect(() => { void refresh(); }, [refresh]);
  return <div data-testid="cultivate-run-detail">
    <UI.Button onClick={back}>Back to runs</UI.Button>
    {loading ? <UI.LoadingState label="Loading run details" /> : error ? <UI.ErrorState title="Could not load run">{error} <UI.Button onClick={() => void refresh()}>Retry</UI.Button></UI.ErrorState>
      : run ? <>
        <UI.Panel title={`Run: ${run.run_id}`}>
          <dl className="cultivate-facts">
            <Fact name="Issue" value={run.issue_key} />
            <Fact name="Status" value={<UI.Badge tone={tone[status(run)] ?? "neutral"}>{status(run)}</UI.Badge>} />
            <Fact name="Controller" value={run.controller_phase || "unknown"} />
          </dl>
        </UI.Panel>
        <UI.Panel title="Slices">
          {run.slices.length ? <UI.DataTable caption="Slice progress" columns={[{ key: "slice", header: "Slice" }, { key: "status", header: "Status" }, { key: "attempt", header: "Attempt" }]}
            rows={run.slices.map(slice => {
              const match = /^([^:]+):([^:()]+?)(?:\((\d+)\))?$/.exec(slice);
              return { slice: match?.[1] ?? slice, status: <UI.Badge tone={tone[match?.[2] ?? ""] ?? "neutral"}>{match?.[2] ?? "unknown"}</UI.Badge>, attempt: match?.[3] ?? "Not reported" };
            })} /> : <p>No slices reported.</p>}
        </UI.Panel>
        <details className="cultivate-diagnostics"><summary>Run diagnostics{run.controller_error ? " (controller error reported)" : ""}</summary>
          <div role="region" aria-label="Run diagnostics" tabIndex={0}>
            <UI.Panel title="Run facts"><dl className="cultivate-facts">
              <Fact name="Projection" value={run.valid ? "Valid" : "Invalid"} />
              <Fact name="Mode" value={run.mode} /><Fact name="Branch" value={run.branch} />
              <Fact name="Base" value={run.pr_base} /><Fact name="Draft PR" value={run.pr_draft ? "Yes" : "No"} />
              <Fact name="Observed" value={run.observed_at || "Not observed"} />
              <Fact name="Sandbox" value={run.sandbox_path} />
              <Fact name="Lock" value={`${run.lock}${run.dead_lock ? " (dead)" : ""}`} />
              <Fact name="Session" value={run.lock_session || "None"} />
              {"next" in run ? <Fact name="Next action" value={run.next || "None"} /> : null}
              {run.pr_url ? <Fact name="PR" value={safeLink(run.pr_url) ? <a href={safeLink(run.pr_url)!} target="_blank" rel="noopener noreferrer">{run.pr_url}</a> : run.pr_url} /> : null}
            </dl></UI.Panel>
            {run.controller_error ? <UI.Panel title="Controller error"><UI.CodeBlock code={run.controller_error} /></UI.Panel> : null}
            <UI.Panel title="Gates"><UI.CodeBlock code={JSON.stringify(run.gates, null, 2)} language="json" /></UI.Panel>
            <UI.Panel title="Steps">{run.steps.length ? <UI.LogBlock lines={run.steps} /> : <p>No steps reported.</p>}</UI.Panel>
            <UI.Panel title="Validator">{run.validator || "No verdict reported."}</UI.Panel>
            <UI.Panel title="Terminal context">{run.terminal_result ? <UI.CodeBlock code={JSON.stringify(run.terminal_result, null, 2)} language="json" /> : "No terminal context reported."}</UI.Panel>
            <UI.Panel title="Cost">Cost attribution is unavailable for this factory projection.</UI.Panel>
          </div>
        </details>
      </> : <UI.EmptyState title="Run not found" />}
  </div>;
}

export function FactoryRuns() {
  const [runs, setRuns] = React.useState<Run[]>([]);
  const [selected, setSelected] = React.useState<string | null>(new URLSearchParams(window.location.search).get("run"));
  const [archiveId, setArchiveId] = React.useState<string | null>(null);
  const [reason, setReason] = React.useState("");
  const [archiveError, setArchiveError] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const busy = React.useRef(false);
  const refresh = React.useCallback(async () => {
    setLoading(true); setError("");
    try { setRuns((await request<{ runs: Run[] }>("/factory-runs")).runs); }
    catch (cause) { setError(String(cause)); }
    finally { setLoading(false); }
  }, []);
  React.useEffect(() => { void refresh(); }, [refresh]);
  const select = (id: string | null) => {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("run", id); else url.searchParams.delete("run");
    window.history.pushState(null, "", url);
    setSelected(id);
  };
  React.useEffect(() => {
    const update = () => setSelected(new URLSearchParams(window.location.search).get("run"));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  return <div className="cultivate" data-testid="cultivate-factory-runs">
    <UI.DashboardHeader title="Factory Runs">Feature factory runs and diagnostics</UI.DashboardHeader>
    {selected ? <RunDetails runId={selected} back={() => select(null)} /> : <>
      <UI.Button disabled={loading} onClick={() => void refresh()}>Refresh runs</UI.Button>
      {loading ? <UI.LoadingState label="Loading factory runs" /> : error ? <UI.ErrorState title="Could not load factory runs">{error} Retry with Refresh runs.</UI.ErrorState>
        : !runs.length ? <UI.EmptyState title="No factory runs found">Runs will appear here when a feature factory run starts. Refresh to check again.</UI.EmptyState>
          : <div className="cultivate-list">{runs.map(run => <UI.Card key={run.run_id} data-testid={`cultivate-run-${run.run_id}`}>
            <div className="cultivate-card-header"><UI.Button onClick={() => select(run.run_id)}>{run.run_id} · {run.issue_key}</UI.Button>
              {run.clearable ? <UI.Button aria-label={`Archive run ${run.run_id}`} disabled={submitting} onClick={() => { setReason(""); setArchiveError(""); setArchiveId(run.run_id); }}>Archive</UI.Button> : null}</div>
            <p><UI.Badge tone={tone[status(run)] ?? "neutral"}>{status(run)}</UI.Badge> <UI.Badge tone={run.valid ? "success" : "danger"}>{run.valid ? "Valid" : "Invalid projection"}</UI.Badge> <UI.Badge tone={tone[run.lock] ?? "neutral"}>Lock {run.lock}</UI.Badge></p>
            <p>{run.mode} · {run.branch} → {run.pr_base}</p><p>Controller: {run.controller_phase || "unknown"} · Observed: {run.observed_at || "Not observed"}</p>
            {run.controller_error ? <p role="alert">{run.controller_error}</p> : null}
          </UI.Card>)}</div>}
    </>}
    <UI.Dialog open={archiveId !== null} title={`Archive run ${archiveId}?`} onClose={() => { if (!busy.current) setArchiveId(null); }}>
      <p>Archive this run to remove it from the list. This does not delete the run.</p>
      <form onSubmit={async event => {
        event.preventDefault();
        if (!archiveId || !reason.trim() || busy.current) return;
        busy.current = true; setSubmitting(true); setArchiveError("");
        try {
          await request(`/factory-run/archive?run_id=${encodeURIComponent(archiveId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: reason.trim() }) });
          await refresh(); setArchiveId(null);
        } catch (cause) { setArchiveError(String(cause)); }
        finally { busy.current = false; setSubmitting(false); }
      }}>
        <label htmlFor="cultivate-archive-reason">Archive reason</label>
        <textarea id="cultivate-archive-reason" className="ui-input" required value={reason} disabled={submitting} onChange={event => setReason(event.target.value)} />
        <div className="cultivate-actions"><UI.Button type="button" disabled={submitting} onClick={() => setArchiveId(null)}>Cancel</UI.Button>
          <UI.Button type="submit" variant="primary" disabled={submitting || !reason.trim()}>{submitting ? "Archiving…" : "Confirm archive"}</UI.Button></div>
        {archiveError ? <UI.ErrorState title="Archive failed">{archiveError}</UI.ErrorState> : null}
      </form>
    </UI.Dialog>
  </div>;
}
