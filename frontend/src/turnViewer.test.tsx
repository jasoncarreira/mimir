import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { listTurns } from "./api";
import { buildTurnTimeline, TurnDetail } from "./turnViewer";
import type { TurnRecord } from "./api";

const representativeTurn: TurnRecord = {
  turn_id: "turn-test-1",
  ts: "2026-06-18T12:00:00Z",
  trigger: "user_message",
  kind: "user_message",
  channel_id: "web-default",
  input: "Inspect the issue.",
  output: "Done.",
  duration_ms: 1234,
  events: [
    { type: "reasoning", content: "Need to inspect the repo.", t_ms: 10 },
    {
      type: "tool_call",
      id: "call-1",
      name: "exec_command",
      args: { cmd: "rg turn_viewer" },
      t_ms: 20
    },
    {
      type: "tool_result",
      id: "call-1",
      content: "mimir/turn_viewer.html",
      is_error: false,
      t_ms: 30
    },
    {
      type: "algedonic_feedback",
      valence: -0.1,
      arousal: 0.5,
      detail: "slow path",
      t_ms: 40
    }
  ],
  saga_calls: [
    {
      call_type: "query",
      args: { q: "turn viewer" },
      result: { atoms: 1 },
      latency_ms: 12,
      t_ms: 25
    }
  ],
  injected_inputs: [{ t_ms: 35, text: "Also keep old behavior." }],
  usage: { input_tokens: 10, output_tokens: 5 }
};

test("listTurns calls the v1 turns endpoint with pagination params", async () => {
  const calls: string[] = [];
  const fetchImpl: typeof fetch = async (input) => {
    calls.push(String(input));
    return new Response(JSON.stringify({
      ok: true,
      version: "v1",
      data: { turns: [representativeTurn] },
      meta: { cursor: "turn-test-1", limit: 1, total: 1, truncated: false }
    }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  const envelope = await listTurns(
    { limit: 1, before: "turn-test-2" },
    { baseUrl: "http://mimir.test", apiKey: "secret", fetchImpl }
  );

  assert.equal(calls[0], "http://mimir.test/api/v1/turns?limit=1&before=turn-test-2");
  assert.equal(envelope.data.turns[0]?.turn_id, "turn-test-1");
  assert.equal(envelope.meta?.cursor, "turn-test-1");
});

test("TurnDetail renders structured sections for representative tool activity", () => {
  const timeline = buildTurnTimeline(representativeTurn);
  assert.deepEqual(timeline.map((entry) => entry.kind), [
    "event",
    "event",
    "saga",
    "event",
    "injected",
    "event"
  ]);

  const html = renderToStaticMarkup(<TurnDetail turn={representativeTurn} />);
  assert.match(html, /Reasoning 1/);
  assert.match(html, /Tool call: exec_command/);
  assert.match(html, /Tool result/);
  assert.match(html, /Saga query/);
  assert.match(html, /Mid-turn user message/);
  assert.match(html, /Feedback: algedonic_feedback/);
  assert.match(html, /Metadata/);
});
