// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { ChainlinkBoardIssue } from "../api";
import { WorklinkPanel } from "./ChainlinkBoardRoute";

afterEach(cleanup);

describe("WorklinkPanel", () => {
  it("does not render executable evidence links", () => {
    const issue = {
      id: 1238,
      title: "Unsafe evidence",
      worklink: {
        issue: 1238,
        attempt: 1,
        backend: "opencode",
        status: "review",
        branch: "worklink/1238",
        diff_stat: "",
        blocked_reason: "",
        tests: null,
        evidence_href: "javascript:alert('evidence')",
        transcript_href: "javascript:alert('transcript')",
        pr_url: "javascript:alert('pr')",
      },
    } as unknown as ChainlinkBoardIssue;

    render(<MemoryRouter><WorklinkPanel issue={issue} /></MemoryRouter>);

    expect(screen.queryByRole("link", { name: "Review PR" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Evidence JSON" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Run transcript" })).toBeNull();
  });
});
