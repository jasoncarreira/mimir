# Recent activity context

Mimir adds a bounded `## Recent activity` block to interactive turn prompts. It
combines continuity from the active channel with recent context associated with
the user who initiated the turn. It is not a global transcript.

## Scoping rules

| Turn target | Active channel | Same canonical user's other public channels | Same canonical user's other DMs | Other users' channels |
| --- | --- | --- | --- | --- |
| Public channel | Included | Included | Excluded | Excluded |
| DM | Included | Included | Included | Excluded |
| Synthetic `scheduler:*` / `poller:*` channel | Excluded | Only when the event has an author | Only for a private target with an author | Excluded |

The active-channel stream includes recent messages from the conversation,
regardless of author, so replies retain local continuity. Cross-channel context
is anchored only by messages written by the initiating user. Mimir also includes
assistant replies immediately following those anchors, stopping at the next user
message, so imported messages are not presented without their response context.

Private context flows only into a DM turn for the same canonical user. A public
turn never receives DM content.

## Identity resolution

When `state/identities.yaml` maps platform-specific aliases to one canonical
identity, cross-channel matching uses that canonical identity. For example, the
same person's Discord and Slack messages can contribute to one turn. Unknown
identities fall back to exact author-ID matching.

`MIMIR_CROSS_PLATFORM_PULL=false` disables resolver-based matching and uses
exact author equality instead. It does not restore a global message pool.

## Bounds and filtering

- `MIMIR_RECENT_PER_CHANNEL` controls the active-channel message limit
  (default: `10`).
- `MIMIR_RECENT_AUTHOR_CROSS` controls the number of cross-channel user-message
  anchors (default: `10`). Adjacent assistant replies may make the rendered
  cross-channel stream longer than this anchor count.
- `MIMIR_RECENT_CROSS_HOURS` limits cross-channel candidates by age
  (default: `24`).
- `MIMIR_RECENT_MESSAGE_CHARS` caps each rendered message body
  (default: `4096`).
- The source allowlist excludes benchmark, API, and scheduler records from
  normal conversational context.

Messages from the active and cross-channel streams are merged chronologically
and de-duplicated before rendering.

## SAGA contextual query rewriting

SAGA's optional contextual query rewrite receives only recent messages from the
active channel. Cross-channel activity is deliberately excluded: unrelated
recent material must not change the memory query before retrieval.

## Implementation

The behavior lives in `MessageBuffer.assemble_recent_activity()` in
`mimir/history.py`. `MessageBuffer.recent_for_channel()` is intentionally an
exact-channel primitive; user-scoped cross-channel augmentation is performed by
`cross_author_context()`.
