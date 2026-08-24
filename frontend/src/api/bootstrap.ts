import { useQuery } from "@tanstack/react-query";
// Import from the barrel (not ./http directly) so tests that mock "./api" /
// "../api" intercept this fetch too.
import { apiFetchEnvelope } from ".";
import type { WebBootstrapData } from "./generated/contracts";

// Public auth policy before login; a credentialed request also returns UI and
// runtime data. Shared query key keeps all consumers on that one response.
export const WEB_BOOTSTRAP_QUERY_KEY = ["web-bootstrap"] as const;

export function useBootstrap() {
  return useQuery({
    queryKey: WEB_BOOTSTRAP_QUERY_KEY,
    queryFn: async () => {
      const envelope = await apiFetchEnvelope<WebBootstrapData>("/api/v1/web/bootstrap", {
        cache: "no-store"
      });
      return envelope.data;
    }
  });
}
