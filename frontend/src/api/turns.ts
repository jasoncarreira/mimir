import { apiFetchJson, buildQuery, type ApiClientOptions } from "./http";

export type TurnTrigger =
  | "user_message"
  | "scheduled_tick"
  | "saga_session_end"
  | "poller"
  | "claude_code_spawn"
  | "shell_job_complete"
  | "react_received"
  | string;

export interface TurnEventBase {
  type: string;
  t_ms?: number | null;
}

export interface TurnReasoningEvent extends TurnEventBase {
  type: "reasoning";
  content?: string;
}

export interface TurnToolCallEvent extends TurnEventBase {
  type: "tool_call";
  id?: string;
  name?: string;
  args?: unknown;
}

export interface TurnToolResultEvent extends TurnEventBase {
  type: "tool_result";
  id?: string;
  content?: string;
  is_error?: boolean;
}

export type TurnEvent =
  | TurnReasoningEvent
  | TurnToolCallEvent
  | TurnToolResultEvent
  | TurnEventBase;

export interface SagaCall {
  call_type?: string;
  args?: unknown;
  result?: unknown;
  error?: string | null;
  latency_ms?: number | null;
  t_ms?: number | null;
}

export interface InjectedInput {
  t_ms?: number | null;
  text: string;
}

export interface TurnRecord {
  turn_id?: string;
  ts?: string;
  trigger?: TurnTrigger;
  kind?: string | null;
  channel_id?: string | null;
  input?: string;
  output?: string;
  error?: string | null;
  duration_ms?: number | null;
  events?: TurnEvent[];
  saga_calls?: SagaCall[];
  injected_inputs?: Array<InjectedInput | string>;
  usage?: Record<string, unknown>;
}

export interface TurnsResponse {
  turns: TurnRecord[];
}

export interface ListTurnsParams {
  limit?: number;
  after?: string;
  before?: string;
}

export function listTurns(
  params: ListTurnsParams = {},
  options?: ApiClientOptions
): Promise<TurnsResponse> {
  return apiFetchJson<TurnsResponse>(
    `/api/turns${buildQuery({ ...params })}`,
    options
  );
}
