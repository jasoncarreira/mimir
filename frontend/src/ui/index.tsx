import React from "react";

type DivProps = Omit<React.HTMLAttributes<HTMLDivElement>, "title">;

export function DashboardShell({ className = "", ...props }: DivProps) {
  return <main className={`ui-shell ${className}`.trim()} {...props} />;
}

export function DashboardHeader({
  eyebrow,
  title,
  children
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <header className="ui-header">
      {eyebrow ? <p className="ui-eyebrow">{eyebrow}</p> : null}
      <h1>{title}</h1>
      {children ? <div className="ui-header__body">{children}</div> : null}
    </header>
  );
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className = "",
  ...props
}: DivProps & {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <section className={`ui-panel ${className}`.trim()} {...props}>
      {title || subtitle || actions ? (
        <div className="ui-panel__header">
          <div>
            {title ? <h2>{title}</h2> : null}
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          {actions ? <div className="ui-panel__actions">{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function Card({
  title,
  children,
  className = "",
  ...props
}: DivProps & { title?: React.ReactNode }) {
  return (
    <article className={`ui-card ${className}`.trim()} {...props}>
      {title ? <h3>{title}</h3> : null}
      {children}
    </article>
  );
}

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost";
}) {
  return (
    <button
      className={`ui-button ui-button--${variant} ${className}`.trim()}
      {...props}
    />
  );
}

export function TextInput({
  className = "",
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`ui-input ${className}`.trim()} {...props} />;
}

export type TabItem = {
  id: string;
  label: React.ReactNode;
  panel: React.ReactNode;
};

export function Tabs({
  items,
  defaultValue,
  label
}: {
  items: TabItem[];
  defaultValue?: string;
  label: string;
}) {
  const [activeId, setActiveId] = React.useState(defaultValue ?? items[0]?.id);
  const tabsRef = React.useRef<Array<HTMLButtonElement | null>>([]);
  const activeIndex = Math.max(
    0,
    items.findIndex((item) => item.id === activeId)
  );

  function moveFocus(nextIndex: number) {
    const normalized = (nextIndex + items.length) % items.length;
    tabsRef.current[normalized]?.focus();
    setActiveId(items[normalized].id);
  }

  return (
    <div className="ui-tabs">
      <div aria-label={label} className="ui-tabs__list" role="tablist">
        {items.map((item, index) => (
          <button
            aria-controls={`${item.id}-panel`}
            aria-selected={item.id === activeId}
            className="ui-tabs__tab"
            id={`${item.id}-tab`}
            key={item.id}
            onClick={() => setActiveId(item.id)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight") {
                event.preventDefault();
                moveFocus(index + 1);
              } else if (event.key === "ArrowLeft") {
                event.preventDefault();
                moveFocus(index - 1);
              } else if (event.key === "Home") {
                event.preventDefault();
                moveFocus(0);
              } else if (event.key === "End") {
                event.preventDefault();
                moveFocus(items.length - 1);
              }
            }}
            ref={(node) => {
              tabsRef.current[index] = node;
            }}
            role="tab"
            tabIndex={item.id === activeId ? 0 : -1}
            type="button"
          >
            {item.label}
          </button>
        ))}
      </div>
      {items.map((item, index) => (
        <div
          aria-labelledby={`${item.id}-tab`}
          className="ui-tabs__panel"
          hidden={index !== activeIndex}
          id={`${item.id}-panel`}
          key={item.id}
          role="tabpanel"
          tabIndex={0}
        >
          {item.panel}
        </div>
      ))}
    </div>
  );
}

export function NavList({
  label,
  items
}: {
  label: string;
  items: Array<{ href: string; label: React.ReactNode; detail?: React.ReactNode }>;
}) {
  return (
    <nav aria-label={label} className="ui-nav-list">
      {items.map((item) => (
        <a className="ui-nav-link" href={item.href} key={item.href}>
          <span>{item.label}</span>
          {item.detail ? <small>{item.detail}</small> : null}
        </a>
      ))}
    </nav>
  );
}

export function DataTable({
  columns,
  rows,
  caption
}: {
  columns: Array<{ key: string; header: React.ReactNode }>;
  rows: Array<Record<string, React.ReactNode>>;
  caption?: React.ReactNode;
}) {
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        {caption ? <caption>{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} scope="col">
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column) => (
                <td key={column.key}>{row[column.key]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function CodeBlock({
  code,
  language,
  title
}: {
  code: string;
  language?: string;
  title?: React.ReactNode;
}) {
  return (
    <figure className="ui-code">
      {title ? <figcaption>{title}</figcaption> : null}
      <pre>
        <code data-language={language}>{code}</code>
      </pre>
    </figure>
  );
}

export function LogBlock({ lines }: { lines: string[] }) {
  return (
    <ol className="ui-log" aria-label="Log output">
      {lines.map((line, index) => (
        <li key={`${index}-${line}`}>{line}</li>
      ))}
    </ol>
  );
}

export function Badge({
  tone = "neutral",
  children
}: {
  tone?: "neutral" | "info" | "success" | "warning" | "danger";
  children: React.ReactNode;
}) {
  return <span className={`ui-badge ui-badge--${tone}`}>{children}</span>;
}

export function Timeline({
  items
}: {
  items: Array<{ title: React.ReactNode; meta?: React.ReactNode; detail?: React.ReactNode }>;
}) {
  return (
    <ol className="ui-timeline">
      {items.map((item, index) => (
        <li key={index}>
          <div>
            <strong>{item.title}</strong>
            {item.meta ? <span>{item.meta}</span> : null}
          </div>
          {item.detail ? <p>{item.detail}</p> : null}
        </li>
      ))}
    </ol>
  );
}

export function EmptyState({
  title,
  children,
  action
}: {
  title: React.ReactNode;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="ui-state ui-state--empty">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  children
}: {
  title?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className="ui-state ui-state--error" role="alert">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
    </div>
  );
}

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="ui-state ui-state--loading" aria-live="polite">
      <span aria-hidden="true" className="ui-spinner" />
      <span>{label}</span>
    </div>
  );
}

function getFocusable(container: HTMLElement | null) {
  if (!container) return [];
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((node) => !node.hasAttribute("hidden"));
}

function useFocusTrap(active: boolean, ref: React.RefObject<HTMLElement | null>) {
  React.useEffect(() => {
    if (!active) return;
    const previous = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusable = getFocusable(ref.current);
    (focusable[0] ?? ref.current)?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Tab") return;
      const nodes = getFocusable(ref.current);
      if (nodes.length === 0) {
        event.preventDefault();
        return;
      }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus();
    };
  }, [active, ref]);
}

export function Dialog({
  open,
  title,
  children,
  onClose
}: {
  open: boolean;
  title: React.ReactNode;
  children?: React.ReactNode;
  onClose: () => void;
}) {
  const dialogRef = React.useRef<HTMLDivElement>(null);
  useFocusTrap(open, dialogRef);
  if (!open) return null;

  return (
    <div className="ui-overlay" onMouseDown={onClose}>
      <section
        aria-labelledby="ui-dialog-title"
        aria-modal="true"
        className="ui-dialog"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="ui-dialog__header">
          <h2 id="ui-dialog-title">{title}</h2>
          <Button aria-label="Close dialog" onClick={onClose} variant="ghost">
            x
          </Button>
        </div>
        {children}
      </section>
    </div>
  );
}

export function Drawer({
  open,
  title,
  side = "right",
  children,
  onClose
}: {
  open: boolean;
  title: React.ReactNode;
  side?: "left" | "right";
  children?: React.ReactNode;
  onClose: () => void;
}) {
  const drawerRef = React.useRef<HTMLDivElement>(null);
  useFocusTrap(open, drawerRef);
  if (!open) return null;

  return (
    <div className="ui-overlay ui-overlay--drawer" onMouseDown={onClose}>
      <aside
        aria-labelledby="ui-drawer-title"
        aria-modal="true"
        className={`ui-drawer ui-drawer--${side}`}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
        onMouseDown={(event) => event.stopPropagation()}
        ref={drawerRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="ui-dialog__header">
          <h2 id="ui-drawer-title">{title}</h2>
          <Button aria-label="Close drawer" onClick={onClose} variant="ghost">
            x
          </Button>
        </div>
        {children}
      </aside>
    </div>
  );
}

export function ToastRegion({
  toasts
}: {
  toasts: Array<{ id: string; tone?: "info" | "success" | "warning" | "danger"; message: React.ReactNode }>;
}) {
  return (
    <div aria-live="polite" className="ui-toasts" role="status">
      {toasts.map((toast) => (
        <div className={`ui-toast ui-toast--${toast.tone ?? "info"}`} key={toast.id}>
          {toast.message}
        </div>
      ))}
    </div>
  );
}
