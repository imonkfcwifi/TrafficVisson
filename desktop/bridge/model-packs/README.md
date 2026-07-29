# TrafficSuperVision local vision model packs

Model weights are not stored in this repository and are never downloaded by the
bridge. An installer must create a private, non-linked directory containing:

1. `trafficsupervision-model-pack.json`
2. every file listed in `artifacts`
3. no executable, script, plug-in, or remote-code model

Set `TRAFFICSUPERVISION_VISION_MODEL_PACK` to the absolute manifest path before the
Electron main process starts the bridge.

## Required model coverage

The common road pack should expose these exact issue types:

- `pothole`
- `road_surface_damage`
- `road_debris`
- `median_barrier_damage`
- `sign_vegetation_occlusion`
- `roadside_mowing_required`
- `lane_marking_faded`
- `color_guidance_line_faded`

`sign_identity` and `tunnel_identity` need OCR plus a later gazetteer/GPS
verification step. OCR text alone must not be relabelled as verified identity.
`other` is available for project-specific review but should not dominate the
common pack.

## Recommended first pack

For a distributable baseline, export a fixed-vocabulary road detector to
YOLOv8-compatible ONNX and declare `engine: "onnx_yolo_v8"`. Keep the detector
training/export tooling outside the installed app. ONNX Runtime is MIT licensed,
but that does not change the model-weight licence.

Ultralytics tooling and many Ultralytics weights are AGPL-3.0 unless covered by
another licence. A commercial/non-public TrafficSuperVision distribution must either
meet those terms, obtain the required enterprise licence, or use permissively
licensed training/export tooling and weights. The manifest intentionally
requires `runtime_spdx`, `weights_spdx`,
`commercial_use_confirmed: true`, and `redistribution_allowed`.

The currently implemented zero-shot option accepts a local CLIP Transformers
directory only. It is suitable for collecting proposals and hard examples, not
for declaring maintenance incidents without review. Its model configuration
must include at least two normal-road negative prompts.

The OCR adapter supports local EasyOCR Korean/English files and disables model
downloads. The planned production OCR model may be replaced later without
changing the result schema, provided the fixed worker adds an allowlisted
engine implementation.

## Manifest shape

The values below are illustrative. Replace all hashes, sizes, notices, labels,
and licence facts with verified values from the installed pack.

```json
{
  "schema_version": "trafficvisson.vision-model-pack.v1",
  "pack_id": "trafficvisson-road-common",
  "pack_version": "0.1.0",
  "engine": "onnx_yolo_v8",
  "license": {
    "runtime_spdx": "MIT",
    "weights_spdx": "Apache-2.0",
    "commercial_use_confirmed": true,
    "redistribution_allowed": false,
    "notice": "Verified model source and attribution go here."
  },
  "artifacts": [
    {
      "path": "road-common.onnx",
      "size_bytes": 123456789,
      "sha256": "replace-with-64-lowercase-hex-characters"
    }
  ],
  "model": {
    "file": "road-common.onnx",
    "input_width": 640,
    "input_height": 640,
    "labels": [
      {
        "class_id": 0,
        "issue_type": "pothole",
        "display_name": "포트홀"
      },
      {
        "class_id": 1,
        "issue_type": "road_debris",
        "display_name": "낙하물"
      }
    ],
    "confidence_threshold": 0.35,
    "nms_iou_threshold": 0.5,
    "max_detections": 16,
    "execution_provider": "auto"
  }
}
```

The loader requires exact JSON fields, contiguous ONNX class IDs from zero,
supported issue types, regular contained paths, matching sizes and hashes, and
a total pack size no larger than 12 GiB.

## Packaging/runtime dependencies

Source development on the current workstation has Torch, Transformers,
Ultralytics, ONNX Runtime, OpenCV, EasyOCR, and CUDA available. The lightweight
bridge requirements do not yet bundle all of those large optional runtimes into
the Windows installer. Before enabling a production model pack, the frozen
bridge build must explicitly collect and smoke-test the chosen fixed runtime:

- ONNX pack: `onnxruntime`, `numpy`, OpenCV/Pillow and required native DLLs
- CLIP pack: Torch, Transformers, tokenizers, safetensors and Pillow
- EasyOCR: Torch, EasyOCR, OpenCV, Pillow and the declared recognition files

The application must not tell a user that a pack is active until the packaged
worker passes the same offline inference contract tests on the target Windows
machine.

## Large-weight provenance contract

Every newly packaged large weight must use a versioned manifest that records,
and verifies before activation:

- model name and immutable model/version identity;
- HTTPS source repository plus immutable revision;
- weight and runtime SPDX identifiers, commercial-use confirmation,
  redistribution decision, licence URL and an inventoried notice file;
- every artifact's relative path, byte size and lowercase SHA-256;
- whether a GPU is required, whether CPU fallback is supported, recommended
  minimum VRAM and the minimum CUDA version when GPU is mandatory.

The bundled Grounding DINO command-proposal pack uses
`trafficvisson.command-proposal-model-pack.v2`. It is review-only and may create
box proposals, but never labels, incidents, training runs or model promotion.
SAM 2, Korean OCR and embedding-search weights must satisfy the same provenance
and GPU requirements before their runtime-readiness state can become `ready`.

`GET /runtime/readiness` and the matching read-only Electron IPC return the
scrubbed component inventory. `unavailable` (not configured), `failed`
(configured but invalid), and PipelineJob `zero_detections` (successful
inference with no predictions) are intentionally different states.

Codex CLI and Claude Code are structured-judgment/report providers only. They
do not receive bulk video frames and are never advertised as detector, OCR,
segmentation or tracking runtimes. Their official CLIs own authentication;
TrafficSuperVision neither reads nor exports their credential files.
