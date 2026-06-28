# Wiki Graph View Dependency Evidence

Issue: Chainlink #693
Date: 2026-06-28

## Graph Size Measured

This Worklink checkout does not include a runtime wiki:

- `/work/repo/state/wiki`: absent, `0` markdown pages found.
- `/mimir-home/state/wiki`: absent in this worker.
- Backend contract fixture for the dashboard graph shape: `2` pages and `1` resolved edge in `tests/test_wiki_backlinks.py::test_build_wiki_payload_returns_json_friendly_page_and_graph_shape`.
- Frontend graph test fixture used for health states: `3` pages, `2` resolved edges, `1` dangling target.

## Dependency Decision

`reagraph` was evaluated as the heavier option for an interactive graph renderer. Given the actual available wiki graph size in this worker is unavailable/zero, and the exercised contract/test sizes are single-digit nodes and edges, adding a graph-rendering dependency is not justified for this leaf issue.

The accepted implementation uses a lazy-loaded local SVG graph component instead:

- `WikiGraphView` is loaded only when the wiki `view=graph` mode is active.
- The default wiki reader path does not import the graph module eagerly.
- Nodes are colored by category and health markers distinguish orphans, dangling targets, and slug collisions.
- Node clicks route back into the existing read-only wiki reader.

If production wiki graphs grow into hundreds or thousands of nodes with pan/zoom/layout requirements, `reagraph` or another graph library should be reconsidered with a measured production payload and bundle-size comparison.
