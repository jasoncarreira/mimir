import { useQuery } from "@tanstack/react-query";
import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ApiError,
  getWikiIndex,
  getWikiPage,
  type WikiDanglingLink,
  type WikiIndexData,
  type WikiPageData,
  type WikiPageSummary
} from "../api";
import type { DashboardSurface } from "../dashboardExtensions";
import { Badge, Button, DashboardHeader, EmptyState, ErrorState, LoadingState, Panel, TextInput } from "../ui";

const CATEGORY_PREFIXES = ["concepts/", "entities/", "topics/"];
const WikiGraphView = React.lazy(() => import("./WikiGraphView"));

function ApiErrorBlock({ error, title }: { error: unknown; title: string }) {
  let detail = error instanceof Error ? error.message : String(error);
  if (error instanceof ApiError && error.body && typeof error.body === "object") {
    const body = error.body as { error?: { code?: string; message?: string } };
    if (body.error?.code) detail = `${body.error.code}: ${body.error.message ?? detail}`;
  }
  return <ErrorState title={title}>{detail}</ErrorState>;
}

function pageKey(page: WikiPageSummary): string {
  return page.path.endsWith(".md") ? page.path.slice(0, -3) : page.path || page.slug;
}

function normalizeWikilinkTarget(target: string): string {
  let normalized = target.trim();
  if (normalized.endsWith(".md")) normalized = normalized.slice(0, -3);
  for (const prefix of CATEGORY_PREFIXES) {
    if (normalized.startsWith(prefix)) return normalized.slice(prefix.length);
  }
  return normalized;
}

function matchesPage(page: WikiPageSummary, selected: string): boolean {
  return selected === page.slug || selected === page.path || selected === pageKey(page);
}

function sortedPages(pages: WikiPageSummary[]): WikiPageSummary[] {
  return [...pages].sort((a, b) => (
    a.category.localeCompare(b.category)
    || a.title.localeCompare(b.title)
    || a.path.localeCompare(b.path)
  ));
}

function healthTone(flag: boolean): "success" | "warning" {
  return flag ? "warning" : "success";
}

function HealthBadges({ index }: { index: WikiIndexData }) {
  const hasOrphans = index.health?.has_orphans ?? index.orphans.length > 0;
  const hasDangling = index.health?.has_dangling_links ?? index.dangling_links.length > 0;
  const hasCollisions = index.health?.has_slug_collisions ?? Object.keys(index.slug_collisions).length > 0;
  return (
    <div className="wiki-health-badges" aria-label="Wiki health flags">
      <Badge tone={healthTone(hasOrphans)}>{hasOrphans ? "orphans" : "no orphans"}</Badge>
      <Badge tone={healthTone(hasDangling)}>{hasDangling ? "dangling links" : "no dangling links"}</Badge>
      <Badge tone={healthTone(hasCollisions)}>{hasCollisions ? "slug collisions" : "no collisions"}</Badge>
    </div>
  );
}

function pageFlags(page: WikiPageSummary) {
  return (
    <span className="wiki-page-flags">
      {page.is_orphan ? <Badge tone="warning">orphan</Badge> : null}
      {page.has_slug_collision ? <Badge tone="danger">collision</Badge> : null}
    </span>
  );
}

function filterPages(pages: WikiPageSummary[], query: string, category: string): WikiPageSummary[] {
  const q = query.trim().toLowerCase();
  return sortedPages(pages).filter((page) => {
    if (category && page.category !== category) return false;
    if (!q) return true;
    return [page.title, page.slug, page.category, page.path]
      .some((field) => field.toLowerCase().includes(q));
  });
}

function linkTargetFor(page: WikiPageSummary) {
  return { pathname: "/wiki", search: `?slug=${encodeURIComponent(pageKey(page))}` };
}

