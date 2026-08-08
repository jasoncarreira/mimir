# ACP over stdio

Install the ACP extra, then let an ACP client launch `mimir acp` as its server
process:

```sh
pip install "mimir-agent[acp]"
mimir acp
```

Mimir provides ACP over stdio only. It does not open a network listener, socket, or port. The ACP client owns the SSH or Docker connection.

stdin carries UTF-8 JSONL requests. stdout carries UTF-8 JSONL frames after Mimir starts. stderr carries diagnostics so logs and errors do not contaminate the protocol frames.

## Remote launch

An ACP client can launch Mimir on another host with SSH:

```sh
ssh <host> mimir acp
```

Disable pseudo-TTY allocation for a framing-safe connection:

```sh
ssh -T <host> mimir acp
```

For an existing container, keep stdin attached without allocating a TTY:

```sh
docker exec -i <container> mimir acp
```

## Framing troubleshooting

| Hazard | Symptom | Fix |
|---|---|---|
| SSH pseudo-TTY allocation | A TTY can alter or interleave bytes, causing malformed-frame errors. | Disable TTY allocation; use `ssh -T`. |
| Docker `-t` | A pseudo-TTY can alter framing. | Use `docker exec -i` without `-t`. |
| MOTD, login banners, or shell startup/rc output | These bytes can precede the first JSON frame and make the client reject the first frame. | Configure a silent noninteractive shell or wrapper, and redirect diagnostics to stderr. |

Mimir reserves stdout only after startup and cannot remove bytes already emitted by a parent shell, SSH daemon, or wrapper. It does not strip banners or provide a network transport. Any shell, daemon, or wrapper that runs before Mimir must therefore keep stdout silent.

## Session replay semantics

Loading a session replays its prepared updates with their original sequence numbers. Replay is client-visible at-least-once delivery: an update delivered before an interruption can be delivered again when the session is loaded. Clients must therefore tolerate duplicate prepared updates.

Plans produced from deepagents Todos preserve Todo content and status. Mimir synthesizes the `medium` priority required by ACP because deepagents Todos have no priority field.
