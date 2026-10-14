# Preview lifecycle verification

## Scope

Operational verification date: 2026-10-13 (UTC). This record covers one simple, single-service fixture and one pull request in one serial five-event trial. The functional observations below do not establish capacity or general reliability.

## Verified observations

| Event | Core / HTTP | SHA and identity | Terminal feedback | Status CLI | Independent cleanup |
| --- | --- | --- | --- | --- | --- |
| opened | 16/16 checks passed; HTTP 200 | Requested = served; baseline identity | Fresh success status + linked comment | ready; exit 0 | Not applicable while open |
| synchronize | 19/19 checks passed; HTTP 200 | New SHA matches; namespace/URL/UID unchanged | Fresh success status + linked comment | ready; exit 0 | Not applicable while open |
| closed | 3/3 checks passed; HTTP not applicable | Cleanup; no served-health comparison | Fresh success status + linked comment | removed; exit 0 | Namespace and related resources absent |
| reopened | 19/19 checks passed; HTTP 200 | Requested = served; same namespace/URL, recreated UID | Fresh success status + linked comment | ready; exit 0 | Prior namespace absence confirmed |
| closed (final) | 3/3 checks passed; HTTP not applicable | Cleanup; no served-health comparison | Fresh success status + linked comment | removed; exit 0 | Namespace and related resources absent |

## SHA and identity bounds

- Initial requested SHA matches the served health revision: true.
- Updated requested SHA differs from the initial SHA and matches the updated served health revision: true.
- Namespace name, preview URL, and namespace UID remain unchanged across synchronize: true for all three comparisons.
- Namespace absence was confirmed before reopening. The recreated namespace has a different UID; this is permitted after confirmed cleanup.
- Comparisons are reported without identifiers or addresses.

## PR terminal feedback and independent cleanup

Each terminal comment was matched to its exact commit status, revision, publisher, and event-start cutoff. Five distinct terminal status/comment pairs were observed. The actual status CLI exited successfully for every event, reporting ready for open previews and removed after closure.

Both closures independently confirmed Namespace, Deployment, Service, and Ingress absence. The pull request finished closed and unmerged. Cleanup results did not export namespace UIDs, so these observations do not establish a UID-bound deletion audit, absence of all orphaned resources, or removal of external resources.

## Limitations

Access was observed from the deployment runner only. Independent Windows browser/client and LAN access verification is unavailable for this trial. No claims are made for other workload classes.

Complete resource/timing measurement windows and formal performance statistics are unavailable. Archived stage/workflow intervals are individual observations, not a complete measurement dataset. Concurrency, capacity, and general reliability were not measured by this single serial trial. Formal dataset eligibility: INELIGIBLE.

Unsuccessful workflow evidence and observer-guard rejection/recovery evidence are retained. The update used one actual source push; recovery recollected observations without replaying a lifecycle event. The original disagreeing HEAD was not captured and remains unknown; no explanation of that original comparison is claimed. Retained failures are not reclassified as passes.

## Evidence manifest

Raw artifacts remain private. The frozen bundle is referenced only by its manifest SHA-256:

```text
6f1a18075fa5b41995750e24006c668b2b472dd148e6f68418e0ab27542ecd9b
```
