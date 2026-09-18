# ACP blocked-main SIGSEGV investigation (#1775)

## Disposition

The initial local investigation on 2026-09-18 at
`afdfe2bace38f1a478bc91a43d1df73f4ddad109` did not reproduce the SIGSEGV.
Subsequently, reviewer inspection of hosted artifact `10532233754` resolved the
truncation question: the original `.stacks` file is incomplete, not merely its
log rendering. Dump/shutdown overlap is established by that supplied evidence;
whether the dump caused the SIGSEGV or was interrupted by it remains open.
Neither a production defect nor a causal test-construction defect is established.
No production code, assertion, timeout, exit contract, or diagnostic was changed.
This is an updated investigation record, not a claim that the crash is fixed
or that the requested causal investigation is complete.

## Signal and exit path

Line references below are to that revision (the Python files are unchanged).
The reported `worker-journal` progress reaches `force-exit-enter` after
`watchdog-fired`, with wakeup byte `0x0f`. Signal delivery and watchdog arming
have therefore already succeeded; attributing this occurrence to handler
installation or signal delivery would contradict the evidence.

- `mimir/acp/proxy.py:1277-1288` observes C-level delivery and arms the watchdog;
  `1428-1438` sets `signum` and starts the daemon timer.
- `mimir/acp/proxy.py:1416-1426` implements forced exit. With no recorded failure,
  it calls `os._exit(128 + self.signum)`. The matrix never calls `record_failure`,
  so the failure-diagnostic branch is not expected on this path.
- The exit path does not restore signal handlers or wakeup descriptors:
  acquisition is at `mimir/acp/proxy.py:1355-1357,1377-1379`, while restoration is
  at `1311-1315,1394-1398`. Those are not operations performed by `_force_exit`.
- `tests/test_acp_shutdown.py:119-121` records `force-exit-enter` *before* calling
  the original method. `123-134` replaces `os._exit` with a journal wrapper that
  records `exit-dispatch`, flushes the tee via `53-61`, and only then calls the
  real exit. Thus reaching the marker does not prove the real `os._exit` began.
  The supplied second occurrence has no `exit-dispatch` diagnostic.
- `tests/test_acp_shutdown.py:1472-1475,1482-1505` declares the ctypes receive
  signature, keeps its two-byte buffer and sockets alive, targets a live worker
  with `pthread_kill`, and blocks main in `MSG_WAITALL`. Inspection did not
  establish an invalid pointer, stale thread target, or buffer-lifetime defect.
  This is not proof against a native/runtime race.

## Reproduction

Environment: Linux aarch64, kernel
`7.0.14-orbstack-00380-ga7e0a2dc9535-aarch64`, glibc 2.41, CPython 3.11.15
(GCC 14.2.0). `uv run --extra dev` created this checkout's environment from its
lock. This is not the hosted CPython 3.12.13 environment from occurrence two.

The following command completed **20 iterations of all eight unchanged matrix
variants**, eight children concurrently per iteration: **160/160 passed**,
including 20 `worker-journal` children. It ran concurrently with the six-worker
full suite. Each invocation retained the actual test's handshake, five-second
production watchdog, six-second exit bound, and exact exit-code assertion.
These are investigation repetitions, not retries added to a test or gate.

```bash
uv run --extra dev python -c 'import asyncio, importlib.util, tempfile, os; from pathlib import Path; os.environ.pop("MIMIR_MODEL_SPEC", None); spec = importlib.util.spec_from_file_location("shutdown_tests", "tests/test_acp_shutdown.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
async def run():
    for iteration in range(20):
        with tempfile.TemporaryDirectory(prefix="acp1775-", dir=".") as root:
            cases = []
            for delivery in ("main", "worker", "main-worker", "main-main"):
                for journal in (False, True):
                    path = Path(root).resolve() / (delivery + str(journal)); path.mkdir()
                    cases.append(m.test_blocked_main_signal_delivery_matrix(delivery, journal, path))
            await asyncio.gather(*cases)
            print(f"iteration {iteration + 1}: 8/8 passed", flush=True)
asyncio.run(run())'
```

An initial launch using `env -u MIMIR_MODEL_SPEC` was denied by tool permissions;
a launch using `/tmp/opencode` failed directory creation with `PermissionError`.
Neither launched a child or contributes to the 160 count.

Independent pytest checks, each including all eight variants:

- `uv run --extra dev pytest tests/test_acp_shutdown.py -q -n 6`:
  **207 passed**, 18 warnings, 30.98 seconds.
- `uv run --extra dev --extra bench pytest -q -n 6`:
  **19692 passed, 61 skipped**, 113 warnings, 221.18 seconds.
  The separately tracked stalled-read session test did not fail. The suite did
  emit an unrelated subprocess-transport `Event loop is closed` warning; its
  presence alone does not link that warning to a child SIGSEGV.

### Follow-up validation (artifact-evidence correction)

The scoped contained runner used CPython 3.11.15 and completed
`tests/test_acp_shutdown.py`: **207 passed**, 18 warnings, 32.60 seconds.
This preserves all eight matrix variants and both child-signal diagnostic cases;
it is not additional evidence that the hosted 3.12.13 fault is fixed.

The full-suite attempt returned runner code `tests_failed` / exit 1, but exposed
only progress through 36%, with no completed test counts, failure names, or
tracebacks. Its output paths were empty. Full-suite completion cannot be
verified from that result: it is neither a clean suite nor a diagnosed test
failure. CI remains the full-suite validation surface for this documentation
correction; the initial investigation's completed counts above are historical,
not substituted for this attempt.

## Stack capture and remaining evidence