function InlineMarkdown({
  text,
  index
}: {
  text: string;
  index: WikiIndexData;
}) {
  const nodes: React.ReactNode[] = [];
  const wikilinkRe = /\[\[([^\]]+)\]\]|`([^`]+)`|\*\*([^*]+)\*\*/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = wikilinkRe.exec(text))) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    if (match[1] !== undefined) {
      const label = match[1].trim();
      const target = normalizeWikilinkTarget(label);
      const matches = index.pages.filter((page) => page.slug === target || pageKey(page) === target);
      if (matches.length) {
        const page = sortedPages(matches)[0];
        nodes.push(
          <Link className="wiki-wikilink" key={`${match.index}-wiki-${label}`} to={linkTargetFor(page)}>
            {label}
          </Link>
        );
      } else {
        nodes.push(
          <span className="wiki-wikilink wiki-wikilink--dangling" key={`${match.index}-dangling-${label}`} title="Dangling wikilink">
            {label}
          </span>
        );
      }
    } else if (match[2] !== undefined) {
      nodes.push(<code key={`${match.index}-code`}>{match[2]}</code>);
    } else if (match[3] !== undefined) {
      nodes.push(<strong key={`${match.index}-strong`}>{match[3]}</strong>);
    }
    lastIndex = wikilinkRe.lastIndex;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return <>{nodes}</>;
}

function MarkdownView({ markdown, index }: { markdown: string; index: WikiIndexData }) {
  const blocks: React.ReactNode[] = [];
  const lines = markdown.split(/\r?\n/);
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let codeLines: string[] = [];
  let codeLanguage = "";

  function flushParagraph(key: string) {
    if (!paragraph.length) return;
    blocks.push(
      <p key={key}>
        <InlineMarkdown text={paragraph.join(" ")} index={index} />
      </p>
    );
    paragraph = [];
  }

  function flushList(key: string) {
    if (!listItems.length) return;
    blocks.push(
      <ul key={key}>
        {listItems.map((item, indexInList) => (
          <li key={`${key}-${indexInList}`}>
            <InlineMarkdown text={item} index={index} />
          </li>
        ))}
      </ul>
    );
    listItems = [];
  }

  function headingBlock(level: number, text: string, key: string) {
    const content = <InlineMarkdown text={text} index={index} />;
    if (level === 2) return <h2 key={key}>{content}</h2>;
    if (level === 3) return <h3 key={key}>{content}</h3>;
    if (level === 4) return <h4 key={key}>{content}</h4>;
    return <h5 key={key}>{content}</h5>;
  }

  lines.forEach((line, indexInDoc) => {
    const fence = line.match(/^```(.*)$/);
    if (fence) {
      if (codeLines.length || codeLanguage) {
        blocks.push(
          <pre key={`code-${indexInDoc}`} className="wiki-markdown__code">
            <code data-language={codeLanguage || undefined}>{codeLines.join("\n")}</code>
          </pre>
        );
        codeLines = [];
        codeLanguage = "";
      } else {
        flushParagraph(`p-${indexInDoc}`);
        flushList(`ul-${indexInDoc}`);
        codeLanguage = fence[1].trim();
      }
      return;
    }
    if (codeLanguage || codeLines.length) {
      codeLines.push(line);
      return;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    const listItem = line.match(/^\s*[-*]\s+(.+)$/);
    const quote = line.match(/^>\s?(.+)$/);
    if (!line.trim()) {
      flushParagraph(`p-${indexInDoc}`);
      flushList(`ul-${indexInDoc}`);
    } else if (heading) {
      flushParagraph(`p-${indexInDoc}`);
      flushList(`ul-${indexInDoc}`);
      const level = Math.min(heading[1].length + 1, 5);
      blocks.push(headingBlock(level, heading[2], `h-${indexInDoc}`));
    } else if (listItem) {
      flushParagraph(`p-${indexInDoc}`);
      listItems.push(listItem[1]);
    } else if (quote) {
      flushParagraph(`p-${indexInDoc}`);
      flushList(`ul-${indexInDoc}`);
      blocks.push(
        <blockquote key={`q-${indexInDoc}`}>
          <InlineMarkdown text={quote[1]} index={index} />
        </blockquote>
      );
    } else {
      flushList(`ul-${indexInDoc}`);
      paragraph.push(line.trim());
    }
  });
  flushParagraph("p-final");
  flushList("ul-final");
  if (codeLanguage || codeLines.length) {
    blocks.push(
      <pre key="code-final" className="wiki-markdown__code">
        <code data-language={codeLanguage || undefined}>{codeLines.join("\n")}</code>
      </pre>
    );
  }

  return <div className="wiki-markdown">{blocks}</div>;
}

