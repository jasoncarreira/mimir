// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DataTable } from ".";

afterEach(cleanup);

describe("DataTable", () => {
  it("names focusable regions with unique caption references", () => {
    const columns = [{ key: "name", header: "Name" }];
    const { rerender } = render(<>
      <DataTable caption={<strong>Runs</strong>} columns={columns} rows={[{ name: "Build" }]} />
      <DataTable caption="Users" columns={columns} rows={[]} />
    </>);
    const runs = screen.getByRole("region", { name: "Runs" });
    const users = screen.getByRole("region", { name: "Users" });
    for (const region of [runs, users]) {
      expect(region.tabIndex).toBe(0);
      expect(region.getAttribute("aria-labelledby")).toBe(region.querySelector("caption")?.id);
      expect(region.hasAttribute("aria-label")).toBe(false);
      region.focus();
      expect(document.activeElement).toBe(region);
    }
    expect(runs.getAttribute("aria-labelledby")).not.toBe(users.getAttribute("aria-labelledby"));
    expect(within(runs).getByRole("table", { name: "Runs" })).toBeTruthy();
    expect(within(runs).getByRole("columnheader", { name: "Name" })).toBeTruthy();
    expect(within(runs).getByRole("cell", { name: "Build" })).toBeTruthy();

    rerender(<DataTable columns={columns} rows={[]} />);
    const fallback = screen.getByRole("region", { name: "Table" });
    expect(fallback.tabIndex).toBe(0);
    expect(fallback.hasAttribute("aria-labelledby")).toBe(false);
    expect(fallback.querySelector("caption")).toBeNull();
    fallback.focus();
    expect(document.activeElement).toBe(fallback);
  });
});
