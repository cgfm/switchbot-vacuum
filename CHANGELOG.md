# Changelog

## 0.6.0

- Support K10+ room cleaning and controls on US (`na1`) and Asia-Pacific (`ap1`) accounts as well as EU, choosing the 360 "botslab" region from the account's SwitchBot host. Verified on a US K10+.
- Report rooms as Home Assistant vacuum segments (S10 and K10+), so they can be mapped to areas and cleaned with `vacuum.clean_area`. A Repairs issue asks to update the mapping when rooms are added or removed.
- Make room sensors diagnostic and disabled by default. On upgrade, existing room sensors stay enabled and an optional Repairs issue offers to disable them.
- Add the K10+ map image: rooms with colours and labels, no-go zones and the dock, rotated to match the SwitchBot app. The map is downloaded again after the robot uploads a new one. A **Last map refresh** diagnostic sensor shows when it last succeeded.
- Draw the K10+ cleaning path, robot position and heading on the map, polling every 15 seconds while the robot is cleaning or returning. A **Last path update** diagnostic sensor shows when new path points last arrived.
- Full suite: 223 passed on Home Assistant 2026.10.4 / Python 3.14.10 (plus 48 unittest subtests).

## 0.5.0

First release of the maintained cgfm fork.

- Merge the current-HA vacuum battery migration and expose a dedicated battery sensor.
- Renew S10 AWS map credentials using the app policy endpoint, keep SDK/TLS initialization off the event loop, persist discovered rooms, and retain the cache on failures.
- Preserve upstream K10+ controls, button/select platforms and privacy fixes.
- Add the authenticated S10 map image, last completed cleaning route and a timestamped last-received pose marker during active tasks.
- Reject overlays for another map and expire stale pose reports; no claim of live docked-position support.
- Use generic entity naming and retain entity unique IDs; include no personal floorplans, credentials, device identifiers or dashboards.
- Update stale test mocks/status expectations to match existing runtime behavior. Full suite: 162 passed on Home Assistant 2026.10.4 / Python 3.14.7 (plus 48 unittest subtests).
