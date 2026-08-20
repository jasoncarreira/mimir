# Recent activity context

Mimir adds a bounded `## Recent activity` block to interactive turn prompts. It
combines continuity from the active channel with recent context associated with
the user who initiated the turn. It is not a global transcript.

## Scoping rules

| Turn target | Active channel | Same user's registered DM or ACP channels | Unknown/group/public channels | Other users' channels |
| --- | --- | --- | --- | --- |
| Unknown, public, guild, or group channel | Included | Excluded | Excluded | Excluded |
| Registered one-person DM or ACP session | Included | Included when audiences are compatible | Excluded | Excluded |
| Synthetic `scheduler:*` / `poller:*` channel | Excluded | Excluded | Excluded | Excluded |

The active-channel stream includes recent messages from the conversation,
regardless of author, so replies retain local continuity. Cross-channel context
is anchored only by messages written by the initiating user. Mimir also includes
assistant replies immediately following those anchors, stopping at the next user
message, so imported messages are not presented without their response context.

ACP sessions and registered one-person DM channels have audience `{P}`. A
server-attested message authored by P can enter an exact `{P}` destination.
Adjacent assistant replies do not inherit that attestation; they enter only when
the destination audience is a subset of their source audience. Missing, empty,
public, guild, and multi-user audiences are unknown and receive no cross-channel
content. DM content therefore never enters an unknown or multi-user destination.

## Identity resolution

When `state/identities.yaml` maps platform-specific aliases to one canonical
identity, cross-channel matching uses that canonical identity. For example, the
same person's Discord and Slack DM messages can contribute to one ACP turn.
Protected cross-channel admission uses strict registered identities; unknown
authors are omitted.

`MIMIR_CROSS_PLATFORM_PULL=false` disables canonical alias matching and uses
raw author equality for cross-channel anchors and feedback owners. It is a raw-
author privacy kill switch, not a ban on exact-author continuity. A strict
identity lookup is still required before protected admission can mint owner
attestation or construct a canonical ACL, so unknown authors remain omitted.

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

Protected candidates are produced by
`MessageBuffer.assemble_recent_activity_candidates()` in `mimir/history.py` and
admitted with server-derived provenance by `Agent._select_recent_activity()`.
`MessageBuffer.recent_for_channel()` remains an exact-channel primitive for SAGA
query rewriting and other direct consumers. The legacy public assembly helpers
retain their compatibility behavior for non-protected callers.
