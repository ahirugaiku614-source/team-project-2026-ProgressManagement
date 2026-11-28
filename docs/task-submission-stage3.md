# Stage 3: task collection and immutable submission snapshots

Implementation is in the saved-folder-submission worktree only. The default
provider still denies all real access. No code-server mount, student files, Linux
environment, real database, frontend or extension has been connected or changed.

## Collection boundary

`ManagedSnapshotProvider` requires explicitly injected source, authorization and
saved-session attestations. There is no environment variable enabling it. The
source supplies a managed User/Project root FD and directory operations; the
registered Task relative folder is resolved by `open_submission_folder`. The
collector never calls `ReviewGit.capture()` and never walks sibling tasks.

Linux `CollectionOps` reuses Stage 1 FD traversal and `ReviewGit.read` bounded
O_NOFOLLOW/O_NONBLOCK reads. Entries must be owned by the approved UID, on the same
device, not group/other writable. Symlinks, hardlinks and special files are rejected
before exclusion. Directory-open identity, file identity/metadata/owner before and
after reads, directory listings and two complete observations (including content)
must agree. Root/ancestor safety comes from the managed source and Stage 1 handles.

Limits: 1000 files, 1 MiB/file, 10 MiB total, 2000 scanned entries, depth 20,
30-second collection deadline. Entry names use the existing canonical relative
path policy. Empty tasks reject. Fixed exclusions include .git/.gitignore/
.gitattributes, .env/.env.*, .ssh/.aws, .venv/venv, node_modules, __pycache__, dist,
build, coverage, .pytest_cache, private key suffixes and known credential names.
Unknown hidden paths reject under the existing path policy. A student's .gitignore
cannot hide source files. This is not complete secret detection or a simultaneous
filesystem snapshot against a hostile writer. Excluded directory contents are not
read. Output modes are data-only 100644; code, hooks, lint, tests and builds are not
executed. No body, credential or raw exception is returned or logged by this code.

## Git and journal protocol

`SubmissionReviewGit` reuses server-owned ReviewGit sessions, locks, object store,
safe environment and hash-object/mktree/commit-tree plumbing. Student .git/config,
filters, hooks, external protocols and worktree Git operations are not used.

Within the project lock:

1. Recheck current authorization and saved-session evidence.
2. Exclusively publish and sync `<submission-id>.intent` in submission-journal.
3. Collect the registered task; recheck authorization/session evidence.
4. Create a parentless commit containing only the task-relative files.
5. Exclusively publish/sync `<id>.prepared` with identity and receipt metadata.
6. Recheck authorization; acquire packed-refs.lock and the dedicated ref lock.
7. Publish `refs/task-submissions/<submission-id>` without overwriting any ref.
8. Recheck authorization/session after Git publication, then publish/sync
   `<id>.complete`; independently reread journal/ref/commit/tree/blobs.

An existing intent prevents recollection even if an earlier attempt crashed before
publishing a receipt. Shared main and refs/change-requests are never changed.
Journals contain identifiers, folder binding and receipt metadata, not file bodies
or permit tokens. A snapshot commit is immutable by convention and protected by
server ownership; a privileged administrator can still mutate/remove refs/objects.
Real Linux fsync, renameat2 and crash durability have not been tested here.

## Database consistency and explicit recovery

Before collection, submissions.py reacquires current authorization after the
permit/admission commit and checks PROCESSING plus consumed permit evidence.
After collection it rechecks authorization/state and verifies Git evidence before
SUBMITTED. SUBMITTED is this implementation's confirmed state; no new CONFIRMED
enum is introduced and Task.DONE is unchanged.

Git success followed by DB failure leaves the Git commit/ref/journal intact and
the DB PROCESSING or REVIEW_REQUIRED. Repeating the POST does not collect again.
For already SUBMITTED retries, Git evidence is verified and mismatches downgrade
to REVIEW_REQUIRED instead of returning confirmed success.

`reconcile_submission` is an explicit internal service, not a new public API or
automatic task. It reauthorizes and reads the existing complete journal, immutable
ref, parentless commit and tree/blob evidence only. It can finalize a matching
snapshot after DB failure without opening the source. Incomplete intent/prepared
records, missing refs, corruption or DB/Git disagreement remain REVIEW_REQUIRED.
It does not recreate refs, recollect, roll back, delete or overwrite evidence. A
PROCESSING request with no complete evidence cannot be automatically resumed.
The implementation intentionally prefers manual review over repairing partial refs.

## Verification boundary

Windows tests use a memory-only FD tree and an isolated disposable bare Git.
PortableGitFixture reuses product tree/entries logic but substitutes its Git runner,
journal I/O and update-ref create-only transactions. Thread serialization uses a
test RLock. These tests do NOT establish Linux FD-relative traversal, Linux
packed/loose ref locks, renameat2, UID permissions, process-level flock behavior,
PostgreSQL row locks or power-loss durability. No real student file is read.

The production truthfulness of dirty-document/session attestations remains
unresolved. A client flag is not proof. A real source/session provider and secure
transport must be reviewed separately before any integration. Existing default
provider remains DENY. A2 incomplete, B1 unapproved, SSH255 cause H are unchanged.

## Test results

Stage 3 defines 41 tests: fake FD safety/limits, task and student scope, fixed
exclusions, two-pass content checks, real disposable Git trees and immutable refs,
threaded duplicate/distinct requests, journal corruption, Git/DB failures and
explicit reconciliation without source access. Full regression before the final
two additions: 121 PASS, 7 SKIP; the 9 affected tests including both additions then
passed separately. Final unique coverage is 123 PASS / 7 SKIP (41 Stage 3 and 82
related tests passed). Skips are five Linux storage tests and two PostgreSQL-only
tests. No external connection attempts occurred. Initial fixture path mismatch and
ORM expiry/autoflush recovery failures were corrected; historical failed runs are
not counted as successful verification. Old worktree seven files still match their
backup byte-for-byte; backup contains ten files and is unchanged by this work.
