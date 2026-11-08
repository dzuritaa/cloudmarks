# ADR 0004 — Bounded socket reception and asynchronous storage-volume proofs

Date: 2026-11-07. Status: **accepted**.

## Evidence

The single-connection helper answers an artificial normal request in 0.17 ms
(p50), but an incomplete request ahead of it raises latency to 581 ms, near the
600 ms client deadline. A cold volume proof also runs `findmnt` on the socket
thread with a five-second timeout. Folder observation repeats unused SQLite
reads and rebuilds the same remote manifest each second.

The [measurement record](../dev/status-performance.md) contains reproducible
1k/10k/50k fixtures and full before/after JSON. No real account, desktop or mount
was used.

## Decision

- Multiplex framing and response writes for at most 16 same-UID peers with a
  selector. Each has one bounded 16 KiB frame and an absolute 600 ms deadline.
  Status evaluation and SQLite reads remain serial on their owner thread;
  there is no state writer or transport in the socket path.
- Read-only volume checks use one short-lived background worker, at most 16
  pending identities and 16 proofs. Queue refresh after half the reuse interval
  (5 of 10 seconds). The interval starts before `findmnt`, not after completion.
  Cold/expired evidence fails closed; there is **no stale grace**. A failed or
  mismatched refresh removes the prior proof, with one-second failure backoff.
  Reuse still checks device/inode and binding. Mutations never use this cache and
  always verify synchronously; their failure also invalidates a cached proof.
- Cache the derived folder manifest with its remote tree, preserving the original
  observation deadline. Query a member's state row only when a retained copy
  exists. Do not enlarge the tree, hash, engine aggregate or desktop budgets.

## Consequences and limits

An initial badge may briefly show Needs checking while its volume proof arrives.
A slow peer no longer holds the listener, but 16 deliberately idle same-UID peers
can still consume its bounded admission set until their deadlines. Local stat,
binding reads, SQLite and small inline hashes remain synchronous; a stuck kernel
filesystem call can still delay the helper. Multiplexing does not make SQLite
concurrent and introduces no runtime dependency.

The artificial observer's 10k p50 improves from 230 to 120 ms; 50k aggregate
requests remain unknown because the tree limit is 10k. The legacy engine path
still parses an oversized manifest before checking its budget; moving that guard
into a bounded state query is future core-module work. The desktop queue admits
512 items per burst; deferred items require later Files callbacks. These bounds
protect responsiveness, not a promise of complete badges in an arbitrarily large
folder. Physical disk/desktop behavior still needs opt-in acceptance testing.
