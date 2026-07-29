# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

## TrafficVisson source of truth

- `../Pretendard와 리퀴드 글래스 디자인/uploads/pasted-1785143633633-0.png` is the sole visual reference. Match its calm blue-gray matte glass shell, generous whitespace, restrained blue emphasis, black pill CTAs, radii, and hierarchy.
- `../Pretendard와 리퀴드 글래스 디자인/TrafficVisson Desktop.dc.html` is a user-authored functional and information-architecture specification only. Preserve its nine workflows and Korean copy, but do not ship its `support.js`, fixed 1920×1080 canvas, inline-style runtime, fake controls, developer HANDOFF panel, or placeholder media.
- The production target is an Electron-based Windows desktop application. The Vite page is the renderer and local visual-verification harness, not a web SaaS.
- The application UI uses the locally bundled `SUIT Variable` family. Do not regress Korean product copy to Pretendard, a system-only fallback, or a monospace face; reserve the bundled mono family for short technical hashes and timestamps only.
- The renderer fills the native Electron client area as one product surface. Do not reintroduce a rounded browser/mockup frame, development menu, or large decorative outer margin inside the Windows window.
- Use semantic native controls, visible focus, locally bundled fonts/icons/assets, responsive layouts from 1280×720 upward, and a single shared playback time for map/video/timeline/evidence synchronization.
- At 1280×720, the app shell and each primary workflow must not create a document-level scrollbar. A data-heavy list, evidence viewer, map, or document preview may own one clearly bounded internal scroll region only when its content length genuinely varies; never stack multiple simultaneous vertical scroll owners in the same task flow.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.
