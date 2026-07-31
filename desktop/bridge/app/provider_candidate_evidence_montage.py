"""Build one path-free candidate montage for bounded Provider comparison.

The Provider attachment budget is one current-video image plus three to five
3Q/4Q reference images.  A single representative crop therefore cannot also
show the temporal and segmentation evidence the user was promised.  This
module deterministically combines four immutable candidate artifacts into one
2x2 image:

``before | representative-with-box``
``mask   | after``

The result is *not* deidentified and is never transport-authorized.  It must
still pass through :mod:`provider_visual_evidence`, including local face/plate
redaction and explicit human privacy review, before an official CLI may see
it.  No paths are accepted or returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import re
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


SCHEMA_VERSION = (
    "trafficvisson.provider-candidate-evidence-montage.v1"
)
ROLE_ORDER = (
    "before_frame",
    "representative_frame",
    "mask_overlay",
    "after_frame",
)
PANEL_WIDTH = 512
PANEL_HEIGHT = 288
HEADER_HEIGHT = 24
CANVAS_WIDTH = PANEL_WIDTH * 2
CANVAS_HEIGHT = (PANEL_HEIGHT + HEADER_HEIGHT) * 2
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_REF = re.compile(
    r"^(?:analysis|evidence):"
    r"(?P<candidate>[a-f0-9]{20}):"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$"
)
_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_ROLE_LABELS = {
    "before_frame": "BEFORE",
    "representative_frame": "REPRESENTATIVE + BOX",
    "mask_overlay": "SAM2 MASK",
    "after_frame": "AFTER",
}


class CandidateEvidenceMontageError(ValueError):
    """A candidate montage input failed its immutable evidence contract."""

    def __init__(self, code: str, message: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code) is None:
            raise ValueError("candidate montage error code is invalid")
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CandidateEvidenceMontage:
    """Path-free output to persist under the owning PipelineJob."""

    body: bytes
    media_type: str
    size_bytes: int
    sha256: str
    manifest: Mapping[str, Any]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalized_box(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise CandidateEvidenceMontageError(
            "candidate_montage_box_invalid",
            "target_box must contain x, y, width, and height",
        )
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise CandidateEvidenceMontageError(
                "candidate_montage_box_invalid",
                "target_box values must be finite numbers",
            )
        result[key] = float(number)
    if (
        not 0 <= result["x"] < 1
        or not 0 <= result["y"] < 1
        or not 0 < result["width"] <= 1
        or not 0 < result["height"] <= 1
        or result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        raise CandidateEvidenceMontageError(
            "candidate_montage_box_invalid",
            "target_box is outside normalized image bounds",
        )
    return result


def _decode_component(
    value: object,
    *,
    candidate_id: str,
    expected_role: str,
) -> tuple[Image.Image, dict[str, Any]]:
    expected_keys = {
        "role",
        "source_ref",
        "source_sha256",
        "media_type",
        "size_bytes",
        "body",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise CandidateEvidenceMontageError(
            "candidate_montage_component_invalid",
            "candidate montage component fields are invalid",
        )
    role = value["role"]
    source_ref = value["source_ref"]
    source_sha256 = value["source_sha256"]
    media_type = value["media_type"]
    size_bytes = value["size_bytes"]
    body = value["body"]
    match = (
        _CANDIDATE_REF.fullmatch(source_ref)
        if isinstance(source_ref, str)
        else None
    )
    if (
        role != expected_role
        or match is None
        or match.group("candidate") != candidate_id
        or not isinstance(source_sha256, str)
        or _SHA256.fullmatch(source_sha256) is None
        or media_type not in _MEDIA_TYPES
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= MAX_SOURCE_BYTES
        or not isinstance(body, bytes)
        or len(body) != size_bytes
        or _sha256(body) != source_sha256
    ):
        raise CandidateEvidenceMontageError(
            "candidate_montage_component_invalid",
            "candidate montage component provenance is invalid",
        )
    try:
        with Image.open(io.BytesIO(body)) as decoded:
            decoded.load()
            image = ImageOps.exif_transpose(decoded).convert("RGB")
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise CandidateEvidenceMontageError(
            "candidate_montage_component_invalid",
            "candidate montage component cannot be decoded",
        ) from error
    if (
        image.width < 16
        or image.height < 16
        or image.width * image.height > MAX_SOURCE_PIXELS
    ):
        raise CandidateEvidenceMontageError(
            "candidate_montage_component_invalid",
            "candidate montage component dimensions are invalid",
        )
    return image, {
        "role": role,
        "source_ref": source_ref,
        "source_sha256": source_sha256,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "width": image.width,
        "height": image.height,
    }


def _letterboxed(image: Image.Image) -> tuple[Image.Image, dict[str, int]]:
    scale = min(PANEL_WIDTH / image.width, PANEL_HEIGHT / image.height)
    width = max(1, int(round(image.width * scale)))
    height = max(1, int(round(image.height * scale)))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    panel = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (12, 18, 28))
    left = (PANEL_WIDTH - width) // 2
    top = (PANEL_HEIGHT - height) // 2
    panel.paste(resized, (left, top))
    return panel, {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def _draw_target_box(
    panel: Image.Image,
    placement: Mapping[str, int],
    target_box: Mapping[str, float],
) -> None:
    left = placement["left"] + int(
        round(target_box["x"] * placement["width"])
    )
    top = placement["top"] + int(
        round(target_box["y"] * placement["height"])
    )
    right = placement["left"] + int(
        round(
            (target_box["x"] + target_box["width"])
            * placement["width"]
        )
    )
    bottom = placement["top"] + int(
        round(
            (target_box["y"] + target_box["height"])
            * placement["height"]
        )
    )
    draw = ImageDraw.Draw(panel)
    draw.rectangle(
        (
            max(0, min(PANEL_WIDTH - 1, left)),
            max(0, min(PANEL_HEIGHT - 1, top)),
            max(0, min(PANEL_WIDTH - 1, right)),
            max(0, min(PANEL_HEIGHT - 1, bottom)),
        ),
        outline=(255, 211, 64),
        width=4,
    )


def build_candidate_evidence_montage(
    *,
    candidate_id: str,
    components: Sequence[Mapping[str, Any]],
    target_box: Mapping[str, Any],
) -> CandidateEvidenceMontage:
    """Return one deterministic unreviewed four-panel candidate image."""

    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or isinstance(components, (str, bytes, bytearray))
        or not isinstance(components, Sequence)
        or len(components) != len(ROLE_ORDER)
    ):
        raise CandidateEvidenceMontageError(
            "candidate_montage_input_invalid",
            "candidate montage identity or component count is invalid",
        )
    normalized_box = _normalized_box(target_box)
    decoded: list[tuple[Image.Image, dict[str, Any]]] = []
    source_refs: set[str] = set()
    for index, role in enumerate(ROLE_ORDER):
        image, row = _decode_component(
            components[index],
            candidate_id=candidate_id,
            expected_role=role,
        )
        if row["source_ref"] in source_refs:
            raise CandidateEvidenceMontageError(
                "candidate_montage_component_invalid",
                "candidate montage source references must be unique",
            )
        source_refs.add(row["source_ref"])
        decoded.append((image, row))

    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), (6, 10, 18))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    manifest_rows: list[dict[str, Any]] = []
    for index, (image, row) in enumerate(decoded):
        column = index % 2
        grid_row = index // 2
        x = column * PANEL_WIDTH
        y = grid_row * (PANEL_HEIGHT + HEADER_HEIGHT)
        draw.rectangle(
            (x, y, x + PANEL_WIDTH - 1, y + HEADER_HEIGHT - 1),
            fill=(18, 27, 43),
        )
        draw.text(
            (x + 9, y + 6),
            _ROLE_LABELS[row["role"]],
            fill=(232, 239, 248),
            font=font,
        )
        panel, placement = _letterboxed(image)
        if row["role"] == "representative_frame":
            _draw_target_box(panel, placement, normalized_box)
        canvas.paste(panel, (x, y + HEADER_HEIGHT))
        manifest_rows.append(
            {
                **row,
                "order": index,
                "panel": {
                    "column": column,
                    "row": grid_row,
                    "x": x,
                    "y": y + HEADER_HEIGHT,
                    "width": PANEL_WIDTH,
                    "height": PANEL_HEIGHT,
                },
                "content_placement": placement,
            }
        )

    output = io.BytesIO()
    canvas.save(
        output,
        format="PNG",
        optimize=False,
        compress_level=6,
    )
    body = output.getvalue()
    if not 1 <= len(body) <= MAX_OUTPUT_BYTES:
        raise CandidateEvidenceMontageError(
            "candidate_montage_output_invalid",
            "candidate montage output is outside the safe byte range",
        )
    output_sha256 = _sha256(body)
    manifest_body = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "layout": {
            "kind": "two_by_two_temporal_mask_evidence",
            "role_order": list(ROLE_ORDER),
            "canvas_width": CANVAS_WIDTH,
            "canvas_height": CANVAS_HEIGHT,
            "header_height": HEADER_HEIGHT,
        },
        "target_box": normalized_box,
        "components": manifest_rows,
        "output": {
            "media_type": "image/png",
            "size_bytes": len(body),
            "sha256": output_sha256,
            "width": CANVAS_WIDTH,
            "height": CANVAS_HEIGHT,
        },
        "privacy_state": "unreviewed",
        "provider_transmission_allowed": False,
        "requires_provider_visual_evidence_review": True,
    }
    manifest = {
        **manifest_body,
        "manifest_sha256": _sha256(_canonical_json(manifest_body)),
    }
    return CandidateEvidenceMontage(
        body=body,
        media_type="image/png",
        size_bytes=len(body),
        sha256=output_sha256,
        manifest=manifest,
    )


__all__ = [
    "CandidateEvidenceMontage",
    "CandidateEvidenceMontageError",
    "ROLE_ORDER",
    "SCHEMA_VERSION",
    "build_candidate_evidence_montage",
]
