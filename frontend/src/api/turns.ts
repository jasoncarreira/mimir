import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  InjectedInput,
  ListMeta,
  SagaCall,
  SessionsData,
  TurnEventBase,
  TurnRecord,
  TurnsData,
  TurnTrigger
} from "./generated/contracts";

export type {
  InjectedInput,
  SagaCall,
  TurnEventBase as TurnEvent,
  TurnRecord,
  TurnTrigger,
  SessionsData
};

export type TurnsResponse = TurnsData;

export interface ListTurnsParams {
  limit?: number;
  after?: string;
  before?: string;
}

export interface ListSessionsParams {
  limit?: number;
  q?: string;
  channel?: string;
  trigger?: string;
  from?: string;
  to?: string;
}

export function normalizeListTurnsParams(params: ListTurnsParams = {}): ListTurnsParams {
  const limit = typeof params.limit === "number" && Number.isFinite(params.limit)
    ? Math.max(1, Math.min(500, Math.trunc(params.limit)))
    : undefined;
  return {
    limit,
    after: params.after?.trim() || undefined,
    before: params.before?.trim() || undefined
  };
}

export function listTurns(
  params: ListTurnsParams = {},
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<TurnsData, ListMeta>> {
  const normalized = normalizeListTurnsParams(params);
  return apiFetchEnvelope<TurnsData, ListMeta>(
    `/api/v1/turns${buildQuery({
      limit: normalized.limit,
      after: normalized.after,
      before: normalized.before
    })}`,
    options
  );
}

export function normalizeListSessionsParams(params: ListSessionsParams = {}): ListSessionsParams {
  const limit = typeof params.limit === "number" && Number.isFinite(params.limit)
    ? Math.max(1, Math.min(500, Math.trunc(params.limit)))
    : undefined;
  return {
    limit,
    q: params.q?.trim() || undefined,
    channel: params.channel?.trim() || undefined,
    trigger: params.trigger?.trim() || undefined,
    from: params.from?.trim() || undefined,
    to: params.to?.trim() || undefined
  };
}

export function listSessions(
  params: ListSessionsParams = {},
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<SessionsData, ListMeta>> {
  const normalized = normalizeListSessionsParams(params);
  return apiFetchEnvelope<SessionsData, ListMeta>(
    `/api/v1/sessions${buildQuery({
      limit: normalized.limit,
      q: normalized.q,
      channel: normalized.channel,
      trigger: normalized.trigger,
      from: normalized.from,
      to: normalized.to
    })}`,
    options
  );
}
