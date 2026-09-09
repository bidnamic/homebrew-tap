# ECS Exec session keepalive — design

**Date:** 2026-06-03
**Status:** Validated design, pending live experiment before implementation.

## Problem

`bidnamic-os connect` opens an interactive shell into the user's ECS Fargate
task via `aws ecs execute-command --interactive` (see `connect_to_task` in
`launcher/bidnamic_os.py`). That session runs over AWS Systems Manager Session
Manager, which **terminates the session after 20 minutes of idle time**.

For ECS Exec this 20-minute idle timeout is **hard-coded and not
configurable** — there is no Session Manager preference that raises it. The
cost to the user is twofold and weighted equally:

- **Lost work** — a command, editor, or REPL state dies when the session drops.
- **Reconnect friction** — having to re-run `bidnamic-os` (and possibly re-auth).

## Approach

A **client-side keepalive**: wrap the `aws ecs execute-command` process in a
local pseudo-terminal (PTY) and, every 10 minutes, toggle the PTY window size
by one column and back. That fires `SIGWINCH` in the AWS CLI, which makes the
session-manager-plugin forward a terminal `set_size` control message to the SSM
agent — a harmless signal (no bytes injected into the remote shell) that we
hope the idle timer counts as activity.

### Critical caveat — must be validated first

The idle timeout is enforced **service-side** (AWS Message Gateway Service),
not in the open-source SSM agent. Whether a `set_size`/resize message counts as
"client input" (vs. only stdin byte payloads) is **undocumented and only
determinable by experiment**. We therefore gate implementation behind a live
test. Even if it works, we accept that we depend on undocumented behaviour AWS
could change.

Rejected for now (kept as fallback if the experiment fails): server-side
persistence by running the remote shell inside `tmux`/`screen` so a drop is
harmless and reconnect re-attaches. That lives in the container's
`start-bidnamic-os.sh` (a different repo), so it is out of scope for this change.

## Section 1 — Validation experiment (the gate)

Empirical proof is the only proof. The timeout cannot be shortened, so the test
takes ~25 minutes.

**Harness:** build the keepalive as a near-final, self-contained PTY wrapper
(productionizing the reference snippet) and run it *standalone* first. If it
passes, the same code folds into `connect_to_task` — the experiment doubles as
the prototype.

**Protocol (self-measuring, unattended):**

1. Start a session through the wrapper, keepalive firing every ~5 min.
2. At the remote shell type one command, then **never touch the keyboard
   again**:

   ```
   sleep 1500 && echo "KEEPALIVE_OK $(date)"
   ```

   (1500s = 25 min.)
3. Walk away ~26 min.

**Reading the result:**

- `KEEPALIVE_OK …` prints → session survived 25 min of zero stdin → **resize
  resets the timer → approach validated.**
- Session drops (wrapper exits) before that → resize does not count → abandon
  the client keepalive and reconsider the tmux fallback.

The `sleep && echo` probe works because remote *output* does not reset the idle
timer (only client input does) — so nothing but our resizes keeps the session
alive. No false positives.

## Section 2 — Wrapper integration

New function, roughly `exec_with_keepalive(argv) -> int`:

- `pty.fork()`; child `execvp`s the `aws ecs execute-command …` argv.
- Parent puts stdin in raw mode, pumps bytes both directions via `select`.
- Forwards real user `SIGWINCH` (terminal resize) to the child PTY.
- Every `KEEPALIVE_INTERVAL = 600` seconds, toggles the PTY size by one column
  and back (the nudge).
- Returns `os.waitstatus_to_exitcode(status)`.

`connect_to_task` builds the **same argv it builds today**; the only change is
it calls `exec_with_keepalive(argv)` instead of `subprocess.run(argv)`.

**Preserved behaviour:**

- **Exit code** propagates (callers `sys.exit` on it).
- **Ctrl-C**: in raw mode the `0x03` byte flows to the *remote* shell (correct
  for an interactive session) instead of killing the launcher. The current
  explicit `KeyboardInterrupt → 130` handling is therefore removed.
- **Terminal always restored**: `termios.tcsetattr` in a `finally`, so a plugin
  crash cannot leave the terminal wedged.

**Edges:**

- **stdin not a TTY** (piped/CI): `tty.setraw` would fail and a keepalive is
  pointless — fall back to the current plain `subprocess.run` path.
- **Flicker cost**: every nudge fires `SIGWINCH` in the remote, so full-screen
  apps (vim/tmux/less) repaint once per 10 min. Unavoidable for any
  resize-based keepalive; accepted.

**Configurability:** always on, no flag (YAGNI — add an opt-out only if the
flicker bothers someone).

## Section 3 — Testing & verification

- **Acceptance gate:** the Section 1 live experiment. Nothing automated can
  prove a service-side timeout behaviour.
- **Automated tests:** none. The repo has no test suite today; we match that
  bar rather than introduce a runner for hard-to-test PTY plumbing.

(Considered but declined: AWS-free smoke tests for exit-code propagation, byte
round-trip via a `cat` child, and the non-TTY fallback.)

## Reference snippet

The user's reference implementation (basis for `exec_with_keepalive`):

```python
KEEPALIVE_INTERVAL_SECONDS = 600  # every 10 minutes
# pty.fork() + execvp; raw stdin; select() pump; SIGWINCH forward;
# periodic toggle of PTY columns by 1 to emit a harmless resize;
# restore termios in finally; exit via waitstatus_to_exitcode.
```
