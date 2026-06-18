# Admin Config UI

The React Admin page (`/app/admin`) is a read-mostly operator surface for
runtime configuration, model routing, schedules, pollers, and environment/key
presence.

The backend contract is `GET /api/v1/admin/config`. It uses the standard v1
envelope and the normal `X-API-Key` middleware. `/api/v1/web/bootstrap` remains
public-shaped, but this admin data endpoint is not auth-exempt.

## Read-Only in v1

- Effective model state: `MIMIR_MODEL_SPEC`, provider prefix, resolved provider,
  model name, 1M-context flag, base-URL override presence, billing mode, usage
  block, rate-limit capture, and max-output-token cap.
- Config schema sections: typed backend sections for model/runtime/env fields.
- Scheduler entries from `scheduler.yaml`.
- Registered poller details, falling back to poller discovery when the live
  scheduler object is unavailable.
- Environment/API-key inventory: categorized names, presence, secret flag, and
  redacted display value.
- Raw config inspection: dataclass fields serialized to JSON with secret-bearing
  fields masked.

## Mutable Settings

None in v1. The response includes:

```json
{
  "mode": "read_only_v1",
  "mutable_fields": [],
  "reveal_secret_values": false,
  "reveal_path": null,
  "edit_path": null,
  "rate_limited": true
}
```

Reveal and edit paths are intentionally omitted for this issue. Future mutation
support should add explicit allowlisted fields, audit logging, and request-rate
limits before exposing any write endpoint.

## Redaction

Secret-like names containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`,
`CREDENTIAL`, or `AUTH` report only presence plus `[REDACTED]`. Secret values are
not returned by default in either the env list or raw config.
