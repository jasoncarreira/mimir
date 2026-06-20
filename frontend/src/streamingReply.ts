// chainlink #583 slice 2: the streaming reply rides on the send_message
// tool-call args, which arrive as fragments of a JSON object {"content": "..."}.
// Extract the (possibly partial) content value so the chat can show the reply
// forming. The final chat.message from /chat/stream is always authoritative and
// replaces this, so best-effort partial extraction mid-stream is fine.
export function extractStreamingContent(rawArgs: string): string {
  if (!rawArgs) return "";
  // Fast path: the fragments have reassembled into valid JSON.
  try {
    const parsed = JSON.parse(rawArgs) as { content?: unknown };
    if (parsed && typeof parsed === "object" && typeof parsed.content === "string") {
      return parsed.content;
    }
  } catch {
    // Incomplete JSON — fall through to partial extraction.
  }
  const match = /"content"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(rawArgs);
  if (!match) return "";
  let body = match[1];
  // Drop a dangling backslash (an escape sequence split across fragments)
  // so JSON decoding of the partial value doesn't choke.
  if (/(?:^|[^\\])(?:\\\\)*\\$/.test(body)) body = body.slice(0, -1);
  try {
    return JSON.parse(`"${body}"`) as string;
  } catch {
    return body
      .replace(/\\n/g, "\n")
      .replace(/\\t/g, "\t")
      .replace(/\\r/g, "\r")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\");
  }
}
