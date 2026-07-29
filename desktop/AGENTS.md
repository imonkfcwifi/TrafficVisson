# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

## TrafficSuperVision source of truth

- `../Pretendard와 리퀴드 글래스 디자인/uploads/pasted-1785143633633-0.png` is the sole visual reference. Match its calm blue-gray matte glass shell, generous whitespace, restrained blue emphasis, black pill CTAs, radii, and hierarchy.
- `../Pretendard와 리퀴드 글래스 디자인/TrafficSuperVision Desktop.dc.html` is a user-authored functional and information-architecture specification only. Preserve its nine workflows and Korean copy, but do not ship its `support.js`, fixed 1920×1080 canvas, inline-style runtime, fake controls, developer HANDOFF panel, or placeholder media.
- The production target is an Electron-based Windows desktop application. The Vite page is the renderer and local visual-verification harness, not a web SaaS.
- The application UI uses the locally bundled `SUIT Variable` family everywhere, including technical hashes, timestamps, form controls, dialogs, and map controls. Do not regress any visible product text to Pretendard, a system-only fallback, or a monospace face.
- The renderer fills the native Electron client area as one product surface. Do not reintroduce a rounded browser/mockup frame, development menu, or large decorative outer margin inside the Windows window.
- Use semantic native controls, visible focus, locally bundled fonts/icons/assets, responsive layouts from 1280×720 upward, and a single shared playback time for map/video/timeline/evidence synchronization.
- At 1280×720, the app shell and each primary workflow must not create a document-level scrollbar. A data-heavy list, evidence viewer, map, or document preview may own one clearly bounded internal scroll region only when its content length genuinely varies; never stack multiple simultaneous vertical scroll owners in the same task flow.
- Report generation requests may carry only selected incident IDs and exact analysis provenance from the renderer. The bridge must reconcile and resolve active approved incidents from its project/job-scoped catalog and must never trust renderer-supplied issue, severity, action, OCR, location, time, or evidence content.

## North Star product contract

- TrafficSuperVision is one Windows Electron program, not a collection of independently operated feature screens. After one project setup, the normal user journey is `drop one driving video -> approve rights/start -> answer the prepared queue -> inspect automatically refreshed map/CSV/evidence/HWPX preview`.
- Normal work uses one primary Windows window. The nine workflows are switchable in-app views inside that window, not separate web apps or child windows; only the one-time project profile is a clearly identified modal over video intake.
- “One program” must never read as “one settings page.” Keep the current workflow name, the nine in-app destinations, and the active destination visibly legible in the shell at 1280×720; label the project profile explicitly as a one-time setup modal and keep it visually separate from the project home.
- The one-time project profile owns review mode, question policy, default sample-based HWPX template, detector/OCR/SAM2 model pack, Codex or Claude report provider, and output location.
- One durable, project-scoped `PipelineJob`/DAG must orchestrate import verification, media hashing/metadata/GPS preflight, 2fps dense indexing, 5-10fps candidate rescan, DINO/dedicated detector/OCR, SAM2 masking/tracking, temporal-spatial merge, approved 3Q/4Q reference search, structured vision proposal, and question-queue preparation. Persist stage state, progress, input/output hashes, model/config versions, retry/cancel/resume/error, idempotency, and provenance.
- A selected network folder means every supported top-level MP4/MOV in that stable folder, not one preferred file. Validate each file independently, enqueue one durable PipelineJob per valid video in deterministic order, show per-file and aggregate queue/review/completion/failure state in intake step 2, and advance to the next job after the current review completes. Never silently analyze only the first file.
- Do not require manual per-screen buttons for dense indexing, detector execution, merge, reference search, CSV generation, or report-draft preparation. The only normal approval boundaries are rights/start, question answers, model-training start, and actual HWP COM apply.
- After review completion, deterministically regenerate server-owned Annotation/Incident projections, synchronized map/timeline state, raw/reviewed CSV, evidence package, and HWPX `DocumentEditPlan` preview. Training and COM writes remain explicitly user-approved.
- Never turn a capture-start coordinate into a route. If no time-series GPS exists, request a GPX in preflight, continue non-map analysis honestly, and keep route playback unavailable.
- Keep `model_unavailable`, `failed`, `zero_detections`, `gpx_required`, `partial`, and `completed` distinct. Never store failure or missing capability as an empty success.
- Grounding DINO proposes object boxes, dedicated road models handle pothole/lane signals, OCR reads text, SAM2 proposes masks/tracks, and LLMs propose semantic labels/report language. None of them alone creates an approved truth. Codex/Claude CLI are structured-judgment and report providers, not bulk-frame detector engines.
- Before user approval, confirmed Annotation, Incident, training membership, and report incidents must all remain zero.
- The 3Q/4Q sample HWPX/PDF files are both exact report templates and `weak_unconfirmed` case sources. Preserve hashes and exact page/table/row/column/photo-slot/checkbox/opinion addressing; only user-confirmed cases become approved references.
- Final acceptance requires one real NAS video to pass through one PipelineJob and one provenance chain from new-project input through automatic queue creation, keyboard review, approved Incident, GPS playback when real timed GPS exists, CSV/evidence export, sample-template preview, HWP COM apply, reopen, and 48-page render validation. Until then, never claim “영상만 넣으면 알아서 된다”.

## Labeling interaction contract

- Quick review: `Y/N/U/E` saves once and advances immediately. Severity is explicitly unassessed.
- Precision review: `Y`, then `1/2/3` for low/normal/high on the same card, saves once, and advances immediately.
- Do not use a list or dropdown for severity. AI/model severity is a separate proposal field; only the user's choice is final.
- Debris and proposed emergency cases are mandatory in every mode.
- Region edit means one click requests a new SAM2 mask/track and then asks for confirmation. Do not present a moved temporary box as completed automatic segmentation.
- At 1280x720 the active card must show the representative frame, box/mask, adjacent evidence, merged-frame count, GPS state, type/score, 3-5 similar sample cases, proposed-severity rationale, shortcuts, and progress without document scrolling.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.
