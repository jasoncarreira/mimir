import React from "react";
import { createRoot } from "react-dom/client";
import { SkinProvider, useSkin } from "./skins/SkinProvider";
import {
  Badge,
  Button,
  DashboardHeader,
  DashboardShell,
  ErrorState,
  LoadingState,
  NavList,
  Panel,
  TextInput,
} from "./ui";
import { DashboardUiExamples } from "./ui/examples";
import "./styles.css";

type Bootstrap = {
  auth: {
    required: boolean;
    scheme: string;
    storage: string;
  };
  server: {
    web_host: string;
    public_bind: boolean;
    unauthenticated_allowed: boolean;
  };
  stream_auth: {
    shape: string;
    header: string;
    native_eventsource_supported_when_auth_required: boolean;
  };
};

const legacySurfaces = [
  { href: "/turns", label: "Turns", detail: "Turn viewer" },
  { href: "/ops", label: "Ops", detail: "Operations dashboard" },
  { href: "/saga", label: "Saga", detail: "Memory atoms" },
  { href: "/state", label: "State", detail: "Memory files" }
];

const authStorageKey = "mimir.api_key";

const AuthContext = React.createContext<{
  bootstrap: Bootstrap | null;
  apiKeyPresent: boolean;
  status: "loading" | "ready" | "error";
  error: string | null;
  setApiKey: (value: string) => void;
  clearApiKey: () => void;
} | null>(null);

function readStoredKey() {
  try {
    return window.localStorage.getItem(authStorageKey) || "";
  } catch {
    return "";
  }
}

function AuthProvider({ children }: { children: React.ReactNode }) {
  const [bootstrap, setBootstrap] = React.useState<Bootstrap | null>(null);
  const [apiKeyPresent, setApiKeyPresent] = React.useState(Boolean(readStoredKey()));
  const [status, setStatus] = React.useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    fetch("/api/web/bootstrap", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<Bootstrap>;
      })
      .then((payload) => {
        setBootstrap(payload);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
        setStatus("error");
      });
  }, []);

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

  const clearApiKey = React.useCallback(() => setApiKey(""), [setApiKey]);

  return (
    <AuthContext.Provider
      value={{ bootstrap, apiKeyPresent, status, error, setApiKey, clearApiKey }}
    >
      {children}
    </AuthContext.Provider>
  );
}

function useAuth() {
  const value = React.useContext(AuthContext);
  if (!value) throw new Error("AuthProvider missing");
  return value;
}

function BootstrapRoute() {
  const { bootstrap, apiKeyPresent, status, error, setApiKey, clearApiKey } = useAuth();
  const [entry, setEntry] = React.useState("");
  const requiresKey = bootstrap?.auth.required ?? false;
  const signedIn = !requiresKey || apiKeyPresent;

  return (
    <Panel
      actions={<Badge tone={signedIn ? "success" : "warning"}>{signedIn ? "ready" : "locked"}</Badge>}
      aria-labelledby="auth-title"
      subtitle={
        status === "loading"
          ? "Loading server auth policy."
          : status === "error"
            ? `Bootstrap failed: ${error}`
            : `${requiresKey ? "Protected" : "Local unauthenticated"} server on ${bootstrap?.server.web_host || "default host"}.`
      }
      title={<span id="auth-title">{signedIn ? "Ready" : "API key required"}</span>}
    >
      {status === "loading" ? <LoadingState label="Loading auth policy" /> : null}
      {status === "error" ? <ErrorState title="Bootstrap failed">{error}</ErrorState> : null}

      {status === "ready" ? (
        <>
          {requiresKey ? (
            <form
              className="auth-form"
              onSubmit={(event) => {
                event.preventDefault();
                setApiKey(entry);
                setEntry("");
              }}
            >
              <TextInput
                aria-label="MIMIR_API_KEY"
                autoComplete="off"
                placeholder={apiKeyPresent ? "Key stored in this browser" : "MIMIR_API_KEY"}
                type="password"
                value={entry}
                onChange={(event) => setEntry(event.target.value)}
              />
              <Button type="submit" variant="primary">Save</Button>
              <Button type="button" onClick={clearApiKey}>Clear</Button>
            </form>
          ) : null}

          <dl className="facts-grid">
            <div><dt>Browser key</dt><dd>{apiKeyPresent ? "stored" : "not stored"}</dd></div>
            <div><dt>Bind</dt><dd>{bootstrap?.server.public_bind ? "public" : "localhost"}</dd></div>
            <div><dt>Streams</dt><dd>{bootstrap?.stream_auth.shape || "loading"}</dd></div>
          </dl>
        </>
      ) : null}
    </Panel>
  );
}

function DirectoryRoute() {
  return (
    <Panel
      subtitle="Current operator pages stay on their existing server routes while React surfaces migrate incrementally."
      title="Operator surfaces"
    >
      <NavList label="Existing operator pages" items={legacySurfaces} />
    </Panel>
  );
}

function App() {
  const { skin } = useSkin();

  return (
    <DashboardShell>
      <DashboardHeader eyebrow={skin.name} title="Mimir App">
        <p>
          Central browser bootstrap for operator auth and migrated web surfaces.
        </p>
      </DashboardHeader>

      <div className="route-grid">
        <BootstrapRoute />
        <DirectoryRoute />
      </div>
      <DashboardUiExamples />
    </DashboardShell>
  );
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("React root element not found");
}

createRoot(root).render(
  <React.StrictMode>
    <AuthProvider>
      <SkinProvider>
        <App />
      </SkinProvider>
    </AuthProvider>
  </React.StrictMode>
);
