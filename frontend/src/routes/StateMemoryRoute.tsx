import { useQuery } from "@tanstack/react-query";
import React from "react";
import { useSearchParams } from "react-router-dom";
import {
  ApiError,
  getMemoryFile,
  getMemoryTree,
  searchMemoryFiles,
  type MemoryFileData,
  type MemorySearchHit,
  type MemoryTreeDir,
  type MemoryTreeNode
} from "../api";
import type { DashboardSurface } from "../dashboardExtensions";
import { Badge, Button, DashboardHeader, ErrorState, LoadingState, Panel, TextInput } from "../ui";
import {
  countByLayer,
  defaultMemoryPath,
  descriptionFromContent,
  flattenFiles,
  fmtBytes,
  fmtTimestamp,
  searchResultsCaption,
  sourceLayerForPath
} from "./stateMemoryViewModel";

function ApiErrorBlock({ error, title }: { error: unknown; title: string }) {
  let detail = error instanceof Error ? error.message : String(error);
  if (error instanceof ApiError && error.body && typeof error.body === "object") {
    const body = error.body as { error?: { code?: string; message?: string } };
    if (body.error?.code) detail = `${body.error.code}: ${body.error.message ?? detail}`;
  }
  return <ErrorState title={title}>{detail}</ErrorState>;
}

function TreeNodeView({
  node,
  selectedPath,
  onSelect
}: {
  node: MemoryTreeNode;
  selectedPath: string;
  onSelect: (path: string) => void;
}) {
  const defaultOpen = (
    node.path === ""
    || node.path === "memory"
    || node.path === "state"
    || node.path === "memory/core"
  );
  const [open, setOpen] = React.useState(defaultOpen);

  if (node.type === "file") {
    return (
      <button
        className={`memory-browser__file${selectedPath === node.path ? " memory-browser__file--selected" : ""}`}
        onClick={() => onSelect(node.path)}
        type="button"
      >
        <span>{node.name}</span>
        {node.desc ? <small>{node.desc}</small> : null}
      </button>
    );
  }

  return (
    <div className="memory-browser__node">
      {node.path ? (
        <button
          aria-expanded={open}
          className="memory-browser__dir"
          onClick={() => setOpen(!open)}
          type="button"
        >
          <span aria-hidden="true">{open ? "v" : ">"}</span>
          <span>{node.name}</span>
        </button>
      ) : null}
      <div className="memory-browser__children" hidden={!open}>
        {node.children.map((child) => (
          <TreeNodeView
            key={child.path}
            node={child}
            onSelect={onSelect}
            selectedPath={selectedPath}
          />
        ))}
      </div>
    </div>
  );
}

function SearchResults({
  hits,
  onSelect
}: {
  hits: MemorySearchHit[];
  onSelect: (path: string) => void;
}) {
  if (!hits.length) return <p className="memory-browser__muted">No matching files.</p>;
  return (
    <div className="memory-browser__search-results">
      {hits.map((hit) => (
        <button
          className="memory-browser__hit"
          key={`${hit.path}:${hit.line_no}:${hit.snippet}`}
          onClick={() => onSelect(hit.path)}
          type="button"
        >
          <span>{hit.path}:{hit.line_no}</span>
          <small>{hit.snippet}</small>
        </button>
      ))}
    </div>
  );
}

function FileDetail({ path }: { path: string }) {
  const fileQuery = useQuery({
    enabled: Boolean(path),
    queryKey: ["memory-file", path],
    queryFn: async () => (await getMemoryFile(path)).data
  });
  const file = fileQuery.data as MemoryFileData | undefined;
  const desc = React.useMemo(() => descriptionFromContent(file?.content), [file?.content]);

  if (!path) {
    return <LoadingState label="Select a state or memory file" />;
  }
  if (fileQuery.isLoading) return <LoadingState label="Loading file" />;
  if (fileQuery.isError) return <ApiErrorBlock error={fileQuery.error} title="File load failed" />;
  if (!file) return <ErrorState title="File unavailable">No file payload was returned.</ErrorState>;

  return (
    <article className="memory-detail">
      <header className="memory-detail__header">
        <div>
          <p className="ui-eyebrow">{sourceLayerForPath(file.path)}</p>
          <h2>{file.path}</h2>
          {desc ? <p>{desc}</p> : null}
        </div>
        <Badge tone="info">{fmtBytes(file.size)}</Badge>
      </header>
      <dl className="facts-grid facts-grid--compact">
        <div><dt>Path</dt><dd>{file.path}</dd></div>
        <div><dt>Layer</dt><dd>{sourceLayerForPath(file.path)}</dd></div>
        <div><dt>Modified</dt><dd>{fmtTimestamp(file.modified)}</dd></div>
      </dl>
      <pre className="memory-detail__content">{file.content}</pre>
    </article>
  );
}

