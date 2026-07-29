"""Reproduce and audit the pinned Vinoth RDD YOLOv8 intake.

This wrapper deliberately keeps the detector outside TrafficVisson production
paths.  It verifies the pinned Hugging Face revision and ONNX digest, delegates
read-only inference to ``desktop/scripts/evaluate_existing_candidates.py``,
then creates a human-review pack which draws every returned box.

It never enables ``--record-sidecars`` and it rejects any evaluation that
reports a label, incident, database write, or model promotion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps


INTAKE_ROOT = Path(__file__).resolve().parent
DESKTOP_ROOT = INTAKE_ROOT.parents[1]
PACK_ROOT = INTAKE_ROOT / "vinothvikas1987-pothole-b001687"
MODEL_MANIFEST = PACK_ROOT / "trafficvisson-model-pack.json"
MODEL_PATH = PACK_ROOT / "best.onnx"
REVISION_METADATA = (
    PACK_ROOT
    / ".cache"
    / "huggingface"
    / "download"
    / "best.onnx.metadata"
)
GENERIC_EVALUATOR = DESKTOP_ROOT / "scripts" / "evaluate_existing_candidates.py"
SUIT_REGULAR = (
    DESKTOP_ROOT
    / "node_modules"
    / "@sun-typeface"
    / "suit"
    / "fonts"
    / "static"
    / "ttf"
    / "SUIT-Regular.ttf"
)
SUIT_BOLD = SUIT_REGULAR.with_name("SUIT-Bold.ttf")

EXPECTED_REVISION = "b001687443175e43442f63bef4691c3546629def"
EXPECTED_MODEL_SHA256 = (
    "590a20e8c4a7bcbdb32e8a3b7b3a3c1d57d1fa8a7dde5c83fcec8928a4aa753f"
)
EXPECTED_MODEL_BYTES = 44_752_195
EXPECTED_CURRENT_MANIFEST_SHA256 = (
    "90187fde6182b2bdb791786d0e479c73da2adeb1746b0b7f5251093a02b61dfd"
)
LEGACY_PRE_HARDENING_MANIFEST_SHA256 = (
    "0cdba1051a59836170786d42c78b02f78bf480a23bf9aeb91ca5712e4e37a0b7"
)
EXPECTED_RECORD_COUNT = 51
EXPECTED_CANDIDATE_COUNT = 349
EXPECTED_PROTECTED_FILE_COUNT = 453

REVIEW_SCHEMA = "trafficvisson.vinoth-rdd-manual-review.v1"
INTAKE_SUMMARY_SCHEMA = "trafficvisson.vinoth-rdd-intake-summary.v1"


class IntakeError(RuntimeError):
    """Raised when a pinned intake invariant is not satisfied."""


# These are human visual judgments for the ten proposals produced by the
# 2026-07-29 349-frame read-only run.  A changed proposal set is never silently
# assigned one of these judgments; it remains ``not_reviewed``.
MANUAL_FRAME_REVIEWS: Mapping[str, Mapping[str, str]] = {
    "157bd4050e3c1c2b39dd": {
        "verdict": "ambiguous_possible_positive",
        "display": "판단 어려움 · 노면 손상 가능",
        "reason": (
            "박스 안에 실제 균열 또는 패치 경계가 보이지만, 단일 프레임만으로 "
            "거북등 균열인지 정상 보수 흔적인지 확정할 수 없음"
        ),
    },
    "1ad7a72c27ef1cd3d1f8": {
        "verdict": "false_positive",
        "display": "오탐 · 터널 천장 조명",
        "reason": "포트홀이 아니라 터널 천장과 조명 열을 검지함",
    },
    "8412381db68c78e71aaa": {
        "verdict": "false_positive",
        "display": "오탐 · 라바콘/노면표시",
        "reason": "종방향 균열이 아니라 라바콘과 흰색 노면표시 경계를 검지함",
    },
    "fa0fb04332ff4da29bcd": {
        "verdict": "false_positive",
        "display": "오탐 · 차선/차량 경계",
        "reason": "종방향 균열이 아니라 화물차 뒤 차선과 그림자 경계를 검지함",
    },
    "51ab0075116d3d2d320c": {
        "verdict": "false_positive",
        "display": "오탐 · 영상 하단 가장자리",
        "reason": "도로 손상 근거가 없는 영상 하단 가장자리의 작은 영역임",
    },
    "ac751af6daf4f83bbc71": {
        "verdict": "false_positive",
        "display": "오탐 · 색깔유도선",
        "reason": "종방향 균열이 아니라 적색 색깔유도선과 백색 차선 경계를 검지함",
    },
    "843ec500514ecb3d1da7": {
        "verdict": "false_positive",
        "display": "오탐 · 차선/그림자 2개",
        "reason": "두 박스 모두 노면 균열이 아니라 차선과 명암 경계를 검지함",
    },
    "07f16839fed670c62abd": {
        "verdict": "false_positive",
        "display": "오탐 · 터널 조명",
        "reason": "종방향 균열이 아니라 터널 천장 조명 열을 검지함",
    },
    "1977b8217abf1d1c516a": {
        "verdict": "false_positive",
        "display": "오탐 · 영상 우하단",
        "reason": "포트홀 근거가 없는 영상 우하단 가장자리의 작은 영역임",
    },
    "ea913e2a3d31de279bc4": {
        "verdict": "false_positive",
        "display": "오탐 · 차량/차선 경계",
        "reason": "종방향 균열이 아니라 차량 뒤쪽 차선과 노면 명암 경계를 검지함",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntakeError(f"유효한 UTF-8 JSON을 읽을 수 없음: {path}") from error
    if not isinstance(value, dict):
        raise IntakeError(f"JSON 최상위 값이 객체가 아님: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _within(child: Path, parent: Path) -> bool:
    child_resolved = child.resolve()
    parent_resolved = parent.resolve()
    try:
        child_resolved.relative_to(parent_resolved)
    except ValueError:
        return False
    return child_resolved != parent_resolved


def _default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise IntakeError("LOCALAPPDATA 환경 변수를 찾을 수 없음")
    return Path(local_app_data) / "TrafficVisson" / "bridge-data"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise IntakeError(message)


def verify_pinned_model_pack() -> dict[str, Any]:
    """Verify the immutable model artifact and hardened review-only manifest."""

    for path in (MODEL_MANIFEST, MODEL_PATH, REVISION_METADATA, GENERIC_EVALUATOR):
        _expect(path.is_file(), f"필수 파일 누락: {path}")

    metadata_lines = [
        line.strip()
        for line in REVISION_METADATA.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    _expect(
        len(metadata_lines) >= 2 and metadata_lines[0] == EXPECTED_REVISION,
        "Hugging Face 고정 리비전이 예상값과 다름",
    )
    _expect(
        metadata_lines[1].lower() == EXPECTED_MODEL_SHA256,
        "Hugging Face metadata의 ONNX 해시가 예상값과 다름",
    )
    _expect(MODEL_PATH.stat().st_size == EXPECTED_MODEL_BYTES, "ONNX 크기가 달라짐")
    _expect(_sha256(MODEL_PATH) == EXPECTED_MODEL_SHA256, "ONNX SHA-256이 달라짐")

    manifest_sha256 = _sha256(MODEL_MANIFEST)
    _expect(
        manifest_sha256 == EXPECTED_CURRENT_MANIFEST_SHA256,
        (
            "현재 review-only manifest 해시가 예상값과 다름. "
            "라이선스/추론 설정 변경 여부를 먼저 검토해야 함"
        ),
    )
    manifest = _load_json(MODEL_MANIFEST)
    license_block = manifest.get("license")
    model_block = manifest.get("model")
    artifacts = manifest.get("artifacts")
    _expect(manifest.get("engine") == "onnx_yolo_v8", "예상하지 못한 engine")
    _expect(
        manifest.get("pack_version") == "b001687.review-only",
        "review-only pack_version이 아님",
    )
    _expect(isinstance(license_block, dict), "license 객체 누락")
    _expect(
        license_block.get("commercial_use_confirmed") is False,
        "상업 사용이 미확인 상태로 차단되어 있지 않음",
    )
    _expect(
        license_block.get("redistribution_allowed") is False,
        "재배포 차단이 설정되어 있지 않음",
    )
    _expect(isinstance(model_block, dict), "model 객체 누락")
    _expect(model_block.get("confidence_threshold") == 0.4, "임계값이 달라짐")
    _expect(model_block.get("nms_iou_threshold") == 0.5, "NMS 설정이 달라짐")
    _expect(model_block.get("max_detections") == 16, "최대 검지 수가 달라짐")
    _expect(isinstance(artifacts, list) and len(artifacts) == 1, "artifact 목록 이상")
    _expect(
        artifacts[0].get("sha256") == EXPECTED_MODEL_SHA256,
        "manifest의 ONNX 해시가 달라짐",
    )
    return {
        "revision": EXPECTED_REVISION,
        "model_sha256": EXPECTED_MODEL_SHA256,
        "model_size_bytes": EXPECTED_MODEL_BYTES,
        "manifest_sha256": manifest_sha256,
        "pack_id": manifest.get("pack_id"),
        "pack_version": manifest.get("pack_version"),
        "commercial_use_confirmed": False,
        "redistribution_allowed": False,
    }


def _zero_side_effects(side_effects: Mapping[str, Any]) -> bool:
    expected_zero = (
        "analysis_records_modified",
        "automatic_labels_created",
        "experimental_sidecar_proposals_created",
        "experimental_sidecar_runs_created",
        "incidents_created",
        "label_records_modified",
        "sqlite_databases_modified",
    )
    return all(side_effects.get(key) == 0 for key in expected_zero) and (
        side_effects.get("model_promoted") is False
    )


def validate_evaluation(
    evaluation_root: Path,
    *,
    accept_legacy_manifest: bool,
) -> tuple[dict[str, Any], str]:
    predictions_path = evaluation_root / "predictions.json"
    _expect(predictions_path.is_file(), f"predictions.json 누락: {evaluation_root}")
    envelope = _load_json(predictions_path)

    results = envelope.get("results")
    source = envelope.get("source")
    side_effects = envelope.get("side_effects")
    model = envelope.get("model")
    _expect(isinstance(results, list), "results 배열 누락")
    _expect(isinstance(source, dict), "source 객체 누락")
    _expect(isinstance(side_effects, dict), "side_effects 객체 누락")
    _expect(isinstance(model, dict), "model 객체 누락")
    _expect(len(results) == EXPECTED_CANDIDATE_COUNT, "349장 결과가 아님")
    _expect(source.get("candidate_count") == EXPECTED_CANDIDATE_COUNT, "후보 수 불일치")
    _expect(source.get("completed_record_count") == EXPECTED_RECORD_COUNT, "레코드 수 불일치")
    _expect(
        source.get("protected_file_count") == EXPECTED_PROTECTED_FILE_COUNT,
        "보호 파일 수 불일치",
    )
    _expect(source.get("skipped_candidate_count") == 0, "건너뛴 후보가 있음")
    _expect(source.get("skipped_record_count") == 0, "건너뛴 레코드가 있음")
    _expect(
        source.get("protected_sources_verified_unchanged") is True,
        "평가 전후 원본 무변경 검증이 완료되지 않음",
    )
    _expect(_zero_side_effects(side_effects), "평가가 사용자 데이터 또는 모델 상태를 변경함")
    _expect(model.get("engine") == "onnx_yolo_v8", "평가 engine 불일치")
    _expect(model.get("redistribution_allowed") is False, "평가 결과가 재배포를 허용함")

    result_manifest_sha = str(model.get("manifest_sha256", "")).lower()
    if result_manifest_sha == EXPECTED_CURRENT_MANIFEST_SHA256:
        provenance_state = "current_hardened_manifest"
    elif (
        accept_legacy_manifest
        and result_manifest_sha == LEGACY_PRE_HARDENING_MANIFEST_SHA256
    ):
        provenance_state = "legacy_pre_hardening_manifest"
    else:
        raise IntakeError("평가 결과의 manifest SHA-256이 승인된 값과 다름")

    for row in results:
        _expect(isinstance(row, dict), "result 행이 객체가 아님")
        _expect(row.get("automatic_label_created") is False, "자동 라벨이 생성됨")
        _expect(row.get("incident_created") is False, "사건이 생성됨")
        _expect(row.get("model_promoted") is False, "모델이 승격됨")
        _expect(row.get("review_state") == "unreviewed", "검토 전용 상태가 아님")
    return envelope, provenance_state


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    path = SUIT_BOLD if bold else SUIT_REGULAR
    if path.is_file():
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _resolve_source_image(
    data_root: Path,
    result: Mapping[str, Any],
) -> Path:
    reference = result.get("source_image_ref")
    _expect(isinstance(reference, str) and reference, "source_image_ref 누락")
    source_path = (data_root / Path(reference)).resolve()
    _expect(_within(source_path, data_root), "source_image_ref가 data root 밖을 가리킴")
    _expect(source_path.is_file(), f"원본 후보 JPEG 누락: {source_path}")
    _expect(
        _sha256(source_path) == result.get("source_image_sha256"),
        f"원본 후보 JPEG 해시 불일치: {source_path}",
    )
    return source_path


def _classified_results(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in envelope["results"]
        if isinstance(row, dict) and isinstance(row.get("predictions"), list)
        and row["predictions"]
    ]
    return sorted(
        rows,
        key=lambda row: max(float(pred.get("score", 0.0)) for pred in row["predictions"]),
        reverse=True,
    )


def _draw_all_boxes(
    source: Image.Image,
    predictions: Iterable[Mapping[str, Any]],
) -> Image.Image:
    canvas = source.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    label_font = _font(max(17, round(width / 48)), bold=True)
    line_width = max(4, round(width / 160))
    for box_index, prediction in enumerate(predictions, start=1):
        box = prediction.get("box")
        if not isinstance(box, dict):
            continue
        left = round(float(box.get("x", 0.0)) * width)
        top = round(float(box.get("y", 0.0)) * height)
        right = round((float(box.get("x", 0.0)) + float(box.get("width", 0.0))) * width)
        bottom = round(
            (float(box.get("y", 0.0)) + float(box.get("height", 0.0))) * height
        )
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        right = max(left + 1, min(width - 1, right))
        bottom = max(top + 1, min(height - 1, bottom))
        color = (244, 63, 94)
        draw.rectangle((left, top, right, bottom), outline=color, width=line_width)
        label = (
            f"#{box_index} {prediction.get('source_label', prediction.get('issue_type'))} "
            f"{float(prediction.get('score', 0.0)):.3f}"
        )
        label_box = draw.textbbox((0, 0), label, font=label_font)
        label_width = label_box[2] - label_box[0] + 16
        label_height = label_box[3] - label_box[1] + 10
        label_top = top if top + label_height < height else max(0, top - label_height)
        label_left = max(0, min(left, width - label_width))
        draw.rectangle(
            (
                label_left,
                label_top,
                min(width, label_left + label_width),
                label_top + label_height,
            ),
            fill=color,
        )
        draw.text((label_left + 8, label_top + 3), label, font=label_font, fill="white")
    return canvas


def render_review_pack(
    evaluation_root: Path,
    data_root: Path,
    envelope: Mapping[str, Any],
    *,
    provenance_state: str,
) -> dict[str, Any]:
    rows = _classified_results(envelope)
    _expect(rows, "표시할 검지 제안이 없음")

    tile_width = 780
    image_width = 740
    image_height = 416
    tile_height = 590
    columns = 2
    title_height = 110
    row_count = (len(rows) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (tile_width * columns, title_height + tile_height * row_count),
        (244, 247, 250),
    )
    sheet_draw = ImageDraw.Draw(sheet)
    title_font = _font(34, bold=True)
    body_font = _font(22)
    small_font = _font(18)
    sheet_draw.text(
        (24, 18),
        "Vinoth RDD b001687 · 전체 박스 수동 검토",
        font=title_font,
        fill=(17, 24, 39),
    )
    sheet_draw.text(
        (24, 65),
        "349장 중 제안 10프레임 / 11박스 · 자동 라벨 0 · 활성화 금지",
        font=body_font,
        fill=(75, 85, 99),
    )

    review_rows: list[dict[str, Any]] = []
    for frame_index, result in enumerate(rows, start=1):
        candidate_id = str(result.get("evaluation_candidate_id"))
        manual = MANUAL_FRAME_REVIEWS.get(
            candidate_id,
            {
                "verdict": "not_reviewed",
                "display": "미검토 · 사람 확인 필요",
                "reason": "고정 수동 검토 목록에 없는 새 제안",
            },
        )
        source_path = _resolve_source_image(data_root, result)
        with Image.open(source_path) as raw:
            annotated = _draw_all_boxes(raw, result["predictions"])
        fitted = ImageOps.fit(
            annotated,
            (image_width, image_height),
            method=Image.Resampling.LANCZOS,
        )

        column = (frame_index - 1) % columns
        row_number = (frame_index - 1) // columns
        origin_x = column * tile_width
        origin_y = title_height + row_number * tile_height
        card_left = origin_x + 16
        card_top = origin_y + 12
        sheet_draw.rounded_rectangle(
            (
                card_left,
                card_top,
                origin_x + tile_width - 16,
                origin_y + tile_height - 12,
            ),
            radius=18,
            fill=(255, 255, 255),
            outline=(222, 228, 236),
            width=2,
        )
        sheet.paste(fitted, (origin_x + 20, origin_y + 20))
        verdict_color = (
            (217, 119, 6)
            if manual["verdict"] == "ambiguous_possible_positive"
            else (190, 24, 93)
            if manual["verdict"] == "false_positive"
            else (107, 114, 128)
        )
        sheet_draw.text(
            (origin_x + 24, origin_y + 450),
            f"{frame_index:02d}. {manual['display']}",
            font=_font(22, bold=True),
            fill=verdict_color,
        )
        sheet_draw.text(
            (origin_x + 24, origin_y + 484),
            (
                f"{result.get('source_file_name')} · "
                f"t={float(result.get('representative_timestamp_seconds', 0.0)):.3f}s"
            ),
            font=small_font,
            fill=(55, 65, 81),
        )
        sheet_draw.text(
            (origin_x + 24, origin_y + 514),
            f"id={candidate_id} · boxes={len(result['predictions'])}",
            font=small_font,
            fill=(75, 85, 99),
        )

        for box_index, prediction in enumerate(result["predictions"], start=1):
            review_rows.append(
                {
                    "review_index": frame_index,
                    "evaluation_candidate_id": candidate_id,
                    "source_file_name": result.get("source_file_name"),
                    "timestamp_seconds": result.get("representative_timestamp_seconds"),
                    "box_index": box_index,
                    "model_issue_type": prediction.get("issue_type"),
                    "source_label": prediction.get("source_label"),
                    "model_score": prediction.get("score"),
                    "verdict": manual["verdict"],
                    "reason": manual["reason"],
                    "automatic_label_created": False,
                }
            )

    contact_sheet_path = evaluation_root / "all-boxes-contact-sheet-001.jpg"
    sheet.save(contact_sheet_path, format="JPEG", quality=92, optimize=True)

    csv_path = evaluation_root / "manual-visual-review.csv"
    fieldnames = list(review_rows[0].keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(review_rows)

    false_box_count = sum(row["verdict"] == "false_positive" for row in review_rows)
    ambiguous_box_count = sum(
        row["verdict"] == "ambiguous_possible_positive" for row in review_rows
    )
    unreviewed_box_count = sum(row["verdict"] == "not_reviewed" for row in review_rows)
    false_frame_count = sum(
        MANUAL_FRAME_REVIEWS.get(
            str(row.get("evaluation_candidate_id")), {}
        ).get("verdict")
        == "false_positive"
        for row in rows
    )
    ambiguous_frame_count = sum(
        MANUAL_FRAME_REVIEWS.get(
            str(row.get("evaluation_candidate_id")), {}
        ).get("verdict")
        == "ambiguous_possible_positive"
        for row in rows
    )

    summary = {
        "schema_version": INTAKE_SUMMARY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evaluation_directory": str(evaluation_root.resolve()),
        "provenance_state": provenance_state,
        "candidate_frame_count": EXPECTED_CANDIDATE_COUNT,
        "proposal_frame_count": len(rows),
        "proposal_box_count": len(review_rows),
        "confirmed_positive_frame_count": 0,
        "confirmed_positive_box_count": 0,
        "ambiguous_possible_positive_frame_count": ambiguous_frame_count,
        "ambiguous_possible_positive_box_count": ambiguous_box_count,
        "false_positive_frame_count": false_frame_count,
        "false_positive_box_count": false_box_count,
        "unreviewed_box_count": unreviewed_box_count,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "activation_verdict": "rejected_for_activation",
        "distribution_verdict": "blocked_pending_provenance_and_legal_review",
        "policy": "evaluation_only_review_only",
        "precision_recall_computed": False,
        "precision_recall_reason": (
            "349장은 고정 정답 세트가 아니며 음성 장면도 전수 라벨링되지 않았으므로 "
            "정밀도·재현율 또는 오탐률을 계산할 수 없음"
        ),
    }
    summary_path = evaluation_root / "intake-run-summary.json"
    _write_json(summary_path, summary)

    review_markdown = _manual_review_markdown(
        envelope,
        rows,
        review_rows,
        summary,
        provenance_state=provenance_state,
    )
    review_path = evaluation_root / "VISUAL_REVIEW.md"
    review_path.write_text(review_markdown, encoding="utf-8")

    supplemental_artifacts = [
        contact_sheet_path,
        csv_path,
        summary_path,
        review_path,
    ]
    supplemental_manifest = {
        "schema_version": REVIEW_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "manual_visual_review_supplement",
        "source_predictions_sha256": _sha256(evaluation_root / "predictions.json"),
        "artifacts": [
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in supplemental_artifacts
        ],
        "side_effects": {
            "automatic_labels_created": 0,
            "incidents_created": 0,
            "model_promoted": False,
            "user_data_modified": False,
        },
    }
    _write_json(evaluation_root / "intake-review-manifest.json", supplemental_manifest)
    return summary


def _manual_review_markdown(
    envelope: Mapping[str, Any],
    frame_rows: Sequence[Mapping[str, Any]],
    box_rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    *,
    provenance_state: str,
) -> str:
    lines = [
        "# Vinoth RDD b001687 시각 판정",
        "",
        "## 결론",
        "",
        "- **활성화 판정: 거부**",
        "- 자동 라벨·사건·모델 승격: `0건`",
        (
            f"- 349장 중 모델 제안: `{len(frame_rows)}프레임 / "
            f"{len(box_rows)}박스`"
        ),
        (
            "- 사람 확인 결과: 확정 양성 `0프레임`, 판단 어려움 "
            f"`{summary['ambiguous_possible_positive_frame_count']}프레임`, "
            f"오탐 `{summary['false_positive_frame_count']}프레임`"
        ),
        (
            "- 박스 기준: 판단 어려움 "
            f"`{summary['ambiguous_possible_positive_box_count']}개`, 오탐 "
            f"`{summary['false_positive_box_count']}개`"
        ),
        "",
        (
            "이 수치는 정밀도나 재현율이 아니다. 349장은 한국 도로 고정 검증 "
            "세트가 아니고, 검지가 없던 339장에 대한 전수 정답 라벨도 없다."
        ),
        "",
        "## 프레임별 판정",
        "",
        "| 영상 / 시각 | 모델 제안 | 점수 | 사람 판정 | 근거 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result in frame_rows:
        candidate_id = str(result.get("evaluation_candidate_id"))
        manual = MANUAL_FRAME_REVIEWS.get(
            candidate_id,
            {
                "display": "미검토",
                "reason": "사람 확인 필요",
            },
        )
        predictions = result["predictions"]
        proposal_counts = Counter(
            str(prediction.get("source_label", prediction.get("issue_type")))
            for prediction in predictions
        )
        proposal = ", ".join(
            f"{label} ×{count}" for label, count in proposal_counts.items()
        )
        score = max(float(prediction.get("score", 0.0)) for prediction in predictions)
        lines.append(
            "| "
            f"`{result.get('source_file_name')}` / "
            f"`{float(result.get('representative_timestamp_seconds', 0.0)):.3f}s`"
            " | "
            f"{proposal}"
            " | "
            f"`{score:.6f}`"
            " | "
            f"{manual['display']}"
            " | "
            f"{manual['reason']}"
            " |"
        )
    lines.extend(
        [
            "",
            "## 관찰된 실패 패턴",
            "",
            "- 터널 천장 조명 열을 포트홀 또는 종방향 균열로 검지",
            "- 라바콘·차선·색깔유도선·차량 그림자를 종방향 균열로 검지",
            "- 영상 하단 또는 우하단의 매우 작은 가장자리 영역을 포트홀로 검지",
            "- 최고 점수 0.715679 제안도 단일 프레임에서는 손상 유형을 확정할 수 없음",
            "",
            (
                "`0.4` 임계값은 이 표본에서 명백한 오탐을 다수 통과시켰다. "
                "그러나 이 미라벨 표본에 맞춰 임계값을 올리는 것은 검증이 아니므로 "
                "설정을 조정하거나 제품에 반영하지 않았다."
            ),
            "",
            "## 출처와 안전 상태",
            "",
            f"- 결과 provenance: `{provenance_state}`",
            (
                f"- 원본 보호 파일 `{envelope['source']['protected_file_count']}개` "
                "평가 전후 해시 일치"
            ),
            "- sidecar 기록 없음, 사용자 라벨·사건·DB 수정 없음",
            "- `all-boxes-contact-sheet-001.jpg`는 11개 박스를 모두 표시",
            "- `manual-visual-review.csv`는 박스별 판정 근거를 보존",
            "",
            "## 다음 게이트",
            "",
            "1. 한국 도로 고정 검증 세트의 유형별 정답 라벨 확보",
            "2. 유형별 precision/recall 및 사건 단위 중복·오탐 평가",
            "3. RDD 정확 버전·라이선스와 YOLOv8 base-weight 권리 확인",
            "4. 법률 검토와 attribution/NOTICE 확정",
            "",
            (
                "위 게이트가 모두 닫히기 전에는 ONNX를 설치 프로그램에 번들하거나 "
                "활성 모델로 승격하거나 자동 라벨·보고서 근거로 사용하면 안 된다."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def run_full_evaluation(
    output_root: Path,
    data_root: Path,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    _expect(_within(output_root, INTAKE_ROOT), "출력은 model-intake 하위여야 함")
    _expect(not output_root.exists(), f"출력 디렉터리가 이미 존재함: {output_root}")
    command = [
        sys.executable,
        str(GENERIC_EVALUATOR),
        "--data-root",
        str(data_root),
        "--model-manifest",
        str(MODEL_MANIFEST),
        "--output-directory",
        str(output_root),
        "--timeout-seconds",
        str(timeout_seconds),
        "--maximum-contact-sheet-items",
        str(EXPECTED_CANDIDATE_COUNT),
        "--operator-note",
        (
            "Pinned b001687 review-only intake. No automatic labels, incidents, "
            "sidecars, report inputs, or model promotion."
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(DESKTOP_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds + 120.0,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise IntakeError("전체 평가가 제한 시간을 초과함") from error

    if output_root.is_dir():
        (output_root / "wrapper-evaluator.stdout.log").write_text(
            completed.stdout,
            encoding="utf-8",
        )
        (output_root / "wrapper-evaluator.stderr.log").write_text(
            completed.stderr,
            encoding="utf-8",
        )
    _expect(
        completed.returncode == 0,
        f"기존 후보 평가기 실패(return code {completed.returncode})",
    )
    return validate_evaluation(output_root, accept_legacy_manifest=False)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pinned Vinoth RDD b001687 detector intake. Runs read-only evaluation "
            "or renders a review pack for an existing result."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--output-directory",
        help="새 349장 평가 출력(model-intake 하위의 존재하지 않는 디렉터리)",
    )
    mode.add_argument(
        "--review-existing",
        help="기존 평가 출력의 모든 박스를 표시하고 수동 판정을 내보냄",
    )
    parser.add_argument("--data-root", default=str(_default_data_root()))
    parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    arguments = parser.parse_args(argv)

    try:
        model_provenance = verify_pinned_model_pack()
        data_root = Path(arguments.data_root).resolve(strict=True)
        _expect(data_root.is_dir(), "bridge data root가 디렉터리가 아님")
        if arguments.output_directory:
            _expect(
                model_provenance["commercial_use_confirmed"] is True,
                (
                    "현재 hardened manifest가 새 추론을 차단함. provenance와 "
                    "base-weight 권리가 확인되기 전에는 이 게이트를 우회하지 말 것"
                ),
            )
            evaluation_root = Path(arguments.output_directory).resolve()
            envelope, provenance_state = run_full_evaluation(
                evaluation_root,
                data_root,
                timeout_seconds=arguments.timeout_seconds,
            )
        else:
            evaluation_root = Path(arguments.review_existing).resolve(strict=True)
            _expect(
                _within(evaluation_root, INTAKE_ROOT),
                "기존 출력은 model-intake 하위여야 함",
            )
            envelope, provenance_state = validate_evaluation(
                evaluation_root,
                accept_legacy_manifest=True,
            )
        summary = render_review_pack(
            evaluation_root,
            data_root,
            envelope,
            provenance_state=provenance_state,
        )
    except (IntakeError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "status": "completed",
                "model": model_provenance,
                "evaluation_directory": str(evaluation_root),
                "proposal_frame_count": summary["proposal_frame_count"],
                "proposal_box_count": summary["proposal_box_count"],
                "confirmed_positive_frame_count": 0,
                "ambiguous_possible_positive_frame_count": summary[
                    "ambiguous_possible_positive_frame_count"
                ],
                "false_positive_frame_count": summary["false_positive_frame_count"],
                "activation_verdict": summary["activation_verdict"],
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
