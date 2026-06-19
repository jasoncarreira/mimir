import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  getSagaActivationHistogram,
  getSagaAtom,
  getSagaClusters,
  getSagaStats,
  listSagaAtoms,
  runSagaSql,
  searchSagaAtoms,
  validateSagaAtomId,
  validateSagaSearchQuery,
  validateSagaSql,
  type SagaAtomSummary,
  type SagaSqlCell
} from "./api/saga";
import type { SagaAtomDetailData } from "./api/generated/contracts";
import { DetailDrawer } from "./DetailDrawer";
import { drilldownHref } from "./routeState";
import { Button, DataTable, EmptyState, ErrorState, LoadingState, Panel, TextInput } from "./ui";

type EvidenceKind = "atom" | "observation" | "triple";

export function classifySagaEvidence(item: SagaAtomSummary | Record<string, unknown>): EvidenceKind {
  const memoryType = String((item as SagaAtomSummary).memory_type ?? "").toLowerCase();
  if (memoryType === "observation") return "observation";
  if ("subject" in item && "predicate" in item && "object" in item) return "triple";
  return "atom";
}

function formatNumber(value: number | null | undefined) {
  return value == null ? "-" : value.toLocaleString();
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

function formatCell(value: SagaSqlCell | undefined) {
  if (value === null || value === undefined) return "NULL";
  return String(value);
}

function formatBytes(value: number | null | undefined) {
  if (value == null) return "-";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let scaled = value / 1024;
  let unitIndex = 0;
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024;
    unitIndex += 1;
  }
  return `${scaled.toFixed(scaled >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatBool(value: unknown) {
  if (value === true || value === 1) return "yes";
  if (value === false || value === 0) return "no";
  return "-";
}

function formatTopics(value: unknown) {
  if (!Array.isArray(value) || !value.length) return "-";
  return value.map((item) => String(item)).join(", ");
}

function stringField(source: unknown, keys: string[]): string {
  if (!source || typeof source !== "object" || Array.isArray(source)) return "";
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}


function AtomDetail({ atomId }: { atomId: string }) {
  const query = useQuery({
    enabled: Boolean(atomId),
    queryKey: ["saga", "atom", atomId],
    queryFn: async () => (await getSagaAtom(atomId)).data
  });

  if (!atomId) return <EmptyState title="Select an atom" />;
  if (query.isLoading) return <LoadingState label="Loading atom detail" />;
  if (query.isError) {
    return <ErrorState title="Atom failed">{query.error instanceof Error ? query.error.message : String(query.error)}</ErrorState>;
  }
  const atom = query.data as SagaAtomDetailData & Record<string, unknown>;
  const embedding = atom.embedding as { provider?: string; model?: string; dim?: number } | null | undefined;
  const relations = Array.isArray(atom.relations_out) ? atom.relations_out as Array<Record<string, unknown>> : [];
  const metadata = atom.metadata && typeof atom.metadata === "object" && !Array.isArray(atom.metadata)
    ? atom.metadata as Record<string, unknown>
    : {};
  const sessionId = stringField(atom, ["session_id"]) || stringField(metadata, ["session_id", "source_session_id"]);
  const turnId = stringField(atom, ["turn_id", "source_turn_id"]) || stringField(metadata, ["turn_id", "source_turn_id"]);
  const channelId = stringField(atom, ["channel_id"]) || stringField(metadata, ["channel_id"]);

  return (
    <div className="saga-detail">
      <pre className="saga-content-full">{String(atom.content ?? "")}</pre>
      <dl className="facts-grid facts-grid--compact">
        <div><dt>ID</dt><dd>{atom.id}</dd></div>
        <div><dt>Type</dt><dd>{String(atom.memory_type ?? "raw")}</dd></div>
        <div><dt>Source</dt><dd>{String(atom.source_type ?? "-")}</dd></div>
        <div><dt>Stream</dt><dd>{String(atom.stream ?? "semantic")}</dd></div>
        <div><dt>Session</dt><dd>{sessionId ? <Link to={drilldownHref("/turns", { session: sessionId, turn: turnId || undefined })}>{sessionId}</Link> : "-"}</dd></div>
        <div><dt>Channel</dt><dd>{channelId ? <Link to={drilldownHref("/turns", { channel: channelId })}>{channelId}</Link> : "-"}</dd></div>
        <div><dt>Source turn</dt><dd>{turnId ? <Link to={drilldownHref("/turns", { turn: turnId, session: sessionId || undefined })}>{turnId}</Link> : "-"}</dd></div>
        <div><dt>Topics</dt><dd>{formatTopics(atom.topics)}</dd></div>
        <div><dt>Confidence</dt><dd>{atom.encoding_confidence == null ? "-" : Number(atom.encoding_confidence).toFixed(3)}</dd></div>
        <div><dt>Pinned</dt><dd>{formatBool(atom.is_pinned)}</dd></div>
        <div><dt>Tombstoned</dt><dd>{formatBool(atom.tombstoned)}</dd></div>
        <div><dt>Tombstone reason</dt><dd>{String(atom.tombstoned_reason ?? "-")}</dd></div>
        <div><dt>Accesses</dt><dd>{String(atom.access_count ?? "-")}</dd></div>
        <div><dt>Last access</dt><dd>{formatDate(atom.last_access_ts as string | undefined)}</dd></div>
        <div><dt>Last access source</dt><dd>{String(atom.last_access_source ?? "-")}</dd></div>
        <div><dt>Activation</dt><dd>{`arousal ${Number(atom.arousal ?? 0).toFixed(3)} · valence ${Number(atom.valence ?? 0).toFixed(3)}`}</dd></div>
        <div><dt>Embedding</dt><dd>{embedding ? `${embedding.provider}/${embedding.model} dim=${embedding.dim}` : "none"}</dd></div>
      </dl>
      <section className="saga-relations">
        <h3>Relations out</h3>
        {relations.length ? relations.map((relation, index) => (
          <p key={index}>
            <strong>{String(relation.relation_type ?? "related")}</strong>{" "}
            {String(relation.target_id ?? "")}
            {relation.confidence != null ? ` · confidence ${Number(relation.confidence).toFixed(2)}` : ""}
            {relation.target_preview ? ` · ${String(relation.target_preview)}` : ""}
          </p>
        )) : <p className="app-copy">none</p>}
      </section>
    </div>
  );
}

// github: combined one-line list of atoms + observations (the old saga viewer's
// shape). Click a row to pop out the detail.
function SagaAtomList({
  atoms,
  selectedId,
  onSelect
}: {
  atoms: SagaAtomSummary[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  if (!atoms.length) return <EmptyState title="No atoms or observations found" />;
  return (
    <div aria-label="Atoms and observations" className="saga-atom-list" role="list">
      {atoms.map((atom) => {
        const kind = classifySagaEvidence(atom);
        return (
          <button
            className={`saga-atom-row${selectedId === atom.id ? " saga-atom-row--selected" : ""}`}
            key={atom.id}
            onClick={() => onSelect(atom.id)}
            type="button"
          >
            <span className="saga-atom-row__kind" data-kind={kind}>{kind}</span>
            <span className="saga-atom-row__content">{atom.content_preview || "(empty)"}</span>
            <span className="saga-atom-row__meta">
              {atom.channel_id || "no channel"}
              {atom.encoding_confidence != null ? ` · conf ${atom.encoding_confidence.toFixed(2)}` : ""}
              {atom.is_pinned ? " · pinned" : ""}
            </span>
            <span className="saga-atom-row__time">{formatDate(atom.created_at)}</span>
          </button>
        );
      })}
    </div>
  );
}

function AtomsWorkflow() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [channel, setChannel] = React.useState("");
  const [limit, setLimit] = React.useState(50);
  const [manualAtom, setManualAtom] = React.useState(searchParams.get("atom") || "");
  const [manualError, setManualError] = React.useState("");
  const selectedAtom = searchParams.get("atom") || "";
  const atomsQuery = useQuery({
    queryKey: ["saga", "recent", channel, limit],
    queryFn: () => listSagaAtoms({ channel, limit })
  });
  const atoms = atomsQuery.data?.data.atoms ?? [];
  const channels = atomsQuery.data?.data.channels ?? [];

  function selectAtom(atomId: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", "atoms");
    params.set("atom", atomId);
    setSearchParams(params);
  }

  function clearAtom() {
    const params = new URLSearchParams(searchParams);
    params.delete("atom");
    setSearchParams(params);
  }

  return (
    <div className="saga-workflow">
      <form className="saga-controls" onSubmit={(event) => event.preventDefault()}>
        <label><span>Channel</span><select value={channel} onChange={(event) => setChannel(event.target.value)}><option value="">all channels</option>{channels.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        <label><span>Limit</span><select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>{[25, 50, 100, 200].map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        <Button onClick={() => void atomsQuery.refetch()} type="button">Refresh</Button>
      </form>
      <form
        className="saga-controls"
        onSubmit={(event) => {
          event.preventDefault();
          try {
            selectAtom(validateSagaAtomId(manualAtom));
            setManualError("");
          } catch (error) {
            setManualError(error instanceof Error ? error.message : String(error));
          }
        }}
      >
        <label><span>Atom ID</span><TextInput value={manualAtom} onChange={(event) => setManualAtom(event.target.value)} /></label>
        <Button type="submit" variant="primary">Inspect</Button>
        {manualError ? <span className="saga-validation">{manualError}</span> : null}
      </form>
      {atomsQuery.isLoading ? <LoadingState label="Loading atoms" /> : null}
      {atomsQuery.isError ? <ErrorState title="Atoms failed">{atomsQuery.error instanceof Error ? atomsQuery.error.message : String(atomsQuery.error)}</ErrorState> : null}
      {!atomsQuery.isLoading && !atomsQuery.isError ? (
        <Panel
          title="Atoms & Observations"
          subtitle={`Newest first${atomsQuery.data?.meta?.total != null ? ` · ${atoms.length} of ${atomsQuery.data.meta.total}` : ""} · click a row for detail.`}
        >
          <SagaAtomList atoms={atoms} selectedId={selectedAtom} onSelect={selectAtom} />
        </Panel>
      ) : null}
      <DetailDrawer onClose={clearAtom} open={Boolean(selectedAtom)} title="Atom Detail">
        <AtomDetail atomId={selectedAtom} />
      </DetailDrawer>
    </div>
  );
}

function SearchWorkflow() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [queryText, setQueryText] = React.useState("");
  const [channel, setChannel] = React.useState("");
  const [submitted, setSubmitted] = React.useState("");
  const [validation, setValidation] = React.useState("");
  const selectedAtom = searchParams.get("atom") || "";
  const searchQuery = useQuery({
    enabled: Boolean(submitted),
    queryKey: ["saga", "search", submitted, channel],
    queryFn: () => searchSagaAtoms({ q: submitted, channel, limit: 100 })
  });
  const channels = searchQuery.data?.data.channel_filter ? [searchQuery.data.data.channel_filter] : [];

  function selectAtom(atomId: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", "search");
    params.set("atom", atomId);
    setSearchParams(params);
  }

  function clearAtom() {
    const params = new URLSearchParams(searchParams);
    params.delete("atom");
    setSearchParams(params);
  }

  return (
    <div className="saga-workflow">
      <form
        className="saga-controls"
        onSubmit={(event) => {
          event.preventDefault();
          try {
            setSubmitted(validateSagaSearchQuery(queryText));
            setValidation("");
          } catch (error) {
            setValidation(error instanceof Error ? error.message : String(error));
          }
        }}
      >
        <label><span>Query</span><TextInput value={queryText} onChange={(event) => setQueryText(event.target.value)} /></label>
        <label><span>Channel</span><TextInput list="saga-search-channels" value={channel} onChange={(event) => setChannel(event.target.value)} /></label>
        <datalist id="saga-search-channels">{channels.map((item) => <option key={item} value={item} />)}</datalist>
        <Button type="submit" variant="primary">Search</Button>
        {validation ? <span className="saga-validation">{validation}</span> : null}
      </form>
      {searchQuery.isLoading ? <LoadingState label="Searching atoms" /> : null}
      {searchQuery.isError ? <ErrorState title="Search failed">{searchQuery.error instanceof Error ? searchQuery.error.message : String(searchQuery.error)}</ErrorState> : null}
      {searchQuery.data ? (
        <Panel title="Search Results" subtitle={`${searchQuery.data.meta?.total ?? searchQuery.data.data.atoms.length} matches for ${submitted}`}>
          <SagaAtomList atoms={searchQuery.data.data.atoms} selectedId={selectedAtom} onSelect={selectAtom} />
        </Panel>
      ) : !submitted ? <EmptyState title="Enter a query to search atoms" /> : null}
      <DetailDrawer onClose={clearAtom} open={Boolean(selectedAtom)} title="Atom Detail">
        <AtomDetail atomId={selectedAtom} />
      </DetailDrawer>
    </div>
  );
}

function ActivationWorkflow() {
  const [days, setDays] = React.useState(7);
  const query = useQuery({
    queryKey: ["saga", "activation", days],
    queryFn: () => getSagaActivationHistogram({ days })
  });
  const buckets = query.data?.data.buckets ?? [];
  const max = Math.max(1, ...buckets.map((bucket) => bucket.count));
  return (
    <Panel title="Activation Histogram" subtitle="Retrieval activation over the selected window.">
      <form className="saga-controls" onSubmit={(event) => event.preventDefault()}>
        <label><span>Days</span><select value={days} onChange={(event) => setDays(Number(event.target.value))}>{[1, 7, 30, 90].map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      </form>
      {query.isLoading ? <LoadingState label="Loading activation histogram" /> : null}
      {query.isError ? <ErrorState title="Activation failed">{query.error instanceof Error ? query.error.message : String(query.error)}</ErrorState> : null}
      {query.data && !buckets.length ? <EmptyState title={`No activation data for ${days} day(s)`}>{`${query.data.data.never_accessed ?? 0} atoms never accessed`}</EmptyState> : null}
      {buckets.length ? (
        <div className="saga-histogram" aria-label="Activation histogram">
          <p className="app-copy">{formatNumber(query.data?.meta?.total)} atoms with finite activation; {formatNumber(query.data?.data.never_accessed)} never accessed.</p>
          {buckets.map((bucket) => (
            <div className="saga-histogram__row" key={`${bucket.range_start}-${bucket.range_end}`}>
              <span>[{bucket.range_start.toFixed(2)}, {bucket.range_end.toFixed(2)})</span>
              <meter min={0} max={max} value={bucket.count}>{bucket.count}</meter>
              <strong>{bucket.count}</strong>
            </div>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

function ClustersWorkflow() {
  const query = useQuery({
    queryKey: ["saga", "clusters"],
    queryFn: () => getSagaClusters()
  });
  const clusters = query.data?.data.clusters ?? [];
  return (
    <Panel title="Session Clusters" subtitle="Browse atoms grouped by session.">
      {query.isLoading ? <LoadingState label="Loading clusters" /> : null}
      {query.isError ? <ErrorState title="Clusters failed">{query.error instanceof Error ? query.error.message : String(query.error)}</ErrorState> : null}
      {query.data && !clusters.length ? <EmptyState title="No clusters found" /> : null}
      <div className="saga-cluster-list">
        {clusters.map((cluster) => (
          <article className="saga-cluster" key={cluster.cluster_id ?? "unclustered"}>
            <h3>{cluster.size} atoms <span>{cluster.cluster_id ?? "(no session)"}</span></h3>
            {cluster.sample_atoms.map((atom) => <p key={atom.id}><code>{atom.id}</code> {atom.content_preview}</p>)}
          </article>
        ))}
      </div>
    </Panel>
  );
}

function SqlWorkflow() {
  const [sql, setSql] = React.useState("SELECT id, subject, predicate, object FROM triples WHERE tombstoned=0 LIMIT 20");
  const [result, setResult] = React.useState<Awaited<ReturnType<typeof runSagaSql>> | null>(null);
  const [error, setError] = React.useState("");
  const [running, setRunning] = React.useState(false);
  const columns = result?.data.columns ?? [];
  const rows = result?.data.rows ?? [];
  const rowObjects = rows.map((row) => Object.fromEntries(columns.map((column, index) => [column, formatCell(row[index])])));
  const typedRows = rowObjects.filter((row) => classifySagaEvidence(row) === "triple");

  async function submit() {
    try {
      setRunning(true);
      setError("");
      const envelope = await runSagaSql(validateSagaSql(sql));
      setResult(envelope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Panel title="Triples and SQL" subtitle="Read-only SQL for browsing triples without changing SAGA storage semantics.">
      <form
        className="saga-sql-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea value={sql} onChange={(event) => setSql(event.target.value)} rows={5} />
        <div className="saga-controls">
          <Button disabled={running} type="submit" variant="primary">{running ? "Running" : "Run Query"}</Button>
          <Button type="button" onClick={() => setSql("SELECT id, subject, predicate, object, confidence FROM triples WHERE tombstoned=0 LIMIT 20")}>Triples</Button>
          <Button type="button" onClick={() => setSql("SELECT id, content, memory_type, created_at FROM atoms WHERE tombstoned=0 ORDER BY created_at DESC LIMIT 20")}>Atoms</Button>
        </div>
      </form>
      {error ? <ErrorState title="Query rejected">{error}</ErrorState> : null}
      {typedRows.length ? (
        <Panel className="saga-inline-panel" title="Triples" subtitle="Triple-shaped rows are rendered separately.">
          <DataTable
            columns={columns.map((column) => ({ key: column, header: column }))}
            rows={typedRows}
          />
        </Panel>
      ) : null}
      {result && columns.length ? (
        <DataTable
          caption={`${result.data.row_count ?? rows.length} row${(result.data.row_count ?? rows.length) === 1 ? "" : "s"}${result.meta?.truncated ? " · truncated" : ""}`}
          columns={columns.map((column) => ({ key: column, header: column }))}
          rows={rowObjects}
        />
      ) : null}
      {result && !columns.length ? <EmptyState title="Query returned no columns" /> : null}
    </Panel>
  );
}

export function SagaDashboard() {
  const [searchParams, setSearchParams] = useSearchParams();
  const stats = useQuery({
    queryKey: ["saga", "stats"],
    queryFn: async () => (await getSagaStats()).data
  });
  const tabs = [
    ["atoms", "Atoms"],
    ["search", "Search"],
    ["activation", "Activation"],
    ["clusters", "Clusters"],
    ["triples", "Triples"]
  ] as const;
  const tabIds = tabs.map(([id]) => id);
  const tab = tabIds.includes(searchParams.get("tab") as typeof tabIds[number])
    ? searchParams.get("tab") as typeof tabIds[number]
    : "atoms";

  function setTab(tabId: string) {
    const params = new URLSearchParams(searchParams);
    params.set("tab", tabId);
    setSearchParams(params);
  }

  return (
    <>
      <header className="ui-header saga-header">
        <p className="ui-eyebrow">SAGA dashboard</p>
        <h1>SAGA Memory</h1>
        <div className="ui-header__body">Browse atoms, observations, triples, activation, and retrieval metadata.</div>
      </header>
      <section className="saga-stats" aria-label="SAGA stats">
        {stats.isError ? <ErrorState title="SAGA stats failed">{stats.error instanceof Error ? stats.error.message : String(stats.error)}</ErrorState> : null}
        {[
          ["Atoms", stats.data?.atom_count],
          ["Sessions", stats.data?.session_count],
          ["Triples", stats.data?.triple_count],
          ["Tombstoned", stats.data?.tombstoned_count],
          ["DB size", formatBytes(stats.data?.db_size_bytes)],
          ["Schema", stats.data?.schema_version == null ? undefined : `v${stats.data.schema_version}`]
        ].map(([label, value]) => (
          <div className="saga-stat" key={label}>
            <span>{label}</span>
            <strong>{stats.isLoading ? "-" : String(value ?? "-")}</strong>
          </div>
        ))}
      </section>
      <div className="ui-tabs saga-tabs">
        <div aria-label="SAGA workflows" className="ui-tabs__list" role="tablist">
          {tabs.map(([id, label]) => (
            <button aria-selected={tab === id} className="ui-tabs__tab" key={id} onClick={() => setTab(id)} role="tab" type="button">{label}</button>
          ))}
        </div>
        <div className="ui-tabs__panel" role="tabpanel">
          {tab === "atoms" ? <AtomsWorkflow /> : null}
          {tab === "search" ? <SearchWorkflow /> : null}
          {tab === "activation" ? <ActivationWorkflow /> : null}
          {tab === "clusters" ? <ClustersWorkflow /> : null}
          {tab === "triples" ? <SqlWorkflow /> : null}
        </div>
      </div>
    </>
  );
}
