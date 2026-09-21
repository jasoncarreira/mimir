# Coding startup requirements

Coding and retained-recovery work is admitted only after the applicable rows in
this closed registry have been probed. A **Fatal** result aborts applicable
coding startup, a **Warning** omits the dependent tools, and **Skipped** means
the row was not probed. When coding is disabled, only the feature-state row is
probed; all other rows are skipped and no coding configuration is required.

| Stable name | Probe | Expected | Observed | Remediation | Applies | Failure |
|---|---|---|---|---|---|---|
| `coding.feature_state` | Existing switch | enabled/disabled | Boolean | Configure as intended | Always | Pass; disabled skips below |
| `coding.opencode.executable` | PATH/realpath/file/mode | Executable | Path/missing | Install pinned OpenCode | Coding | Fatal |
| `coding.opencode.version` | fixed `--version`, 5s | `1.18.21` | Version/failure | Install `1.18.21` | Coding | Fatal |
| `coding.git.executable` | `/usr/bin/git` lstat | Regular executable | Type/mode | Install Git | Coding | Fatal |
| `coding.pr_checkout_lease_root` | Existing path/write probe | Valid root | Path/result | Repair config | Coding | Fatal; not containment |
| `coding.github.identity` | Existing identity probe | Resolved | Login/reason | Repair credentials | Coding | Warning |
| `coding.repositories.inventory` | Inventory load | Strict canonical schema | Named result | Correct inventory | Coding+repos | Fatal |
| `coding.repositories.root_mode_agreement` | Canonical path→mode maps | Exact equality | Both maps | Align roots/modes | Coding+repos | Fatal |
| `coding.worklink.target_rw_unique` | Resolve target | One canonical `rw` | Target/path/count/mode | Correct target | Ready queue | Fatal |
| `coding.worklink.git_binding` | Top-level/origin | Exact root/origin | Expected/actual | Repair binding | Ready queue | Fatal |
| `coding.factory.package_versions` | Entrypoint/manifests | Package-bound, both `0.9.2` | Paths/versions | Install `0.9.2` | Factory recovery | Fatal |
| `coding.factory.command_contract` | Isolated probe | Exact 16 commands | Commands/failure | Install compatible factory | Factory recovery | Fatal |
| `coding.worklink.worker_protocol` | Handshake | Matching protocols | Both versions | Rebuild worker | Retained recovery | Fatal |

Repository agreement compares canonical path-to-mode maps. Only repositories
selected as coding targets must be `rw`; unrelated repositories and allowed
roots may remain `ro`. A selected target must have exactly one canonical `rw`
binding in the effective authorization projection. The Git binding then proves
the selected checkout's exact top-level path and configured origin.

The feature-factory checks require the package-bound `factory.js` entrypoint,
both package manifests at `0.9.2`, and the exact sixteen-command non-mutating
capability contract. The worker handshake compares the controller and
image-owned executor protocol identities. These checks run before retained work
is dispatched; the PR checkout lease root remains an existing prerequisite and
is not used as retained-checkout containment.
