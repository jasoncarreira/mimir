import React from "react";
import { useQuery } from "@tanstack/react-query";
import type { Bootstrap } from "./types";

const authStorageKey = "mimir.api_key";

function readStoredKey() {
  try {
    return window.localStorage.getItem(authStorageKey) || "";
  } catch {
    return "";
  }
}

async function fetchBootstrap(): Promise<Bootstrap> {
  const response = await fetch("/api/web/bootstrap", { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<Bootstrap>;
}

export function AuthPanel() {
  const { data: bootstrap, status, error } = useQuery({
    queryKey: ["web-bootstrap"],
    queryFn: fetchBootstrap
  });
  const [apiKeyPresent, setApiKeyPresent] = React.useState(Boolean(readStoredKey()));
  const [entry, setEntry] = React.useState("");
  const requiresKey = bootstrap?.auth.required ?? false;
  const signedIn = !requiresKey || apiKeyPresent;

  const setApiKey = React.useCallback((value: string) => {
    const trimmed = value.trim();
    try {
      if (trimmed) window.localStorage.setItem(authStorageKey, trimmed);
      else window.localStorage.removeItem(authStorageKey);
    } catch {
      // Storage may be blocked; visible status still reflects this attempt.
    }
    setApiKeyPresent(Boolean(trimmed));
  }, []);

  return (
    <section className="auth-panel" aria-labelledby="auth-title">
      <div>
        <p className="eyebrow">Status</p>
        <h2 id="auth-title">{signedIn ? "Ready" : "API key required"}</h2>
        <p>
          {status === "pending"
            ? "Loading server auth policy."
            : status === "error"
              ? `Bootstrap failed: ${error instanceof Error ? error.message : String(error)}`
              : `${requiresKey ? "Protected" : "Local unauthenticated"} server on ${bootstrap?.server.web_host || "default host"}.`}
        </p>
      </div>

      {requiresKey ? (
        <form
          className="key-form"
          onSubmit={(event) => {
            event.preventDefault();
            setApiKey(entry);
            setEntry("");
          }}
        >
          <input
            aria-label="MIMIR_API_KEY"
            autoComplete="off"
            placeholder={apiKeyPresent ? "Key stored in this browser" : "MIMIR_API_KEY"}
            type="password"
            value={entry}
            onChange={(event) => setEntry(event.target.value)}
          />
          <button type="submit">Save</button>
          <button type="button" onClick={() => setApiKey("")}>Clear</button>
        </form>
      ) : null}

      <dl className="auth-facts">
        <div><dt>Browser key</dt><dd>{apiKeyPresent ? "stored" : "not stored"}</dd></div>
        <div><dt>Bind</dt><dd>{bootstrap?.server.public_bind ? "public" : "localhost"}</dd></div>
        <div><dt>Streams</dt><dd>{bootstrap?.stream_auth.shape || "loading"}</dd></div>
      </dl>
    </section>
  );
}