function LinkList({
  title,
  links,
  pages,
  empty
}: {
  title: string;
  links: string[];
  pages: WikiPageSummary[];
  empty: string;
}) {
  return (
    <section className="wiki-link-list">
      <h3>{title}</h3>
      {links.length ? (
        <ul>
          {links.map((link) => {
            const page = pages.find((candidate) => candidate.path === link || candidate.slug === link);
            return (
              <li key={link}>
                {page ? <Link to={linkTargetFor(page)}>{page.title}</Link> : <span>{link}</span>}
                {page ? <small>{page.path}</small> : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="wiki-muted">{empty}</p>
      )}
    </section>
  );
}

function DanglingLinksForPage({
  dangling
}: {
  dangling: WikiDanglingLink[];
}) {
  if (!dangling.length) return null;
  return (
    <section className="wiki-link-list">
      <h3>Dangling</h3>
      <ul>
        {dangling.map((item) => (
          <li key={`${item.source}:${item.line}:${item.target}`}>
            <span className="wiki-wikilink wiki-wikilink--dangling">{item.target}</span>
            <small>line {item.line}</small>
          </li>
        ))}
      </ul>
    </section>
  );
}

function WikiPageReader({
  index,
  selected
}: {
  index: WikiIndexData;
  selected: string;
}) {
  const pageQuery = useQuery({
    enabled: Boolean(selected),
    queryKey: ["wiki-page", selected],
    queryFn: async () => (await getWikiPage(selected)).data
  });
  const page = pageQuery.data as WikiPageData | undefined;
  const danglingForPage = React.useMemo(
    () => index.dangling_links.filter((item) => page && item.source === page.path),
    [index.dangling_links, page]
  );

  if (!selected) {
    return (
      <EmptyState title="No wiki pages">
        The wiki API returned an empty page list.
      </EmptyState>
    );
  }
  if (pageQuery.isLoading) return <LoadingState label="Loading wiki page" />;
  if (pageQuery.isError) return <ApiErrorBlock error={pageQuery.error} title="Page load failed" />;
  if (!page) return <ErrorState title="Page unavailable">No wiki page payload was returned.</ErrorState>;

  return (
    <article className="wiki-reader">
      <header className="wiki-reader__header">
        <div>
          <p className="ui-eyebrow">{page.category}</p>
          <h2>{page.title}</h2>
          <p>{page.path}</p>
        </div>
        {pageFlags(page)}
      </header>
      <MarkdownView markdown={page.markdown} index={index} />
      <div className="wiki-reader__links">
        <LinkList title="Backlinks" links={page.inbound} pages={index.pages} empty="No inbound wiki links." />
        <LinkList title="Outlinks" links={page.outbound} pages={index.pages} empty="No outbound wiki links." />
        <DanglingLinksForPage dangling={danglingForPage} />
      </div>
    </article>
  );
}

export function WikiRoute({ surface }: { surface: DashboardSurface }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = React.useState(searchParams.get("q") || "");
  const [category, setCategory] = React.useState(searchParams.get("category") || "");
  const selectedParam = searchParams.get("slug") || "";
  const view = searchParams.get("view") === "graph" ? "graph" : "reader";
  const indexQuery = useQuery({
    queryKey: ["wiki-index"],
    queryFn: async () => (await getWikiIndex()).data
  });
  const index = indexQuery.data as WikiIndexData | undefined;
  const pages = React.useMemo(() => sortedPages(index?.pages ?? []), [index?.pages]);
  const categories = React.useMemo(
    () => Array.from(new Set(pages.map((page) => page.category))).sort(),
    [pages]
  );
  const selectedPage = pages.find((page) => matchesPage(page, selectedParam)) ?? pages[0];
  const selected = selectedParam || (selectedPage ? pageKey(selectedPage) : "");
  const filteredPages = React.useMemo(
    () => filterPages(pages, query, category),
    [pages, query, category]
  );

  function selectPage(page: WikiPageSummary) {
    const params = new URLSearchParams(searchParams);
    params.set("slug", pageKey(page));
    params.set("view", "reader");
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    if (category) params.set("category", category);
    else params.delete("category");
    setSearchParams(params);
  }

  function openPage(slug: string) {
    const params = new URLSearchParams(searchParams);
    params.set("slug", slug);
    params.set("view", "reader");
    setSearchParams(params);
  }

  function setView(nextView: "reader" | "graph") {
    const params = new URLSearchParams(searchParams);
    params.set("view", nextView);
    if (!params.get("slug") && selected) params.set("slug", selected);
    setSearchParams(params);
  }

  function submitFilters(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const params = new URLSearchParams(searchParams);
    if (query.trim()) params.set("q", query.trim());
    else params.delete("q");
    if (category) params.set("category", category);
    else params.delete("category");
    if (selectedParam) params.set("slug", selectedParam);
    setSearchParams(params);
  }

  return (
    <>
      <DashboardHeader eyebrow="Wiki" title={surface.title}>
        <p>{surface.detail}</p>
      </DashboardHeader>
      <div className="wiki-browser">
        <Panel className="wiki-browser__sidebar" title="Pages">
          <form className="wiki-browser__search" onSubmit={submitFilters}>
            <TextInput
              aria-label="Search wiki pages"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search title, slug, category"
              type="search"
              value={query}
            />
            <select
              aria-label="Filter wiki category"
              className="ui-input"
              onChange={(event) => setCategory(event.target.value)}
              value={category}
            >
              <option value="">All categories</option>
              {categories.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <Button type="submit" variant="primary">Search</Button>
            <Button
              type="button"
              onClick={() => {
                setQuery("");
                setCategory("");
                const params = new URLSearchParams(searchParams);
                params.delete("q");
                params.delete("category");
                setSearchParams(params);
              }}
            >
              Clear
            </Button>
          </form>
          {index ? (
            <>
              <dl className="wiki-browser__counts">
                <div><dt>Pages</dt><dd>{index.page_count}</dd></div>
                <div><dt>Shown</dt><dd>{filteredPages.length}</dd></div>
                <div><dt>Edges</dt><dd>{index.graph.edges.length}</dd></div>
                <div><dt>Dangling</dt><dd>{index.dangling_links.length}</dd></div>
              </dl>
              <HealthBadges index={index} />
            </>
          ) : null}
          {indexQuery.isLoading ? <LoadingState label="Loading wiki index" /> : null}
          {indexQuery.isError ? <ApiErrorBlock error={indexQuery.error} title="Wiki index failed" /> : null}
          {index && !filteredPages.length ? (
            <EmptyState title="No matching pages">
              Try a different title, slug, or category filter.
            </EmptyState>
          ) : null}
          {filteredPages.length ? (
            <nav aria-label="Wiki pages" className="wiki-browser__list">
              {filteredPages.map((page) => (
                <button
                  className={`wiki-browser__page${selectedPage && page.path === selectedPage.path ? " wiki-browser__page--selected" : ""}`}
                  key={page.path}
                  onClick={() => selectPage(page)}
                  type="button"
                >
                  <span>{page.title}</span>
                  <small>{page.slug} · {page.category}</small>
                  {pageFlags(page)}
                </button>
              ))}
            </nav>
          ) : null}
        </Panel>
        <Panel
          className="wiki-browser__detail"
          title={view === "graph" ? "Graph" : "Reader"}
          actions={index ? (
            <div className="wiki-view-toggle" aria-label="Wiki view">
              <Button onClick={() => setView("reader")} variant={view === "reader" ? "primary" : "secondary"}>Reader</Button>
              <Button onClick={() => setView("graph")} variant={view === "graph" ? "primary" : "secondary"}>Graph</Button>
            </div>
          ) : null}
        >
          {index ? (
            view === "graph" ? (
              <React.Suspense fallback={<LoadingState label="Loading wiki graph" />}>
                <WikiGraphView index={index} onOpenPage={openPage} selected={selected} />
              </React.Suspense>
            ) : (
              <WikiPageReader index={index} selected={selected} />
            )
          ) : indexQuery.isLoading ? (
            <LoadingState label="Loading wiki reader" />
          ) : indexQuery.isError ? null : (
            <EmptyState title="No wiki pages">
              The wiki API returned an empty page list.
            </EmptyState>
          )}
        </Panel>
      </div>
    </>
  );
}
