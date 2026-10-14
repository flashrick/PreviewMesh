# Preview access verification

## Scope

Operational verification date: 2026-10-14 (UTC). One simple, single-service fixture completed one serial opened/access/closed trial. Actual Windows command-line access in a WSL2 mirrored setup passed, as did deployment readiness and independent cleanup. This is not a capacity or general reliability assessment.

## Access path and readiness

Windows command-line client → normal DNS/hosts resolution → host proxy listener 18080 → Traefik → Service ClusterIP → Pod.

| Layer | Verified observation |
| --- | --- |
| Windows / resolution | Native Windows client; normal resolution; connected address matches resolved address |
| Host proxy | Proxy/socket active; kernel bind matches; selected backend matches Traefik ClusterIP |
| Traefik / Ingress | Ingress address populated; Ingress hostname/backend Service/port mapping check passed |
| Deployment / Pod | Deployment available; application Pod ready |
| Service | ClusterIP assigned |
| Opened core checks | 16/16 passed, including deployment, ownership, artifact identity and served revision |

## Normal request and served revision

| Check | Result |
| --- | --- |
| Deployment artifact URL used without hostname/address substitution | Passed |
| Normal Windows DNS/hosts resolution without forced-address override | Passed |
| Actual Windows request to `/health`; client exit 0 | Passed |
| HTTP 200; response `status=ok` | Passed |
| Served commit SHA exactly matches requested deployment SHA | Passed |
| No redirects | Passed |

Revision and URL comparisons are reported as matches; identifiers and addresses are omitted. This was an actual client request, not a function return value or runner-only HTTP observation. A separate forced-resolution diagnostic would not establish normal DNS/hosts reachability.

## Feedback and closure

| Event | Terminal feedback | Actual status CLI | Functional outcome |
| --- | --- | --- | --- |
| Opened | Fresh matching commit status and PR comment passed | Exit 0; ready | Core 16/16; Windows request passed |
| Closed | Fresh matching commit status and PR comment passed | Exit 0; removed | Cleanup 3/3; independent absence passed |

The PR was closed unmerged and its branch retained. Independent observations confirmed Namespace absence and related Deployment, Service and Ingress absence. Cleanup UID was not exported, so these checks do not establish a UID-bound deletion audit or cleanup of unexamined external/orphaned resources.

## Observer limitations

Two optional observer derivations are unvalidated, neither passes nor proven configuration mismatches:

- `artifact_host_matches_config`: original result false because the raw `auto` suffix was compared without resolution.
- `preview_service_target_matches_registered_app_port`: original result false because target-port resolution was incomplete.

The attempted correlated receipt was not created (`KeyError: 'document'`; `derived_correlated_receipt_created=false`). No complete configuration-correlation claim is made. Independently, `preview_ingress_host_backend_service_port_matches` and core `release_hostname_matches_url` passed, alongside the actual normal-resolution Windows HTTP and SHA checks.

Optional Windows listener inventory is unavailable (`CimJobException`); listener presence is unknown (`null`) and the cause is unknown. No permissions or firewall failure is inferred.

## Client coverage and measurement limits

| Client/path | Coverage and prerequisites |
| --- | --- |
| Windows command-line client | Observed; normal request passed |
| GUI browser | Unobserved; requires an interactive browser session and recorded navigation |
| Separate physical LAN client | Unobserved; requires a separate allowed device, hostname resolution and a route to the listener |

Complete resource and timing measurements remain unavailable; cleanup UID evidence is missing. Both event records remain formally **INELIGIBLE** as a measurement dataset. Functional acceptance does not establish dataset eligibility, throughput, capacity or general reliability. Unobserved or unavailable paths are not classified as unsupported.

## Evidence manifest

Raw evidence remains private. Frozen bundle manifest SHA-256:

`480a621f7cfa4556a32635162348fba14985cf066d760facf4dbfa39ab9c3a91`
