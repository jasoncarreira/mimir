/*
 * React parity API inventory.
 *
 * This file intentionally documents only the endpoints consumed by the
 * existing static pages and web-chat bridge. It is a hand-written parity map,
 * not the generated/OpenAPI-style platform contract reserved for #548.
 *
 * Shared auth: the legacy HTML shells are public enough to prompt for a key;
 * data routes send `X-API-Key: <MIMIR_API_KEY>` when the server requires it.
 *
 * Turns:
 *   GET /api/turns
 *   GET /api/turns?limit=200
 *   GET /api/turns?after=t-1
 *   GET /api/turns?before=t-9&limit=200
 *   -> { "turns": [TurnRecord] }
 *
 * Events:
 *   GET /api/events?since=2026-01-01T00:00:00Z&type=tool_call&limit=50
 *   -> { "events": [{ "timestamp": "...", "type": "tool_call", ... }] }
 *
 * Ops:
 *   GET /api/ops?days=7
 *   -> OpsPayload with summary cards, event histograms, shell/tool stats,
 *      chainlink issue envelope, usage histories, failures, and backlog items.
 *
 * SAGA:
 *   GET /api/saga?view=stats
 *   GET /api/saga?view=recent&channel=web-default&limit=50
 *   GET /api/saga?view=atom&id=atom-1
 *   GET /api/saga?view=search&q=context&channel=web-default&limit=100
 *   GET /api/saga?view=activation_hist&days=7
 *   GET /api/saga?view=clusters&sample_size=3
 *   POST /api/saga/sql { "sql": "SELECT id FROM atoms LIMIT 10" }
 *
 * State/memory:
 *   GET /api/memory?view=tree
 *   GET /api/memory?view=file&path=memory/core/00-identity.md
 *   GET /api/memory?view=search&q=heartbeat
 *   GET /api/memory?view=channels
 *
 * Web chat:
 *   POST /chat
 *     { "channel_id": "web-default", "content": "hello", "author": "alice",
 *       "author_id": "u-1", "msg_id": "client-1", "extra": {} }
 *     -> { "ok": true, "channel_id": "web-default" }
 *   GET /chat/stream
 *     data: {"channel_id":"web-default","text":"hi","message_id":"m-1","attachments":[]}
 *     data: {"_event":"react","channel_id":"web-default","message_id":"m-1","emoji":"👍"}
 */

export interface ParityApiBlocker {
  surface: "chat" | "turn-details";
  endpoint: string;
  missingFields: string[];
  neededFor: string;
  suggestedHandle: string;
}

export const parityApiBlockers: ParityApiBlocker[] = [
  {
    surface: "chat",
    endpoint: "POST /chat",
    missingFields: ["msg_id/source_id echo", "accepted_at"],
    neededFor:
      "Optimistic chat send reconciliation without relying on client-only pending state.",
    suggestedHandle: "chat-send-ack-fields"
  },
  {
    surface: "chat",
    endpoint: "GET /chat/stream",
    missingFields: ["_event on normal message payloads", "created_at", "final"],
    neededFor:
      "A stable chat timeline and message/update distinction before the broader SSE union in #542.",
    suggestedHandle: "chat-stream-message-envelope"
  },
  {
    surface: "chat",
    endpoint: "GET /chat/history or GET /api/turns?channel_id=...",
    missingFields: ["endpoint", "channel_id filter", "message author/timestamp history"],
    neededFor:
      "Opening the React chat route with prior messages instead of an empty live-only stream.",
    suggestedHandle: "chat-history-backfill"
  }
];

export * from "./chat";
export * from "./memory";
export * from "./ops";
export * from "./saga";
export * from "./turns";
