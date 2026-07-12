# Changelog

All notable changes to telemetry-mcp are documented here. Versions follow
[semantic versioning](https://semver.org/) and are published as git tags
(`vX.Y.Z`). Entries are derived from the merged history at each tag.

## [v0.3.0] - 2026-07-12

### Added
- Bounded, catalog-driven BigQuery read adapter with parameterized queries, scan caps, and offline coverage (#16).

## [v0.2.0] - 2026-06-18

### Added
- At-will telemetry emit MCP server (#7).

### Changed
- Call the reusable Python CI workflow (#6).
- Unify the `selamy-skills` plugin pin to v0.34.0 (#4).
- Add CI and license badges to the README (#5).
- Scrub internal issue references from the public README (#3).

## [v0.1.0] - 2026-06-17

### Added
- Initial release: a read-only metrics MCP server with tools over an injected backend (#1).
- Declare the `selamy-skills` plugin pinned (v0.27.3) (#2).
