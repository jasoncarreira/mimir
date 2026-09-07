import { describe, expect, it, vi } from "vitest";

import { issueUserKey, listUsers, revokeUserKey } from "./admin-users";

describe("admin users key requests", () => {
  it("keeps labelled issuance additive and rotation explicit", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
      ok: true, data: { canonical: "alice", key: "show-once" }
    }), { headers: { "Content-Type": "application/json" } }));

    await issueUserKey("alice", "user", { label: "laptop", rotate: false }, { fetchImpl });
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v1/admin/users/key");
    expect(JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body))).toEqual({
      canonical: "alice", role: "user", label: "laptop", rotate: false
    });

    await issueUserKey("alice", null, { rotate: true }, { fetchImpl });
    expect(JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body))).toEqual({
      canonical: "alice", rotate: true
    });
  });

  it("targets one label or omits it to revoke all; listing bypasses cache", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
      ok: true, data: { canonical: "alice", revoked: true }
    }), { headers: { "Content-Type": "application/json" } }));

    await revokeUserKey("alice", "phone", { fetchImpl });
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/api/v1/admin/users/revoke");
    expect(JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body))).toEqual({
      canonical: "alice", label: "phone"
    });
    await revokeUserKey("alice", undefined, { fetchImpl });
    expect(JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body))).toEqual({ canonical: "alice" });
    await listUsers({ fetchImpl });
    expect(fetchImpl.mock.calls[2]?.[1]?.cache).toBe("no-store");
  });
});
