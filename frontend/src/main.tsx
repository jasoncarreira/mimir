import React from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const legacySurfaces = [
  { href: "/turns", label: "Turns", detail: "Turn viewer" },
  { href: "/ops", label: "Ops", detail: "Operations dashboard" },
  { href: "/saga", label: "Saga", detail: "Memory atoms" },
  { href: "/state", label: "State", detail: "Memory files" }
];

function App() {
  return (
    <main className="app-shell">
      <section className="intro" aria-labelledby="app-title">
        <p className="eyebrow">React foundation</p>
        <h1 id="app-title">Mimir App</h1>
        <p>
          This route is the new React mount point. Existing operator pages stay
          available while features migrate into this app.
        </p>
      </section>

      <nav className="surface-grid" aria-label="Existing operator pages">
        {legacySurfaces.map((surface) => (
          <a className="surface-link" href={surface.href} key={surface.href}>
            <span>{surface.label}</span>
            <small>{surface.detail}</small>
          </a>
        ))}
      </nav>
    </main>
  );
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("React root element not found");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
