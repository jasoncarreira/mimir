// chainlink #583 slice 2: the streaming reply rides on the send_message
// tool-call args, which arrive as fragments of a JSON object. The send_message
// schema is send_message(text, channel_id), so the reply is the `text` arg
// (older shapes used `content` — kept as a fallback). Extract the (possibly
// partial) value so the chat can show the reply forming. The final chat.message
// from /chat/stream is always authoritative and replaces this, so best-effort
// partial extraction mid-stream is fine.

function decodePartialString(body: string): string {
  // Drop a dangling backslash (an escape sequence split across fragments) so
  // JSON decoding of the partial value doesn't choke.
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

export function extractStreamingContent(rawArgs: string): string {
  if (!rawArgs) return "";
  // Fast path: the fragments have reassembled into valid JSON.
  try {
    const parsed = JSON.parse(rawArgs) as { text?: unknown; content?: unknown };
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.text === "string") return parsed.text;
      if (typeof parsed.content === "string") return parsed.content; // legacy
    }
  } catch {
    // Incomplete JSON — fall through to partial extraction.
  }
  // Partial: prefer the send_message `text` arg, fall back to legacy `content`.
  const textMatch = /"text"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(rawArgs);
  if (textMatch) return decodePartialString(textMatch[1]);
  const contentMatch = /"content"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(rawArgs);
  if (contentMatch) return decodePartialString(contentMatch[1]);
  return "";
}
