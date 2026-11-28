# Task submission backend: first implementation

Explicit POST endpoints issue a 120-second TASK_SUBMISSION permit and admit a
submission. Saving files does not invoke either endpoint or change Task.status.
No Vue, code-server extension, Linux configuration or real storage adapter changes.

## Contracts

- `POST /api/teams/{team_id}/projects/{project_id}/tasks/{task_id}/submission-permits`
  accepts only `editing_session_id` (UUID). Returns a random bearer permit once,
  expiry and purpose. Only SHA-256 of its 256-bit random value is stored. Responses
  are no-store; never enable request-body/access-token logging at proxy or app level.
- `POST .../submissions` accepts only `editing_session_id`, `request_id` (UUID),
  `permit`, and a strict boolean `has_unsaved_changes`. True rejects before admission.
  This client claim is not proof of saved state: a trusted collector must check it.
- Current membership, assignment and registered target are checked both at issue
  and use, and again before final publication. Binding changes invalidate permits.
- First admission returns 202 with PROCESSING, SUBMITTED or REVIEW_REQUIRED;
  repeats return 200 with the existing result. A request ID is unique per student
  and task. A changed session/permit/binding conflicts, never recollects files.
- Permit consumption and PROCESSING insertion commit together before collection.
  One permit admits at most one request. After a crash, PROCESSING is not resumed
  automatically. REVIEW_REQUIRED never triggers another collection. There is no
  automatic rollback of snapshots, repair, deletion or retry with a new ID.
- Task IDs in history are historical integers, without cascading Task foreign keys.
  User, Team and Project deletion is restricted while referenced. Task status is
  unchanged; teacher authorization and history GET endpoints are not yet implemented.

## Provider boundary

`SubmissionProvider` receives an immutable server-resolved target and session,
not a client path, URL, token, command or uploaded body. The default implementation
rejects both readiness and collection. Only tests override its FastAPI dependency.
No runtime configuration selects a real provider.

A future trusted provider must authenticate the editor session; reject dirty
documents; safely inspect stable saved files under the managed root; enforce
per-file/aggregate/count limits, symlink/hardlink/secret exclusions; persist an
immutable server-owned Git snapshot; and return a matching receipt. The backend
validates receipt identity, namespace, commit shape and aggregate bounds. This is
not independent proof that Git exists or the filesystem was safe: the fake provider
does not establish real Git, FS, concurrent-process or durability guarantees.

Provider failures/invalid receipts become REVIEW_REQUIRED with the permit still
consumed. If final database persistence itself fails, the durable admission remains
PROCESSING or an already committed SUBMITTED result; no automatic recollection.
Explicit recovery/status handling and Git journal reconciliation remain future work.
Database uniqueness and conditional consumption protect admission; PostgreSQL
locking/concurrent-client behavior still requires separately approved validation.

## Database deployment

005 and 006 are explicit operator migrations; new tables are excluded from startup
creation. Do not apply to a real database as part of tests or application reload.
Tests use isolated SQLite and fake collector receipts only. PostgreSQL deployment,
session pairing, authenticated transport, history UI and immutable Git persistence
are not connected. A2 incomplete, B1 unapproved and SSH255 classification H remain.

## Local verification

New API/provider/migration tests: 27 PASS. Stage 1: 29 PASS. Existing related
Project/database tests: 25 PASS, 7 SKIP (Linux storage and PostgreSQL-only cases).
The initial combined run was 77 PASS / 7 SKIP; four subsequently added failure
tests were run separately and all passed. No FAIL/ERROR, no external connection
attempts. This is not real Git persistence or PostgreSQL concurrency validation.
