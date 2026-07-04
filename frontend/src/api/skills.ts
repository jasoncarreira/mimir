import { apiFetchEnvelope, buildQuery, type ApiClientOptions } from "./http";
import type { ApiSuccessEnvelope, InvocableSkillsData } from "./generated/contracts";

export type { InvocableSkill, InvocableSkillsData } from "./generated/contracts";

export function fetchInvocableSkills(
  params: { channelId?: string | null } = {},
  options?: RequestInit & ApiClientOptions
): Promise<ApiSuccessEnvelope<InvocableSkillsData>> {
  const query = buildQuery({ channel_id: params.channelId });
  return apiFetchEnvelope<InvocableSkillsData>(`/api/v1/skills/invocable${query}`, {
    ...options,
    method: "GET"
  });
}
