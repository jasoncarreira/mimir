import { describe, expect, it } from "vitest";
import { extractStreamingContent } from "./streamingReply";

describe("extractStreamingContent (#583 slice 2)", () => {
  it("returns the send_message `text` arg from complete args JSON", () => {
    expect(extractStreamingContent('{"text":"hello world"}')).toBe("hello world");
    // channel_id may precede text in the args object.
    expect(extractStreamingContent('{"channel_id":"web-x","text":"hi"}')).toBe("hi");
  });

  it("extracts the partial text while the JSON is still forming", () => {
    expect(extractStreamingContent('{"text":"hel')).toBe("hel");
    expect(extractStreamingContent('{"text":"hello wor')).toBe("hello wor");
  });

  it("reassembles text from accumulated fragments", () => {
    let raw = "";
    for (const frag of ['{"text":"', "hi ", "there", '"}']) raw += frag;
    expect(extractStreamingContent(raw)).toBe("hi there");
  });

  it("decodes escapes, including a partial value with newlines", () => {
    expect(extractStreamingContent('{"text":"line1\\nline2"}')).toBe("line1\nline2");
    expect(extractStreamingContent('{"text":"a\\"b')).toBe('a"b');
  });

  it("tolerates a backslash split across fragments without throwing", () => {
    // mid-escape: trailing lone backslash is dropped rather than breaking decode
    expect(extractStreamingContent('{"text":"done\\')).toBe("done");
  });

  it("falls back to the legacy `content` arg when there is no `text`", () => {
    expect(extractStreamingContent('{"content":"legacy reply"}')).toBe("legacy reply");
    expect(extractStreamingContent('{"content":"par')).toBe("par");
  });

  it("returns empty string for empty or fieldless args", () => {
    expect(extractStreamingContent("")).toBe("");
    expect(extractStreamingContent('{"channel_id":"web-x"}')).toBe("");
    expect(extractStreamingContent("{")).toBe("");
  });
});