`tests/test_acp_shutdown.py:39-46` writes periodic native faulthandler dumps to
`.stacks`, starting before signal installation. Its interval equals the product
watchdog interval but their start times differ. No synchronization requires a
dump to finish before exit. The reviewer-supplied artifact evidence below
establishes overlap with shutdown and an interrupted dump. It does not establish
that faulthandler caused the fault, or that the real `os._exit` was entered.

The matrix uses `-X faulthandler` at `1509-1512`, which separately directs fatal
diagnostics to stderr. At `1526-1528` it waits for `communicate()` before reporting.
The helper at `1397-1415` reads the entire side files and stderr without slicing;
this is not a live-file snapshot race in the matrix's parent. A final file can
nevertheless be incomplete if its writer dies mid-dump. The timeout reporter at
`184-200`, unlike this exit reporter, can read a still-running child's file.

The initial investigation could not obtain the original raw CI log or artifact:
log endpoints returned HTTP 403 and artifact download returned HTTP 401; `gh`
was denied by tool permissions. Run `35308316963`, job `105485062376`, has
artifact `10532233754` named
`pytest-evidence-pytest-worker-uid-3.12-35308316963-1`. Those access failures are
historical limitations, not an outstanding truncation question.

### Artifact evidence supplied after the initial investigation

Source: Jason Carreira's review `5244619087` on PR #2107, 2026-09-18,
https://github.com/jasoncarreira/mimir/pull/2107#pullrequestreview-5244619087.
He downloaded the artifact; the following is attributed to that inspection,
not a claim that this follow-up independently downloaded its bytes.

- Failing `popen-gw2/test_blocked_main_signal_deliv3`: progress records
  `child-started`, `install-enter`, `handlers-installed`, `watchdog-start-enter`,
  `watchdog-start-returned`, `watchdog-fired`, and `force-exit-enter`.
  `.wakeup` contains `0o017` (SIGTERM); `.diagnostics` confirms the watchdog's
  5.0-second timed wait. `.stacks` is 204 bytes and ends at the bare `File `
  token. Its last complete frame is `threading.py`, line 0, in `__exit__`.
- Passing sibling journal variants `...deliv1` and `...deliv5` have 2957-byte
  dumps and also record `signal-enter:15`. Their dumps show real line numbers,
  including line 359 in `wait` and line 655 in `wait`.

This resolves the capture question: the writer died mid-dump, leaving a partial
file. Dump/exit overlap in the sense of a dump active during shutdown is now
established. `line 0` is not a usable Python source location and cannot identify
the native instruction that faulted.

The missing Python signal marker is consistent with the intentionally blocked
worker-delivery variant, not independently a defect: the unchanged matrix at
1534-1536 explicitly requires no `signal-enter:` or `signal-dispatch:` for
`delivery == "worker"`. Main remains in untimed `libc.recv(..., MSG_WAITALL)`;
C-level delivery and watchdog activation do not require Python handler dispatch.
The passing siblings exercise different delivery variants, so they are useful
capture comparisons, not a controlled experiment on the cause of SIGSEGV.

### Narrower causal question: diagnostic cause or diagnostic victim?

The remaining hypothesis is that the test-only periodic faulthandler thread
races frame/thread changes during forced shutdown. If demonstrated, the repair
belongs to test construction, not `_ShutdownHooks`. Source inspection narrows
but does not decide that hypothesis:

- The periodic dump is armed before handler installation; equal five-second
  intervals do not synchronize it with the later production timer.
- The observed `force-exit-enter` is before the call to the production method.
  The production method checks `signum` and `_failure_detail` before calling
  the patched `os._exit`; the patch first records `exit-dispatch`. That marker
  is absent. A theory requiring the *real* exit syscall to have already begun
  is therefore not established by this trace.
- A partial dump can be the victim of a fatal fault elsewhere, or the dump's
  native frame walk can itself fault. Both explain these artifacts. Neither
  the short file nor a line-0 Python frame selects between them.

The discriminating next evidence is a native faulting instruction/backtrace
with all threads from hosted CPython 3.12.13 (including its exact build and
architecture), alongside the unchanged matrix's side files. Locate whether
SIGSEGV originated in the periodic dump's frame walk, another thread's runtime
operation, or a signal handler. Then test that specific mechanism with a
controlled overlap reproducer and a targeted synchronization change. A passing
run after disabling periodic dumps would only be correlation, especially given
the existing 160/160 baseline; it is not sufficient justification to remove
them. No such native trace or controlled causal reproduction is available in
this follow-up, so the dump remains enabled and no speculative fix is claimed.
The causal investigation remains unfinished; this update closes only the stale
artifact/disposition claims.

For a future occurrence, retain the byte-for-byte `child-progress`, `.wakeup`,
`.diagnostics`, `.stacks`, child stdout/stderr, and raw job log, rather than just
the rendered assertion. Compare the artifact's `.stacks` tail with the raw log
and rendered log. Record interpreter build, architecture, libc, revision, and
variant. A native core/backtrace with all threads would discriminate a crash
inside faulthandler, ctypes, or another runtime operation from the Python exit
wrapper; Python frames alone may not. In particular, preserve whether
`exit-dispatch`, `flush-enter`, and `flush-returned` were reached after
`force-exit-enter`.

`_assert_blocked_main_exit` and `test_blocked_main_exit_reports_child_signal`
remain unchanged, including `Fatal Python error: Segmentation fault` and
`in crash_child` assertions at `1447-1448`. The matrix still requires exactly
`128 + expected` at `1528`; negative return codes remain failures. No security
guard or product fix was introduced, so there is no mutation claim to prove.
