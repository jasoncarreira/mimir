import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ChainlinkBoardData
} from "./generated/contracts";

export type { ChainlinkBoardData, ChainlinkBoardIssue } from "./generated/contracts";

export interface ChainlinkBoardParams {
  label?: string;
  status?: string;
  priority?: string;
  show_completed?: boolean;
  offset?: number;
  issue?: number;
}

export function chainlinkBoardHref(params: ChainlinkBoardParams = {}): string {
  return `/api/v1/chainlink-board${buildQuery({ ...params })}`;
}

export function getChainlinkBoard(
  params: ChainlinkBoardParams = {},
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<ChainlinkBoardData>> {
  return apiFetchEnvelope<ChainlinkBoardData>(
    chainlinkBoardHref(params),
    options
  );
}
