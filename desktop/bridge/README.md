# TrafficSuperVision local bridge

This directory is the Python service supervised by Electron main. It is not a
general web API.

Security invariants:

- The launcher binds an OS-assigned port on `127.0.0.1` only.
- Every route, including `/health`, requires the per-process bearer token.
- The token is received from Electron through the child environment, removed
  from `os.environ`, and never returned or logged.
- Renderer-facing IPC models reject unknown fields and arbitrary file paths.
  Electron main resolves opaque media, document, and destination selections
  before making the authenticated loopback request.
- The real-media intake exception is an Electron-main-owned, token-authenticated
  directory inspection call. It accepts one explicit absolute local or UNC
  directory, never returns that path, never recurses, never follows symbolic
  links/reparse points, and never writes to the selected directory.
- No CORS middleware is enabled. Host, client address, request size, response
  headers, provider names, edit operations, and export names are allowlisted.
- URL inspection rejects credentials, special-use IPs, local hostnames, custom
  ports, and mismatched provider hosts. A production downloader must also
  resolve DNS and recheck every redirect immediately before each connection.
- Provider login remains owned by the official CLI. This fixture never reads
  Codex or Claude authentication files.
- Report apply currently validates a schema-limited `DocumentEditPlan` and
  creates a preview record. It does not invoke COM, macros, shells, or arbitrary
  commands.

The in-memory fixture covers projects, imports, question/answer conversion,
history playback, providers, report planning, and export contracts. It is a
contract harness, not the production inference/training/downloader/COM engine.

## Read-only real-media intake

The production-oriented intake slice uses these project-scoped endpoints:

1. `POST /projects/{project_id}/media/local-file`
   is called only by Electron main after a native picker selection. The
   renderer sends an opaque UUID; main resolves exactly one canonical path and
   injects it into the token-authenticated bridge request. The bridge repeats
   regular-file, extension, symlink/reparse-point, and metadata checks and
   returns a path-free one-file snapshot. Later probe and hash operations still
   fail closed if that file changes.
2. `POST /projects/{project_id}/media/snapshots`
   accepts an explicit `directory` from Electron main and returns supported
   filenames, byte sizes, creation/change timestamps, modification timestamps,
   and a metadata-only SHA-256 digest. It does not calculate file-content
   hashes. The digest basis is exactly filename, size, creation time, and
   modification time, with a versioned binary encoding.
3. `POST /projects/{project_id}/media/stability`
   accepts three distinct snapshots in capture order. A directory is stable
   only when A equals B and B equals C and the final snapshot contains at least
   one supported video. The production bridge also requires at least 10 seconds
   between A/B and B/C; the response includes both observed intervals. A
   changed or too-short interval cannot be promoted to stable.
4. `POST /projects/{project_id}/media/probe`
   accepts only a filename already present in a stable final snapshot. The
   service rechecks metadata before and after probing, starts only the
   server-configured `ffprobe` binary using an argument list with
   `shell=False`, enforces a 1–30 second timeout, and validates positive
   duration, exact byte size, at least one video stream, dimensions, codec,
   container, and frame-rate metadata. Neither stderr nor filesystem paths are
   returned.
5. `POST /projects/{project_id}/media/hash-jobs` starts an explicit SHA-256
   job for one filename in a stable snapshot. `GET
   /projects/{project_id}/media/hash-jobs/{job_id}` returns the actual
   `bytes_hashed` count, and `POST
   /projects/{project_id}/media/hash-jobs/{job_id}/cancel` requests
   cancellation. One daemon worker reads fixed 4 MiB chunks, so a multi-gigabyte
   file is never loaded into memory as one value. The service checks the
   allowlisted filename and stable metadata before starting, validates the
   opened file descriptor, and validates the path metadata again after EOF.
   Only a completed job returns matching lowercase and uppercase 64-character
   SHA-256 values.

Supported extensions are `.mp4`, `.mov`, `.m4v`, `.mkv`, `.avi`, `.webm`,
`.mts`, and `.m2ts` (case-insensitive). Executables, scripts, unsupported
formats, nested paths, device namespaces, wildcard paths, alternate data
streams, symlinks, and reparse points are excluded. Enumeration is top-level
only and capped at 10,000 supported files per directory.

Hashing is never started by snapshot capture, stability comparison, ffprobe, or
directory enumeration. The Electron caller must treat it as a long-running
operation, poll the job endpoint, show only the returned byte count, and allow
cancellation; it must not invent timer-based progress. Job state is in memory
and intentionally does not survive bridge restart. Cancellation is checked
between chunks, so a blocked network read may delay the terminal `cancelled`
state until that read returns.

On Windows, `created_time_ns` is the filesystem creation timestamp. The
cross-platform test fallback uses the closest available birth time and then
filesystem change time. Snapshot and ffprobe responses keep
`content_hash_status=not_computed`; full SHA-256 provenance is produced only by
the explicit per-file hash job, never an automatic operation against all
multi-gigabyte network files.

## Opt-in local road-vision model pack

Candidate extraction remains usable without a model and is explicitly returned
as `scene_fallback` / `model_pack_not_configured`. It is not presented as a
road-defect classification.

A local model is activated only when
`TRAFFICSUPERVISION_VISION_MODEL_PACK` points to an absolute, regular
`trafficsupervision-model-pack.json`. The pack is never downloaded by the bridge.
The manifest and every model artifact are size-bounded and SHA-256 checked,
linked/reparse files are rejected, and the installer must record the runtime
and weight SPDX identifiers plus a positive commercial-use-rights
confirmation. A pack cannot supply Python code or an executable.

The first provider contract supports:

- `onnx_yolo_v8`: fixed-vocabulary, localized detections. A YOLO-World model
  may be exported after fixing the Korean road-maintenance vocabulary. The
  runtime is ONNX Runtime (MIT); exported weight rights still have to be
  declared in the pack.
- `transformers_clip_zero_shot`: local-only CLIP candidate classification.
  It requires at least two normal-road negative prompts, is marked
  `zero_shot_relative`, and does not fabricate a bounding box.
- `easyocr_ocr_only`, or optional EasyOCR alongside another engine:
  Korean/English OCR with `download_enabled=False`. OCR output is always
  `sign_or_tunnel_text_unverified`; tunnel/sign identity and location still
  require user/GPS validation.

Inference runs in a disposable child process with `shell=False`, offline
Hugging Face flags, a reduced environment, single-candidate image decoding,
bounded image pixels, predictions, OCR rows, request/output JSON, and a hard
wall-clock timeout. On Windows the child is placed in a kill-on-close Job Object
with an 8 GiB process-memory cap; if that boundary cannot be installed,
inference fails closed. A timeout, dependency failure, malformed output, or
model failure produces an empty `scene_fallback` record with a reason code. It
never becomes an AI detection.

The model-pack schema and installation checklist are documented in
`model-packs/README.md`. Model weights are intentionally not part of this
repository.