export function StateMemoryRoute({ surface }: { surface: DashboardSurface }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = React.useState(searchParams.get("q") || "");
  const selectedPath = searchParams.get("path") || "";
  const treeQuery = useQuery({
    queryKey: ["memory-tree"],
    queryFn: async () => (await getMemoryTree()).data
  });
  const searchQuery = useQuery({
    enabled: query.trim().length > 0,
    queryKey: ["memory-search", query.trim()],
    queryFn: async () => searchMemoryFiles(query.trim())
  });
  const tree = treeQuery.data as MemoryTreeDir | undefined;
  const files = React.useMemo(() => (tree ? flattenFiles(tree) : []), [tree]);
  const { state: stateCount, memory: memoryCount } = React.useMemo(() => countByLayer(files), [files]);

  function selectPath(path: string) {
    const params = new URLSearchParams(searchParams);
    params.set("path", path);
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    setSearchParams(params);
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const params = new URLSearchParams(searchParams);
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    setSearchParams(params);
  }

  React.useEffect(() => {
    if (!selectedPath && files.length) {
      selectPath(defaultMemoryPath(files));
    }
  }, [files, selectedPath]);

  const searchEnvelope = searchQuery.data;

  return (
    <>
      <DashboardHeader eyebrow="State and memory" title={surface.title}>
        <p>Browse searchable markdown files exposed by the existing state/memory endpoints.</p>
      </DashboardHeader>
      <div className="memory-browser">
        <Panel
          className="memory-browser__sidebar"
          subtitle="Known state/ files and non-core memory/ files are searchable; core memory is shown read-only when exposed by the tree."
          title="Files"
        >
          <form className="memory-browser__search" onSubmit={submitSearch}>
            <TextInput
              aria-label="Search state and memory files"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search files"
              type="search"
              value={query}
            />
            <Button type="submit" variant="primary">Search</Button>
            <Button
              type="button"
              onClick={() => {
                setQuery("");
                const params = new URLSearchParams(searchParams);
                params.delete("q");
                setSearchParams(params);
              }}
            >
              Clear
            </Button>
          </form>
          <dl className="memory-browser__counts">
            <div><dt>State</dt><dd>{stateCount}</dd></div>
            <div><dt>Memory</dt><dd>{memoryCount}</dd></div>
          </dl>
          {treeQuery.isLoading ? <LoadingState label="Loading file tree" /> : null}
          {treeQuery.isError ? <ApiErrorBlock error={treeQuery.error} title="Tree load failed" /> : null}
          {query.trim() ? (
            <section className="memory-browser__results" aria-label="Search results">
              {searchQuery.isLoading ? <LoadingState label="Searching files" /> : null}
              {searchQuery.isError ? <ApiErrorBlock error={searchQuery.error} title="Search failed" /> : null}
              {searchEnvelope ? (
                <>
                  <p className="memory-browser__muted">
                    {searchResultsCaption(searchEnvelope.data.hits, searchEnvelope.meta?.total, searchEnvelope.meta?.truncated)}
                  </p>
                  <SearchResults hits={searchEnvelope.data.hits} onSelect={selectPath} />
                </>
              ) : null}
            </section>
          ) : tree ? (
            <nav aria-label="State and memory file tree" className="memory-browser__tree">
              <TreeNodeView node={tree} onSelect={selectPath} selectedPath={selectedPath} />
            </nav>
          ) : null}
        </Panel>
        <Panel className="memory-browser__detail" title="Detail">
          <FileDetail path={selectedPath} />
        </Panel>
      </div>
    </>
  );
}
