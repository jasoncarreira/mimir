import type { ApiClient } from "./http";
import { withQuery } from "./http";

export interface TurnEventBase {
  type: string;
  t_ms?: number;
}

export interface ReasoningTurnEvent extends TurnEventBase {
  type: "reasoning";
  content?: string;
}

export interface ToolCallTurnEvent extends TurnEventBase {
  type: "tool_call";
  id?: string;
  name?: string;
  args?: unknown;
}

export interface ToolResultTurnEvent extends TurnEventBase {
  type: "tool_result";
  id?: string;
  content?: string;
  is_error?: boolean;
}

export type TurnEvent =
  | ReasoningTurnEvent
  | ToolCallTurnEvent
  | ToolResultTurnEvent
  | TurnEventBase;

export interface SagaCall {
  call_type?: string;
  latency_ms?: number;
  t_ms?: number;
  args?: unknown;
  result?: unknown;
  error?: string;
}

export interface InjectedInput {
  t_ms?: number | null;
  text: string;
}

export interface TurnRecord {
  turn_id?: string;
  ts?: string;
  trigger?: string;
  kind?: string;
  channel_id?: string;
  input?: string;
  output?: string;
  error?: string;
  duration_ms?: number;
  events?: TurnEvent[];
  saga_calls?: SagaCall[];
  injected_inputs?: Array<InjectedInput | string>;
  usage?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface TurnsResponse {
  turns: TurnRecord[];
}

export interface EventsResponse {
  events: RawEventRecord[];
}

export interface RawEventRecord {
  timestamp?: string;
  type?: string;
  [key: string]: unknown;
}

export interface TurnsQuery {
  after?: string;
  before?: string;
  limit?: number;
}

export interface EventsQuery {
  since?: string;
  type?: string | string[];
  limit?: number;
}

export function createTurnsClient(api: ApiClient) {
  return {
    listTurns(query: TurnsQuery = {}): Promise<TurnsResponse> {
      return api.requestJson<TurnsResponse>(
        withQuery("/api/turns", { ...query })
      );
    },

    listEvents(query: EventsQuery = {}): Promise<EventsResponse> {
      const params: Record<string, string | number | undefined> = {
        since: query.since,
        limit: query.limit
      };
      if (Array.isArray(query.type)) {
        params.type = query.type.join(",");
      } else {
        params.type = query.type;
      }
      return api.requestJson<EventsResponse>(withQuery("/api/events", params));
    }
  };
}
