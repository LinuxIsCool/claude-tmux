# Tombstone every destructive operation on `.state` files

- **Date:** 2026-08-06
- **Status:** design, awaiting review
- **Branch:** `feature/state-tombstone-destructive-ops`
- **Author:** Claude (claude-opus-5, Claude Code)

## Problem

`claude-tmux` binds a Claude Code session to a tmux pane by storing the session
UUID in a file **named after the pane position**:

```
~/.local/share/tmux/claude-sessions/<tmux_session>:<window>.<pane>.state
```

Position is not identity. Positions are reused constantly; sessions are unique
and permanent. Keying the record by the reusable half means the record is
destroyed whenever the position turns over.

There are four operations that remove a `.state` file. Only one of them leaves
any trace:

| Operation | Location | Fires when | Recoverable |
|---|---|---|---|
| `mv -f "$tmp" "$target"` | `lib/common.sh` `save_session_state` | a different session takes an occupied slot | **no** |
| `rm -f "$other"` (Fix 3 dedupe) | `lib/common.sh` `save_session_state` | a session appears in a new slot; its old file is deleted | **no** |
| `find -name '*.state' -mtime +7 -delete` | `scripts/claude-save-sessions.sh` | age | **no** |
| move to `.tombstones/` + meta record | `scripts/claude-tombstone-gc.sh` | JSONL missing, or empty session ID | yes |

### Evidence: the 2026-08-06 incident

The tmux server crashed at 19:37:30 with seven live Claude sessions across three
windows. The recovery session started 16 seconds later and landed in slot
`0:1.1`, whose `SessionStart` hook immediately overwrote `0:1.1.state`:

```
SESSION_ID=c511ae9f-...   # recovery session, written 19:37:52
```

destroying the only record that `a5a795a2-...` (an in-flight TELUS capstone
thread) had been there. Recovery succeeded only because
`session-map.tsv` still held a copy from the 19:30 periodic save.

**That copy was luck, not architecture.** `claude-save-sessions.sh` rebuilds the
map from the current `.state` files and atomically replaces it:

```sh
for state_file in "${STATE_DIR}"/*.state; do ... done
mv "$TMP" "$MAP_FILE"
```

The map is a **derived mirror on a 15-minute delay**, not an independent record.
The clobber propagates into it at the next save. Two ways tonight could have
gone differently:

1. A crash at 19:44 instead of 19:37 would have lost the ID from both files.
2. Running `claude-save-sessions.sh` during recovery *before* rebuilding the
   panes (a natural "refresh state" reflex) would have rebuilt the map from the
   clobbered state file and destroyed the last copy. This nearly happened.

The dedupe path is the same data loss from an unrelated cause and was not
involved in this incident. It will eventually cause one.

## Goals

1. No `.state` file is destroyed without a recoverable record of what it held.
2. Recovery becomes a defined procedure rather than improvisation, and does not
   depend on periodic-save timing.
3. Reuse the tombstone convention already proven in `claude-tombstone-gc.sh`.
4. The save path never breaks because tombstoning failed.

## Non-goals

**Re-keying state files by session ID.** This is the correct eventual fix: make
the filename the session UUID and demote position to a field. It would delete
the Fix 3 dedupe outright, because dedupe exists *only* to compensate for
position-keying.

Deferred deliberately. It rewrites `save_session_state`, `claude-restore.sh`,
`claude-save-sessions.sh`, `claude-tombstone-gc.sh`, and the on-disk layout, in
the subsystem whose entire purpose is surviving surprise. A defect there costs
exactly what the system exists to protect.

This work is not throwaway under that future change. Tombstone-before-destroy is
the right invariant under either schema, and the accumulated history gives a
migration a corpus to validate against.

**Making `session-map.tsv` merge-preserving.** Rejected on purpose.
`claude-restore.sh` looks up by position, so a map retaining stale position rows
could resurrect the wrong conversation into a slot. That is the same failure
mode as resurrect's bulk restore, reintroduced by the fix. History stays out of
the file the restore path reads.

## Design

### Separation of concerns

- `session-map.tsv` remains a **pure derived index**: rebuilt from state, always
  consistent with the present, carries no history. Mirrors are fine once
  everyone knows they are mirrors.
- `.tombstones/` becomes the **append-only history** of every binding that has
  ever existed.

Recovery procedure becomes: read the map; if a slot's session looks wrong or
absent, read the tombstones for what used to be there.

### Component: `tombstone_state_file()`

New helper in `lib/common.sh`. Single writer for all tombstone creation;
`claude-tombstone-gc.sh` is refactored to call it so there is exactly one
implementation of the convention.

```
tombstone_state_file <path-to-state-file> <reason>
```

Behavior, matching the existing GC convention exactly:

1. Return success immediately if the file does not exist (idempotent).
2. `mv` (never `rm`) to `${TOMBSTONE_DIR}/<basename>.<UTC-ISO8601>Z`.
3. Append one JSON line to `${TOMBSTONE_DIR}/tombstone-meta.jsonl`:
   ```json
   {"ts":"...","tombstone":"...","origin":"...","reason":"...","session_id":"..."}
   ```
   `session_id` is new. The GC's current record omits it, forcing a reader to
   open the moved file. Denormalizing it makes recovery greppable in one pass,
   which matters when the reader is a stressed operator or an agent.
4. Never write outside `TOMBSTONE_DIR`.

**Locking precondition.** The helper does **not** acquire `$STATE_DIR/.lock`.
`save_session_state` already holds it on fd 9 across its write/dedupe sequence,
and re-opening fd 9 inside would clobber the caller's descriptor. The contract
is: *callers must already hold the lock.* Documented in the function header and
asserted by review, not at runtime. The age-prune call site does not currently
lock and must be wrapped.

