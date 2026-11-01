# Changelog

## 0.5.0

First release of the maintained cgfm fork.

- Merge the current-HA vacuum battery migration and expose a dedicated battery sensor.
- Renew S10 AWS map credentials using the app policy endpoint, keep SDK/TLS initialization off the event loop, persist discovered rooms, and retain the cache on failures.
- Preserve upstream K10+ controls, button/select platforms and privacy fixes.
- Add the authenticated S10 map image, last completed cleaning route and a timestamped last-received pose marker during active tasks.
- Reject overlays for another map and expire stale pose reports; no claim of live docked-position support.
- Use generic entity naming and retain entity unique IDs; include no personal floorplans, credentials, device identifiers or dashboards.
- Update stale test mocks/status expectations to match existing runtime behavior. Full suite: 162 passed on Home Assistant 2026.10.4 / Python 3.14.7 (plus 48 unittest subtests).
