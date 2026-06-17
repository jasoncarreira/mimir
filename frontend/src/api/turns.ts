import { requestJson, type RequestJsonOptions } from "./http";

export type TurnTrigger =
  | "user_message"
  | "scheduled_tick"
  | "saga_session_end"
  | "poller"
  | "claude_code_spawn"
  | "shell_job_complete"
  | "react_received"
  | string;

export interface TurnReasoningEvent {
  type: "reasoning";
  content?: string;
  t_ms?: number;
}

export interface TurnToolCallEvent {
  type: "tool_call";
  id?: string;
  name?: string;
  args?: unknown;
  t_ms?: number;
}

export interface TurnToolResultEvent {
  type: "tool_result";
  id?: string;
  content?: string;
  is_error?: boolean;
  t_ms?: number;
}

export type TurnEvent =
  | TurnReasoningEvent
  | TurnToolCallEvent
  | TurnToolResultEvent
  | ({ type: string; t_ms?: number } & Record<string, unknown>);

export interface SagaCall {
  call_type?: string;
  args?: unknown;
  result?: unknown;
  error?: string;
  latency_ms?: number;
  t_ms?: number;
}

export type InjectedInput = string | { text?: string; t_ms?: number };

export interface TurnRecord {
  turn_id?: string;
  ts?: string;
  trigger?: TurnTrigger;
  kind?: string;
  channel_id?: string;
  duration_ms?: number;
  input?: string;
  output?: string;
  error?: string;
  events?: TurnEvent[];
  saga_calls?: SagaCall[];
  injected_inputs?: InjectedInput[];
  usage?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface TurnsResponse {
  turns: TurnRecord[];
}

export interface ListTurnsParams {
  limit?: number;
  after?: string;
  before?: string;
}

export interface EventLogRecord extends Record<string, unknown> {
  timestamp?: string;
  type?: string;
  channel_id?: string;
  trigger?: string;
}

export interface EventsResponse {
  events: EventLogRecord[];
}

export interface ListEventsParams {
  since?: string;
  type?: string | string[];
  limit?: number;
}

export function listTurns(
  params: ListTurnsParams = {},
  options: Pick<RequestJsonOptions, "apiKey" | "signal"> = {}
): Promise<TurnsResponse> {
  return requestJson<TurnsResponse>("/api/turns", {
    ...options,
    query: {
      limit: params.limit,
      after: params.after,
      before: params.before
    }
  });
}

export function listEvents(
  params: ListEventsParams = {},
  options: Pick<RequestJsonOptions, "apiKey" | "signal"> = {}
): Promise<EventsResponse> {
  return requestJson<EventsResponse>("/api/events", {
    ...options,
    query: {
      since: params.since,
      type: params.type,
      limit: params.limit
    }
  });
}
