# ACP client

Mimir's server owns the shared ACP runtime and listens on the owner-only Unix socket at `<home>/.mimir/acp/daemon.sock`. `mimir acp` is a client proxy; it never creates a standalone runtime.

Create a local profile and store its key in the native OS secure store:

```console
mimir acp profile set default --home /absolute/mimir/home
mimir acp credential set --profile default
mimir acp --profile default
```

Profiles in `${XDG_CONFIG_HOME:-~/.config}/mimir/acp/profiles.json` contain no credentials. Selection uses `--profile`, then nonempty `MIMIR_ACP_PROFILE`, then `default`.

For a remote daemon, configure every SSH field:

```console
mimir acp profile set remote --home /remote/home \
  --ssh-host host.example --ssh-user mimir --ssh-port 22 \
  --identity-file /absolute/id_ed25519 --known-hosts-file /absolute/known_hosts
mimir acp credential set --profile remote
mimir acp --profile remote
```

The client invokes hardened public-key OpenSSH and the internal remote command `mimir-agent acp relay --home PATH`. The relay is profile-, credential-, and runtime-blind.

stdin and stdout carry UTF-8 JSONL ACP frames only. stdout is reserved before command imports; diagnostics go to stderr. Do not allocate a pseudo-TTY or insert banners because either alters protocol framing.
