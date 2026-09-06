import { readFileSync } from "node:fs";
import postcss, { type AtRule, type Container, type Document } from "postcss";
import { describe, expect, it } from "vitest";

const styles = postcss.parse(readFileSync(new URL("./styles.css", import.meta.url), "utf8"));
const skins = ["default-retro", "neon-terminal", "cosmic-nebula"];

// Static layout contract, not a simulated scrollWidth: jsdom has no layout engine.
// Resolve the shared rules in source order at the operator's viewport/pane sizes.
function declarations(selector: string, viewport: number, pane: number) {
  const result: Record<string, string> = {};
  styles.walkRules((rule) => {
    if (!rule.selectors.includes(selector)) return;
    for (let parent: Container | Document | undefined = rule.parent; parent; parent = parent.parent) {
      if (parent.type !== "atrule") continue;
      const atRule = parent as AtRule;
      const limit = /max-width: (\d+)px/.exec(atRule.params);
      if (!limit) return;
      const width = atRule.name === "container" ? pane : viewport;
      if (width > Number(limit[1])) return;
    }
    rule.walkDecls((decl) => { result[decl.prop] = decl.value; });
  });
  return result;
}

describe.each(skins)("%s narrow-pane stylesheet contract", (skin) => {
  const manifest = readFileSync(new URL(`./skins/${skin}.ts`, import.meta.url), "utf8");
  const sidebar = manifest.includes('layout: "sidebar"');

  it.each([390, 652, 768, 1153])("keeps shell and controls shrinkable at %ipx", (viewport) => {
    const padding = viewport <= 640 ? 16 : 32;
    const pane = viewport - (sidebar && viewport > 900 ? 280 : 0) - padding * 2;
    const css = (selector: string) => declarations(selector, viewport, pane);

    expect(css("body")["min-width"]).toBe("0");
    expect(css(".app-frame")["grid-template-columns"]).toBe("minmax(0, 1fr)");
    expect(css(".app-main")).toMatchObject({
      width: "min(1280px, 100%)", "min-width": "0", "overflow-x": "auto",
      container: "app-main / inline-size"
    });
    if (sidebar && viewport <= 900) {
      expect(css(".app-frame--sidebar")["grid-template-columns"]).toBe("minmax(0, 1fr)");
      expect(css(".app-sidebar")["height"]).toBe("auto");
      expect(css(".app-sidebar .app-nav")["flex-direction"]).toBe("row");
    }
    for (const selector of [".app-header", ".app-header__status", ".app-nav",
      ".ui-panel__header", ".ui-panel__actions", ".ops-header-row",
      ".chainlink-actions", ".chainlink-filters", ".wiki-view-toggle", ".ui-tabs__list"]) {
      expect(css(selector)["flex-wrap"], selector).toBe("wrap");
      expect(css(selector).overflow, selector).not.toBe("hidden");
      expect(css(selector)["overflow-x"], selector).not.toBe("hidden");
    }
    expect(css(".app-nav")["min-width"]).toBe("0");
    expect(css(".skin-picker .ui-input")).toMatchObject({ "min-width": "0", "max-width": "100%" });
    expect(css(".chainlink-filter")).toMatchObject({ "min-width": "min(140px, 100%)", "max-width": "100%" });
    for (const selector of [".chainlink-route", ".ops-route", ".mcp-route", ".ops-panel-stack"]) {
      expect(css(selector)["grid-template-columns"]).toBe("minmax(0, 1fr)");
    }
    for (const selector of [".wiki-browser", ".memory-browser"]) {
      if (pane <= 960) {
        expect(css(selector)["grid-template-columns"]).toBe("minmax(0, 1fr)");
        expect(css(`${selector}__sidebar`)).toMatchObject({
          position: "static", "max-height": viewport <= 720 ? "65dvh" : "none"
        });
        if (viewport <= 720) expect(css(`${selector}__sidebar`).overflow).toBe("auto");
      } else {
        expect(css(selector)["grid-template-columns"]).toMatch(/minmax\(\d+px, \d+px\) minmax\(0, 1fr\)/);
      }
    }
    for (const selector of [".chainlink-board", ".ui-table-wrap"]) {
      expect(css(selector)).toMatchObject({ "min-width": "0", "max-width": "100%", "overflow-x": "auto" });
    }
    expect(css(".chainlink-board")["grid-template-columns"]).toBe("repeat(6, minmax(220px, 1fr))");
    expect(css(":focus-visible").outline).toContain("3px solid var(--mimir-color-focus-ring");
  });
});

it("caps route-card minimum tracks at their available width", () => {
  for (const selector of [".route-grid", ".ui-card-grid", ".ops-panel-grid",
    ".admin-config__split", ".factory-runs__list"]) {
    expect(declarations(selector, 390, 358)["grid-template-columns"]).toMatch(/minmax\(min\(\d+px, 100%\), 1fr\)/);
  }
});
