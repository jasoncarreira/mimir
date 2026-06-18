import { apiFetchEnvelope, type ApiClientOptions } from "./http";
import type {
  ApiSuccessEnvelope,
  ChainlinkBoardData
} from "./generated/contracts";

export type { ChainlinkBoardData, ChainlinkBoardIssue } from "./generated/contracts";

export function getChainlinkBoard(
  options?: ApiClientOptions & RequestInit
): Promise<ApiSuccessEnvelope<ChainlinkBoardData>> {
  return apiFetchEnvelope<ChainlinkBoardData>(
    "/api/v1/chainlink-board",
    options
  );
}
