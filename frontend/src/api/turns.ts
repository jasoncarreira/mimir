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

export function listTurns(
  params: ListTurnsParams = {},
  options?: ApiClientOptions
): Promise<ApiSuccessEnvelope<TurnsData, ListMeta>> {
  return apiFetchEnvelope<TurnsData, ListMeta>(
    `/api/v1/turns${buildQuery({ ...params })}`,
    options
  );
}
