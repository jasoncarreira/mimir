// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, onTestFinished } from "vitest";
import { LogReadWarning } from "./LogReadWarning";
import { localSkins, skinTokensToCssVariables } from "./skins/SkinProvider";
import { EmptyState } from "./ui";

const styles = readFileSync("frontend/src/styles.css", "utf8");

afterEach(cleanup);

describe.each(Object.values(localSkins))("LogReadWarning in $id", (skin) => {
  it.each(["Turns", "Events"] as const)("styles %s read failures differently from empty results", (log) => {
    const variables = skinTokensToCssVariables(skin);
    // Resolve tokens before parsing: jsdom is not a browser layout engine and
    // cannot reliably resolve inherited CSS variables. This tests the real CSS
    // cascade and skin palette, not responsive layout or actual visibility.
    let resolved = styles.replace(/\/\*[\s\S]*?\*\//g, "");
    while (/var\(/.test(resolved)) {
      const next = resolved.replace(/var\((--[\w-]+)(?:,\s*((?:[^()]|\([^()]*\))*))?\)/g, (_, name: string, fallback: string | undefined) =>
        String(variables[name as keyof typeof variables] ?? fallback ?? "initial"),
      );
      if (next === resolved) throw new Error("Unresolved CSS variable expression");
      resolved = next;
    }
    const stylesheet = document.createElement("style");
    stylesheet.textContent = resolved;
    document.head.append(stylesheet);
    onTestFinished(() => stylesheet.remove());
    expect(stylesheet.sheet?.cssRules.length).toBeGreaterThan(0);
    const { container } = render(<>
      <div className="skin-root" data-skin={skin.id}>
        <LogReadWarning log={log} />
        <EmptyState title="No matching records">Try another filter.</EmptyState>
      </div>
    </>);
    const warning = screen.getByRole("alert");
    const heading = within(warning).getByRole("heading", { name: `${log} log could not be read` });
    const message = within(warning).getByText(
      "Available records may be incomplete. Check server log access and refresh after resolving.",
    );
    const empty = container.querySelector<HTMLElement>(".ui-state--empty")!;
    const warningStyle = getComputedStyle(warning);
    const emptyStyle = getComputedStyle(empty);
    const rgb = (hex: string) => `rgb(${hex.slice(1).match(/../g)!.map((part) => parseInt(part, 16)).join(", ")})`;

    expect(warningStyle.backgroundColor).toBe(rgb(skin.tokens.colorStatusDangerBackground));
    expect(warningStyle.borderTopColor).toBe(rgb(skin.tokens.colorStatusDanger));
    expect(emptyStyle.backgroundColor).toBe(rgb(skin.tokens.colorPanelBackgroundMuted));
    expect(emptyStyle.borderTopColor).toBe(rgb(skin.tokens.colorPanelBorder));
    expect(warningStyle.backgroundColor).not.toBe(emptyStyle.backgroundColor);
    expect(warningStyle.borderTopColor).not.toBe(emptyStyle.borderTopColor);
    expect(getComputedStyle(heading).color).toBe(rgb(skin.tokens.colorText));
    expect(getComputedStyle(message).color).toBe(rgb(skin.tokens.colorText));
    expect(warningStyle.borderTopStyle).toBe("dashed");
    expect(warningStyle.borderTopWidth).toBe(skin.tokens.borderWidthHairline);
    expect(warningStyle.paddingTop).toBe(skin.tokens.spaceLg);
    expect(warningStyle.display).toBe("grid");
    expect(warningStyle.minHeight).toBe("160px");
    for (const element of [warning, heading, message]) {
      expect(getComputedStyle(element).display).not.toBe("none");
      expect(getComputedStyle(element).visibility).toBe("visible");
    }
    expect(empty.hasAttribute("role")).toBe(false);
  });
});
