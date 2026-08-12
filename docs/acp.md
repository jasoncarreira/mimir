# ACP client

Mimir's server owns the shared ACP runtime and listens on the owner-only Unix socket at `<home>/.mimir/acp/daemon.sock`. `mimir acp` is a client proxy; it never creates a standalone runtime.

Create a local profile and store its key in the native OS secure store:

```console
mimir acp profile set default --home /absolute/mimir/home
mimir acp credential set --profile default
mimir acp --profile default
```

Profiles in `${XDG_CONFIG_HOME:-~/.config}/mimir/acp/profiles.json` contain no credentials. Raw keys are stored only by the operating system's native secure store under service `mimir.acp`; there is no plaintext or third-party keyring fallback. Selection uses `--profile`, then nonempty `MIMIR_ACP_PROFILE`, then `default`.

Credential operations are:

```console
mimir acp credential status --profile default
mimir acp credential set --profile default
mimir acp credential delete --profile default
```

Setting a credential reads from a controlling TTY, never stdin, argv, or the environment. Deleting a missing credential succeeds. If the native store raises after a set or delete was dispatched, the command exits 3 and reports `credential-mutation-uncertain`; inspect the native store before deciding whether to repeat the operation. Validation, profile, secure-store selection, reads, and TTY failures exit 1 and do not report uncertainty.

For a remote daemon, configure every SSH field:

```console
mimir acp profile set remote --home /remote/home \
  --ssh-host host.example --ssh-user mimir --ssh-port 22 \
  --identity-file /absolute/id_ed25519 --known-hosts-file /absolute/known_hosts
mimir acp credential set --profile remote
mimir acp --profile remote
```

The client invokes fixed `/usr/bin/ssh` public-key options and the internal remote command `mimir-agent acp relay --home PATH`. The relay is profile-, credential-, and runtime-blind. Install the same Mimir version remotely and ensure that command is on the remote account's noninteractive PATH. The identity file must be owned by the current user with mode `0600`; known hosts must be owner-controlled and not group/world writable.

Local socket and relay connection attempts are bounded to 5 seconds. SSH process creation and establishment are bounded to 12 seconds. Once established, an interactive ACP session has no duration limit. Cleanup uses bounded writer drain/close/abort stages of 2, 1, and 1 seconds within a 5-second force-close bound, then waits 1 second for SSH, terminates and waits 2 seconds, and kills and waits 1 second.

stdin and stdout carry UTF-8 JSONL ACP frames only. stdout is reserved before command imports; diagnostics go to stderr. Do not allocate a pseudo-TTY or insert banners because either alters protocol framing.
