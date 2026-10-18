# State, events and cancellation

Operations, attempts and explicit resume are durable. See [recovery](recovery.md).

```text
operations/<operation_id>/
  input.json              Immutable OperationInput
  events.jsonl            Recovery journal
  state.json              Atomic snapshot
  owner.lock              One live worker
  control.lock            Short state read/write transactions
  cancel.json             Explicit cancellation request
  worker.log
  attempts/N/progress.log
  attempts/N/logs/*.stdout.log, *.stderr.log
  workspace/source, build, output
```

State includes operation kind/ID, input hash, request ID, owner/token, attempt,
sequence, timestamps, progress, resources, result and error. Statuses progress from
planned to running and then succeeded/failed/cancelled/partial. Owner loss produces
interrupted; startup failure can terminate planned directly. Cleanup may append
terminal-state events and update references. Only `operation_resumed` starts a new
planned attempt, incrementing attempt with no live resources. Successful runs cannot
resume. Partial means some branches succeeded while another failed; its exit code is 5.

A single owner writes JSONL. The parent creates initial records and transfers an open
flock descriptor to the worker. Durable ordering is completed/verified result, event
with fsync, then state snapshot with file and directory fsync. Events contain UTC,
contiguous sequence starting at 1, operation ID, null run ID, target/step IDs,
attempt ID, type and bounded state in data.state. Full command output stays in files.

Readers stream-validate the entire journal. After owner loss, the last event can
reconstruct the snapshot. Only a final unterminated record may be discarded;
complete invalid JSON or middle-of-journal corruption produces `state_corrupt`.
Event cursors return at most limit entries, the last emitted sequence and has_more.
Reading events does not mutate the journal.

Cancellation is stored independently and consumed by the worker; worker SIGTERM also
requests cancellation. Closing the CLI does not signal the worker. Resource intent,
name and labels are durable before Docker create. PID/start ticks/UID/boot ID are
recorded before waiting on subprocesses. Cancellation after SIGKILL can therefore
stop only verified processes and containers. Status detects owner loss but does not
retry commands. Failed cleanup retains resource references.

The worker verifies input identity before side effects. Request IDs provide
idempotent startup within one operations directory. Different inputs need a new
request/operation ID. There is no automatic retry after failure; explicit resume
verifies inputs, checkpoints and old resources.
