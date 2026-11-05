# False working state in an idle tmux window

Date: 2026-11-04. Status: resolved by `34f5e15f5a0bbc0f0e864465521789d7fe4b7f04`.

An idle window containing three agents displayed a green working badge.
The arbiter accepted a hook against a reusable pane number, retained that
pane's native execution identity, and let the hook override native idle
evidence indefinitely. The correction validates event ownership before
mutation and limits busy-hook precedence.

## Evidence and cause

| Finding | Evidence | Consequence |
| --- | --- | --- |
| F1: Event identity was incomplete. | At 11:15:18 UTC−07:00, `PreToolUse` changed pane `%0` from idle to working. Its saved provider became `codex`, although its live owner was Claude and its native source was `claude-session`. The other two panes were idle. | The window aggregator selected working from the incorrectly marked pane. |
| F2: Reconciliation preserved the contradiction. | Event handling copied the previous native identity into the hook state. Reconciliation compared identity without provider, and a hook newer than unchanged native metadata could win indefinitely. | Reading native idle evidence again did not repair the badge. |
| F3: Test isolation was incomplete. | Some unit tests mocked tmux writes but retained production persistence paths. The shell smoke test sent events to production IPC. | Tests could alter operator state. This was an additional defect, not proof that a test produced the original event. |

The original sender remains unknown. The old transition log omitted sender,
session and socket provenance. A collision between private tmux servers is
reproducible, but the available record cannot attribute this event to a
particular server or test.

## Corrective changes

The [receiver policy](../../scripts/event_identity.py) now requires native
provider/session ownership and a current PID start time and pane ancestry.
New source envelopes also carry the exact tmux socket and timestamp. Supplied
turn IDs must match the native turn. Reconciliation repairs provider
contradictions and limits busy/dialog hook precedence to the configured grace
period. Quiet native working turns remain working.

Saved active badges are unvalidated cache entries on restart. Malformed rows
are skipped individually. Hook transport forwards bounded metadata, preserves
one valid hook response, and resolves helpers through canonical paths.
Installed services stay under systemd supervision. Automated tests use
private state and sockets with production-access guards.

The maintained rules and remaining transport, coverage and platform limits
are in [status ownership and recovery](../../README.md#status-ownership-and-recovery).

## Verification

- V1: The committed fix passed 233 tests and 15 subtests from an export containing only committed source. The 120 skipped tests required optional host data.
- V2: [Two real private tmux servers](../../tests/test_tmux_writer.py) with the same pane ID rejected foreign event and focus messages without redrawing the monitored server. A correctly bound event was accepted.
- V3: [Identity tests](../../tests/test_event_identity.py), [reconciliation tests](../../tests/test_lifecycle_reconcile.py) and [transport tests](../../tests/test_hook_transport.py) cover wrong owners, PID reuse, old turns, hook expiry, quiet inference, cache recovery, bounded payloads and hook-response isolation. Independent review passed after repairs to cache projection and response handling.

At 13:25:06 UTC−07:00, live verification recorded all three affected agents as
idle, restored `%0` to provider `claude`, and confirmed a blue idle window
badge. The arbiter was active and status-bar formats were unchanged. Agent
processes were not restarted.

To repeat the automated checks from the repository root:

```bash
uv run --with pytest --with 'hypothesis>=6.150' python -m pytest -q tests -W error::pytest.PytestCacheWarning
```
