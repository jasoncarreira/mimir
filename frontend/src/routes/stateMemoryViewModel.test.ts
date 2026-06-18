import { describe, expect, it } from "vitest";
import {
  countByLayer,
  defaultMemoryPath,
  descriptionFromContent,
  flattenFiles,
  fmtBytes,
  fmtTimestamp,
  searchResultsCaption,
  sourceLayerForPath
} from "./stateMemoryViewModel";
import type { MemoryTreeDir } from "../api";

describe("state/memory dashboard view model", () => {
  const tree: MemoryTreeDir = {
    name: "",
    type: "dir",
    path: "",
    desc: null,
    children: [
      {
        name: "memory",
        type: "dir",
        path: "memory",
        desc: null,
        children: [
          { name: "INDEX.md", type: "file", path: "memory/INDEX.md", size: 2048, modified: "2026-06-18T14:00:00Z", desc: "Memory index" },
          { name: "notes.md", type: "file", path: "memory/channels/demo/notes.md", size: 99, modified: "not-a-date", desc: null }
        ]
      },
      {
        name: "state",
        type: "dir",
        path: "state",
        desc: null,
        children: [
          { name: "topic.md", type: "file", path: "state/wiki/topics/topic.md", size: 1048576, modified: "2026-06-18T15:01:02Z", desc: "Topic" }
        ]
      }
    ]
  };

  it("flattens nested state and memory trees for list rendering and counts", () => {
    const files = flattenFiles(tree);

    expect(files.map((file) => file.path)).toEqual([
      "memory/INDEX.md",
      "memory/channels/demo/notes.md",
      "state/wiki/topics/topic.md"
    ]);
    expect(countByLayer(files)).toEqual({ state: 1, memory: 2 });
    expect(defaultMemoryPath(files)).toBe("memory/INDEX.md");
  });

  it("maps source layers, byte sizes, timestamps, and desc headers", () => {
    expect(sourceLayerForPath("state/wiki/topics/topic.md")).toBe("state");
    expect(sourceLayerForPath("memory/core/00-identity.md")).toBe("core memory");
    expect(sourceLayerForPath("memory/issues/gotcha.md")).toBe("non-core memory");
    expect(sourceLayerForPath("attachments/file.txt")).toBe("unknown");

    expect(fmtBytes(99)).toBe("99 B");
    expect(fmtBytes(2048)).toBe("2.0 KB");
    expect(fmtBytes(1048576)).toBe("1.0 MB");
    expect(fmtTimestamp("2026-06-18T15:01:02Z")).toBe("2026-06-18 15:01:02Z");
    expect(fmtTimestamp("not-a-date")).toBe("not-a-date");
    expect(descriptionFromContent("<!-- desc: dashboard notes -->\n# Title")).toBe("dashboard notes");
    expect(descriptionFromContent("# Title")).toBe("");
  });

  it("summarizes search result counts and truncation", () => {
    const hits = [{ path: "memory/INDEX.md", line_no: 1, snippet: "Memory" }];

    expect(searchResultsCaption(hits, null, false)).toBe("1 result(s)");
    expect(searchResultsCaption(hits, 23, true)).toBe("23 result(s) (truncated)");
  });
});