### Call site 1: clobber (`save_session_state`)

Before `mv -f "$tmp" "$target"`, tombstone `$target` **only when a different
session is taking the slot**:

```sh
if [ -f "$target" ]; then
    prev=$(grep '^SESSION_ID=' "$target" | cut -d= -f2)
    [ -n "$prev" ] && [ "$prev" != "$session_id" ] \
        && tombstone_state_file "$target" clobbered
fi
```

The inequality guard is load-bearing, not an optimization. `save_session_state`
fires on `SessionStart` and again on a second hook event, so the same session
re-saves into the same slot repeatedly. Without the guard, every routine re-save
tombstones itself and the directory becomes noise that buries the rare record
that matters. With it, a tombstone means precisely "a slot changed hands."

### Call site 2: dedupe (`save_session_state`)

Replace the unconditional delete:

```sh
-  rm -f "$other"
+  tombstone_state_file "$other" deduped
```

### Call site 3: age prune (`claude-save-sessions.sh`)

Replace `find ... -delete` with a loop that tombstones each match, wrapped in
`flock` on `$STATE_DIR/.lock` to satisfy the helper's precondition.

Ordering note: the prune currently runs *before* the map rebuild, so pruned
sessions vanish from the map in the same pass. That ordering is preserved; the
difference is only that the file survives in `.tombstones/`.

### Reason codes

Existing: `empty_session_id`, `jsonl_missing`.
Added: `clobbered`, `deduped`, `aged_out`.

### Retention

Volume today is 341 tombstones since 2026-07-01 (~9.5/day) from GC triggers
alone. With the inequality guard above, `clobbered` fires only on genuine slot
turnover, so the expected increase is single-digit per day rather than per-pane.

Retention rule, value-based rather than arbitrary:

> Prune a tombstone when its `session_id` has **no live JSONL**, **and** the
> tombstone is older than 30 days.

Rationale: a tombstone exists to answer "what conversation can I resume here?"
A session whose transcript is gone cannot be resumed, so the record has no
remaining value. This reuses `session_jsonl_exists()` from `lib/jsonl.sh`, the
same predicate the GC already trusts. The 30-day floor guarantees recent records
survive regardless, covering transcript archival races.

Rejected: count-based caps. Dropping the oldest can discard a rare
slot-turnover record while retaining fifty routine ones from a busy hour.

**Enforced by** `claude-tombstone-gc.sh`, extended with a second pass over
`.tombstones/` after its existing pass over `.state`. It already runs every 15
minutes under `flock`, already sources `session_jsonl_exists()`, and is already
the component that owns retention policy. No new scheduler.

Retention **does** delete tombstones, and that is intentional rather than a
contradiction of the "never `rm`" rule below. That rule governs a *failure* to
record: never delete a state file because tombstoning did not work. Retention is
a deliberate expiry of a record proven valueless (no resumable transcript, older
than the floor). Pruned tombstones are removed, not re-tombstoned; there is no
second graveyard.

**This is the one decision made without explicit direction.** It is isolated to
one predicate and trivially changed.

### Error handling

Tombstoning is **best-effort and must never fail the save path.** Session
capture is the primary function; history is secondary. If the `mv` fails
(permissions, full disk, vanished file), log to `$RESTORE_LOG` and let the
caller proceed with its original operation. Matching the GC's existing stance:
an unwritable telemetry log does not fail the run.

The one hard rule: **never fall back to `rm`.** If a file cannot be tombstoned,
leaving it in place is strictly better than deleting it. A stale state file is a
recoverable nuisance; a deleted one is the bug being fixed.

## Testing

Shell-level tests against a temp `STATE_DIR`, following existing `scripts/test-*.sh`
patterns.

1. **Clobber records the predecessor.** Write slot with session A, save session B
   into the same slot, assert a `clobbered` tombstone exists containing A, and
   that the live file holds B.
2. **Same-session re-save produces no tombstone.** Save A twice; assert the
   tombstone directory is unchanged. Guards the inequality condition.
3. **Dedupe records rather than deletes.** Place A in one slot, save A into
   another, assert a `deduped` tombstone for the old slot.
4. **Age prune records rather than deletes.** Backdate a file past 7 days, run
   the save hook, assert an `aged_out` tombstone.
5. **Failure does not break saving.** Make `TOMBSTONE_DIR` unwritable, save into
   an occupied slot, assert the new state file is still written correctly and
   the exit status is success.
6. **Incident replay.** Reconstruct 2026-08-06: seven state files, clobber
   `0:1.1` with a new session, then run `claude-save-sessions.sh` (the move that
   nearly destroyed the evidence) and assert `a5a795a2` is still recoverable
   from tombstones after the map has been rebuilt from clobbered state.
7. **Retention.** A tombstone whose session has a live JSONL survives; one
   without a JSONL and older than 30 days is pruned; one without a JSONL but
   younger than 30 days survives.
8. **Mutation check.** Per house practice, break each guard and confirm a test
   fails. Specifically: remove the `!=` condition and assert test 2 fails;
   revert `deduped` to `rm -f` and assert test 3 fails.

Test 6 is the acceptance test. It is the incident, replayed, with the near-miss
included.

## Migration and compatibility

No migration. Existing tombstones keep their current shape; `session_id` is
additive and readers must tolerate its absence in pre-existing records.
`claude-restore.sh` is untouched. Nothing changes about how sessions resume; the
only difference is what survives a destructive write.

## Follow-up (out of scope)

1. Re-key `.state` files by session ID, position as an attribute. Removes the
   dedupe path entirely.
2. Teach `claude-restore.sh` to consult tombstones when a slot's state file is
   newer than the crash it is recovering from, closing the loop automatically
   instead of relying on an operator noticing.
