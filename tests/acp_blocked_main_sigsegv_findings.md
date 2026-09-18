# ACP blocked-main SIGSEGV investigation (#1775)

## Disposition

Unreproduced on 2026-09-18 at `afdfe2bace38f1a478bc91a43d1df73f4ddad109`.
Neither a production defect nor a test-construction artifact was established.
No production code, assertion, timeout, exit contract, or diagnostic was changed.
This is an investigation record, not a claim that the crash is fixed.

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

## Stack capture and remaining evidence

`tests/test_acp_shutdown.py:39-46` writes periodic native faulthandler dumps to
`.stacks`, starting before signal installation. Its interval equals the product
watchdog interval but their start times differ. No synchronization requires a
dump to finish before exit. An interrupted dump is possible, but neither a
dump/exit overlap nor a faulthandler-caused fault was established here.

The matrix uses `-X faulthandler` at `1509-1512`, which separately directs fatal
diagnostics to stderr. At `1526-1528` it waits for `communicate()` before reporting.
The helper at `1397-1415` reads the entire side files and stderr without slicing;
this is not a live-file snapshot race in the matrix's parent. A final file can
nevertheless be incomplete if its writer dies mid-dump. The timeout reporter at
`184-200`, unlike this exit reporter, can read a still-running child's file.

The original raw CI log and artifact contents could not be obtained: log
endpoints returned HTTP 403 and artifact download returned HTTP 401; `gh` was
denied by tool permissions. Run `35308316963`, job `105485062376`, has artifact
`10532233754` named `pytest-evidence-pytest-worker-uid-3.12-35308316963-1`.
**Whether the reported cutoff is in the original `.stacks` file or only log
transport/rendering remains unresolved.** The source has no reporting length
limit, but that alone cannot resolve the distinction. No speculative capture
change was made.

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
