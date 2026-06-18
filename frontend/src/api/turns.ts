import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  InjectedInput,
  ListMeta,
  SagaCall,
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
  TurnTrigger
};

export type TurnsResponse = TurnsData;

export interface ListTurnsParams {
  limit?: number;
  after?: string;
  before?: string;
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
