# ADR 0005 — Share badge evidence and bound encoded status replies

Date: 2026-11-07. Status: **accepted**.

## Evidence

An independent review reproduced a folder marked kept offline for a 9 MiB
member with no explicit verification proof, after hashing 18 MiB. The file's
own badge was unknown. Advancing the folder clock by 120 seconds did not
repeat either hash. A valid 16-path request also fitted the 16 KiB IPC frame
while its response exceeded that limit, causing every answer to become unknown.
The two doctor commands disagreed about state consistency problems, and a
successful Keep → Free left terminal cancelled jobs counted as unfinished.

## Decision

- FolderObserver keeps its own worker, but uses the same `copy_fingerprint`,
  `cached_fingerprint` and `HashMemo` policy as single-file status: hashes up to
  8 MiB, explicit proofs above it, 60-second rehash and failure memory keyed by
  kernel fingerprint. Mutations continue to verify synchronously without the
  memo. A folder lease is capped by every large member's proof deadline.
- One folder check hashes at most 16 MiB across both retained copies. A 150 ms
  elapsed budget is checked between members and reads of at most 1 MiB. Reads
  stop at the recorded size and check both fingerprints and SHA-256. No success
  is memoized after an interrupted hash; completed hashes allow later checks to
  progress. Time limits cannot interrupt an in-flight network or kernel call.
- The IPC client reserves space for both the encoded request and the largest
  valid result per path (256 Unicode reason characters, state and lease),
  splitting before either frame or the 16-path limit would be exceeded. Replies
  use UTF-8 JSON so a single valid Unicode path near the request limit can still
  carry its result. The 16 KiB frame limit and protocol validation stay intact.
- Both doctors use one state-health classification, including integrity
  problems, pending publications, issues, attention files and non-terminal jobs.
  `done` and `cancelled` are terminal. Storage and view checks remain separate
  from state health in the main doctor's report.

## Validation and limits

`tests/test_folder_evidence.py` compares single-file and folder status for small
files, large files with and without proofs, changed bytes and expired rehash;
it also checks byte/time budgets, failure memory and the explicit-proof lease.
Socket regressions cover 16 long ASCII paths and one near-frame Unicode path.
Doctor tests cover pin-flag inconsistency and Keep → Free → doctor.

Evidence caches are bounded. Large folders may never finish within the budget
and memo capacity; they remain Needs checking. The socket hash worker retains
its existing one-interval grace for an unchanged fingerprint during rehash;
the folder worker hashes synchronously at expiry. Neither path downloads or
writes state while answering status. No physical disk, desktop or cloud-account
acceptance result is implied by these artificial tests.
