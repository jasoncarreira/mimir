import { describe, expect, it } from "vitest";
import { extractStreamingContent } from "./streamingReply";

describe("extractStreamingContent (#583 slice 2)", () => {
  it("returns content from complete args JSON", () => {
    expect(extractStreamingContent('{"content":"hello world"}')).toBe("hello world");
  });

  it("extracts the partial content while the JSON is still forming", () => {
    expect(extractStreamingContent('{"content":"hel')).toBe("hel");
    expect(extractStreamingContent('{"content":"hello wor')).toBe("hello wor");
  });

  it("reassembles content from accumulated fragments", () => {
    let raw = "";
    for (const frag of ['{"content":"', "hi ", "there", '"}']) raw += frag;
    expect(extractStreamingContent(raw)).toBe("hi there");
  });

  it("decodes escapes, including a partial value with newlines", () => {
    expect(extractStreamingContent('{"content":"line1\\nline2"}')).toBe("line1\nline2");
    expect(extractStreamingContent('{"content":"a\\"b')).toBe('a"b');
  });

  it("tolerates a backslash split across fragments without throwing", () => {
    // mid-escape: trailing lone backslash is dropped rather than breaking decode
    expect(extractStreamingContent('{"content":"done\\')).toBe("done");
  });

  it("returns empty string for empty or contentless args", () => {
    expect(extractStreamingContent("")).toBe("");
    expect(extractStreamingContent('{"other":"x"}')).toBe("");
    expect(extractStreamingContent("{")).toBe("");
  });
});
