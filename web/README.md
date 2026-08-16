# OpsGraph alpha UI

Build-free, dependency-free public-alpha interface for OpsGraph. The interface is intentionally trust-first: source, policy, runtime isolation, query decisions, evidence provenance, and replay are visible throughout.

## Preview

The canonical packaged UI is under `src/opsgraph/web`. This root copy exists as
a browser-preview mirror and is kept byte-for-byte in sync by release checks.

From `opsgraph-alpha/web`:

```bash
python3 -m http.server 4173
```

Then open `http://127.0.0.1:4173/`. The product server mounts this folder at
`/assets` and serves `index.html` from `/`.

## Integration

`src/opsgraph/web/index.html` loads local assets from `/assets/static/`, matching
the FastAPI mount.
Replay calls `POST /api/investigations/sample` with an `X-OpsGraph-Key` entered by
the operator. The key is held only in `sessionStorage`; it is not persisted in
local storage or embedded in the page. Interactive data is the fictional,
synthetic alpha sample; denied states are deliberate product behavior, not live
database access.

## Accessibility baseline

- Body copy is at least 16px.
- Inspector tabs use the ARIA tabs pattern and arrow-key navigation.
- Drawers trap focus and close with Escape.
- Focus rings are always visible for keyboard users.
- Layout uses `100dvh`, independent scroll regions, reduced-motion support, and responsive breakpoints down to 320px and high zoom.
- No fonts, scripts, images, or other assets load from a network.
