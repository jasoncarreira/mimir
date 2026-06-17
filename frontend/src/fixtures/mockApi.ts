import {
  chatPostFixture,
  eventsFixture,
  memoryChannelsFixture,
  memoryFileFixture,
  memorySearchFixture,
  memoryTreeFixture,
  opsFixture,
  sagaActivationHistFixture,
  sagaAtomFixture,
  sagaClustersFixture,
  sagaRecentFixture,
  sagaSearchFixture,
  sagaStatsFixture,
  turnsFixture
} from "./apiFixtures";

type MockHandler = (url: URL, init?: RequestInit) => unknown;

const handlers: Record<string, MockHandler> = {
  "GET /api/turns": () => turnsFixture,
  "GET /api/events": () => eventsFixture,
  "GET /api/ops": () => opsFixture,
  "POST /chat": () => chatPostFixture,

  "GET /api/saga": (url) => {
    switch (url.searchParams.get("view") || "recent") {
      case "atom":
        return sagaAtomFixture;
      case "stats":
        return sagaStatsFixture;
      case "search":
        return sagaSearchFixture;
      case "activation_hist":
        return sagaActivationHistFixture;
      case "clusters":
        return sagaClustersFixture;
      case "recent":
      default:
        return sagaRecentFixture;
    }
  },

  "GET /api/memory": (url) => {
    switch (url.searchParams.get("view") || "tree") {
      case "file":
        return memoryFileFixture;
      case "search":
        return memorySearchFixture;
      case "channels":
        return memoryChannelsFixture;
      case "tree":
      default:
        return memoryTreeFixture;
    }
  }
};

export function createMockFetch(
  overrides: Record<string, MockHandler> = {}
): typeof fetch {
  const allHandlers = { ...handlers, ...overrides };

  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const rawUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(rawUrl, "http://mimir.test");
    const method = (init?.method || "GET").toUpperCase();
    const handler = allHandlers[`${method} ${url.pathname}`];

    if (!handler) {
      return new Response(JSON.stringify({ error: "mock route not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" }
      });
    }

    return new Response(JSON.stringify(handler(url, init)), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    });
  };
}
