import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.candidate_extraction import _effective_bootstrap_cadence
from app.candidate_window_rescan import (
    CandidateWindow,
    CandidateWindowFrame,
    CandidateWindowRescanResult,
    load_candidate_window_rescan,
)
from app.visual_evidence_quality import (
    VISUAL_EVIDENCE_QUALITY_SCHEMA,
    evaluate_candidate_window_visual_quality,
)
from app.weak_case_visual_index import VerifiedLocalClipImageEmbedder
from app.contracts import PipelineJobResponse
from app.pipeline_jobs import (
    PIPELINE_STAGES,
    PipelineJobService,
    PipelineStageResult,
)
from test_pipeline_jobs import (
    FakeRuntime,
    _configure,
    _start,
    _wait,
)


_CANDIDATE_ID = "a" * 20
_WINDOW_ID = "b" * 20
_SEMANTIC_MODEL_SHA = "1" * 64
_SEMANTIC_PROMPT_SHA = "2" * 64


class _HashBoundRoadSceneObserver:
    def __init__(
        self,
        probabilities_by_sha: dict[str, float],
    ) -> None:
        self._probabilities_by_sha = probabilities_by_sha

    def observe_road_scene(
        self,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ):
        assert media_type == "image/jpeg"
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        road_probability = self._probabilities_by_sha[
            expected_sha256
        ]
        return {
            "status": "completed",
            "image_sha256": expected_sha256,
            "model_identity_sha256": _SEMANTIC_MODEL_SHA,
            "prompt_set_sha256": _SEMANTIC_PROMPT_SHA,
            "road_scene_probability": road_probability,
            "non_road_view_probability": 1.0 - road_probability,
            "local_only": True,
            "review_only": True,
        }


def _covered_frame(index: int) -> Image.Image:
    image = Image.new("RGB", (320, 180), (118, 55, 154))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            offset = round(
                28
                * (
                    (x / max(1, image.width - 1))
                    - (y / max(1, image.height - 1))
                )
            )
            pixels[x, y] = (
                max(0, min(255, 118 + offset + index)),
                max(0, min(255, 55 + offset // 2)),
                max(0, min(255, 154 + offset)),
            )
    return image.filter(ImageFilter.GaussianBlur(radius=10))


def _road_frame(index: int) -> Image.Image:
    image = Image.new("RGB", (320, 180), (110, 145, 178))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(0, 180), (82, 75), (238, 75), (320, 180)],
        fill=(62, 64, 68),
    )
    draw.line((154, 180, 159, 90), fill=(245, 245, 220), width=5)
    draw.line((190, 180, 170, 90), fill=(245, 245, 220), width=5)
    draw.rectangle((22 + index, 68, 91 + index, 94), fill=(20, 78, 46))
    draw.rectangle((25 + index, 71, 88 + index, 91), outline="white", width=2)
    for x in range(0, 320, 12):
        draw.point((x, 130 + ((x + index) % 17)), fill=(150, 150, 150))
    return image


def _night_frame(index: int) -> Image.Image:
    image = Image.new("RGB", (320, 180), (5, 8, 14))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(0, 180), (95, 85), (225, 85), (320, 180)],
        fill=(18, 20, 24),
    )
    draw.line((150, 180, 158, 95), fill=(210, 210, 150), width=3)
    draw.line((195, 180, 172, 95), fill=(210, 210, 150), width=3)
    draw.ellipse((135 + index, 87, 146 + index, 98), fill=(255, 238, 188))
    draw.ellipse((176 + index, 87, 187 + index, 98), fill=(255, 238, 188))
    return image


def _tunnel_frame(index: int) -> Image.Image:
    image = Image.new("RGB", (320, 180), (30, 31, 34))
    draw = ImageDraw.Draw(image)
    draw.arc((18, 5, 302, 230), 180, 360, fill=(135, 135, 130), width=7)
    draw.polygon(
        [(0, 180), (100, 85), (220, 85), (320, 180)],
        fill=(43, 44, 46),
    )
    draw.line((155, 180, 159, 100), fill=(235, 232, 180), width=4)
    for x in range(70, 270, 50):
        draw.rectangle((x + index, 35, x + 12 + index, 40), fill="white")
    return image


def _car_interior_frame(index: int) -> Image.Image:
    image = Image.new("RGB", (240, 300), (125, 119, 121))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(0, 0), (240, 0), (240, 70), (0, 170)],
        fill=(170, 213, 235),
    )
    draw.polygon(
        [(0, 170), (240, 70), (240, 260), (0, 300)],
        fill=(92, 88, 90),
    )
    draw.rectangle(
        (72, 175, 218, 230),
        fill=(145, 139, 142),
        outline=(54, 51, 52),
        width=4,
    )
    draw.polygon(
        [(0, 35), (32 + index, 60), (54, 300), (0, 300)],
        fill=(125, 20, 63),
    )
    return image.filter(ImageFilter.GaussianBlur(radius=2.2))


def _make_result(
    tmp_path: Path,
    images: list[Image.Image],
) -> CandidateWindowRescanResult:
    root = tmp_path / f"candidate-window-rescan-{'c' * 24}"
    frame_root = root / "windows" / _WINDOW_ID
    frame_root.mkdir(parents=True)
    frames = []
    for index, image in enumerate(images):
        path = frame_root / f"frame-{index:06d}.jpg"
        image.save(path, format="JPEG", quality=92, optimize=False)
        body = path.read_bytes()
        frames.append(
            CandidateWindowFrame(
                evidence_id=hashlib.sha256(body + bytes([index])).hexdigest()[:20],
                window_id=_WINDOW_ID,
                frame_index=index,
                timestamp_seconds=round(index / 8, 6),
                timestamp_numerator=index,
                timestamp_denominator=8,
                source_candidate_ids=(_CANDIDATE_ID,),
                primary_candidate_id=_CANDIDATE_ID,
                relative_path=(
                    f"windows/{_WINDOW_ID}/{path.name}"
                ),
                width=image.width,
                height=image.height,
                size_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
        )
    window = CandidateWindow(
        window_id=_WINDOW_ID,
        start_seconds=0.0,
        end_seconds=round(len(images) / 8, 6),
        duration_seconds=round(len(images) / 8, 6),
        source_candidate_ids=(_CANDIDATE_ID,),
        planned_frame_limit=len(images),
    )
    return CandidateWindowRescanResult(
        output_directory=str(root),
        manifest_relative_path="candidate-window-rescan.json",
        manifest_sha256="d" * 64,
        input_revision="e" * 64,
        windows=(window,),
        frames=tuple(frames),
        downstream_evidence_ids=tuple(
            frame.evidence_id for frame in frames
        ),
        cached=False,
    )


def test_persistently_covered_window_is_explicitly_unusable_and_never_truth(
    tmp_path,
):
    result = _make_result(
        tmp_path,
        [_covered_frame(index) for index in range(7)],
    )

    first = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
    )
    second = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
    )
    checkpoint = first.to_checkpoint()

    assert first.schema_version == VISUAL_EVIDENCE_QUALITY_SCHEMA
    assert first.state == "unusable_visual_evidence"
    assert first.quality_revision == second.quality_revision
    assert len(first.frame_observations) == 7
    assert all(item.strong_unusable for item in first.frame_observations)
    assert first.selected_evidence_ids == ()
    assert first.eligible_parent_candidate_ids == ()
    assert first.blocked_parent_candidate_ids == (_CANDIDATE_ID,)
    assert checkpoint["automatic_annotation_creation"] is False
    assert checkpoint["automatic_incident_creation"] is False
    assert checkpoint["automatic_training_membership"] is False
    assert checkpoint["automatic_report_truth"] is False


def test_bad_initial_representative_is_reselected_from_usable_window(
    tmp_path,
):
    images = [
        _covered_frame(0),
        _covered_frame(1),
        _road_frame(0),
        _road_frame(1),
        _road_frame(2),
    ]
    result = _make_result(tmp_path, images)

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
    )
    decision = quality.window_decisions[0]
    observations = {
        item.evidence_id: item
        for item in quality.frame_observations
    }

    assert quality.state == "usable"
    assert decision.state == "usable_representative_reselected"
    assert decision.representative_evidence_id is not None
    assert (
        observations[
            decision.representative_evidence_id
        ].strong_unusable
        is False
    )
    assert all(
        observations[evidence_id].strong_unusable is False
        for evidence_id in quality.selected_evidence_ids
    )


@pytest.mark.parametrize(
    "factory",
    [
        _road_frame,
        _night_frame,
        _tunnel_frame,
        lambda index: _road_frame(index).filter(
            ImageFilter.GaussianBlur(radius=2.5)
        ),
    ],
    ids=["road", "night", "tunnel", "motion_blur"],
)
def test_valid_road_night_tunnel_and_blur_are_not_overfiltered(
    tmp_path,
    factory,
):
    result = _make_result(
        tmp_path,
        [factory(index) for index in range(5)],
    )

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
    )

    assert quality.state == "usable"
    assert quality.selected_evidence_ids
    assert quality.blocked_parent_candidate_ids == ()


def test_persistent_hash_bound_car_interior_semantics_block_whole_window(
    tmp_path,
):
    result = _make_result(
        tmp_path,
        [_car_interior_frame(index) for index in range(7)],
    )
    observer = _HashBoundRoadSceneObserver(
        {frame.sha256: 0.01 for frame in result.frames}
    )

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
        semantic_observer=observer,
    )
    checkpoint = quality.to_checkpoint()

    assert quality.state == "unusable_visual_evidence"
    assert quality.road_scene_semantic_status == "completed"
    assert (
        quality.semantic_model_identity_sha256
        == _SEMANTIC_MODEL_SHA
    )
    assert quality.semantic_prompt_set_sha256 == _SEMANTIC_PROMPT_SHA
    assert quality.road_scene_supported_window_ids == ()
    assert quality.persistent_non_road_window_ids == (_WINDOW_ID,)
    assert quality.selected_evidence_ids == ()
    assert all(
        item.strong_non_road_view
        and "semantic_non_road_camera_view" in item.reason_codes
        for item in quality.frame_observations
    )
    assert (
        checkpoint["road_scene_semantic_status"] == "completed"
    )
    assert checkpoint["automatic_annotation_creation"] is False
    assert checkpoint["automatic_incident_creation"] is False
    assert checkpoint["automatic_training_membership"] is False
    assert checkpoint["automatic_report_truth"] is False


@pytest.mark.parametrize(
    "factory",
    [_road_frame, _night_frame, _tunnel_frame],
    ids=["semantic_road", "semantic_night", "semantic_tunnel"],
)
def test_positive_semantic_road_support_preserves_road_views(
    tmp_path,
    factory,
):
    result = _make_result(
        tmp_path,
        [factory(index) for index in range(5)],
    )
    observer = _HashBoundRoadSceneObserver(
        {frame.sha256: 0.92 for frame in result.frames}
    )

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
        semantic_observer=observer,
    )

    assert quality.state == "usable"
    assert quality.road_scene_supported_window_ids == (_WINDOW_ID,)
    assert quality.persistent_non_road_window_ids == ()
    assert quality.selected_evidence_ids


def test_semantic_minor_non_road_frames_do_not_overrule_road_support(
    tmp_path,
):
    result = _make_result(
        tmp_path,
        [_road_frame(index) for index in range(5)],
    )
    probabilities = {
        frame.sha256: (0.01 if index < 2 else 0.92)
        for index, frame in enumerate(result.frames)
    }

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
        semantic_observer=_HashBoundRoadSceneObserver(
            probabilities
        ),
    )

    assert quality.state == "usable"
    assert quality.road_scene_supported_window_ids == (_WINDOW_ID,)
    assert quality.persistent_non_road_window_ids == ()
    assert len(quality.selected_evidence_ids) == 3


def test_two_suspicious_frames_are_insufficient_to_reject_recall(tmp_path):
    result = _make_result(
        tmp_path,
        [_covered_frame(0), _covered_frame(1)],
    )

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256="f" * 64,
    )

    assert quality.state == "usable"
    assert (
        quality.window_decisions[0].state
        == "indeterminate_allowed_for_recall"
    )
    assert len(quality.selected_evidence_ids) == 2


def test_short_clip_bootstrap_samples_do_not_change_normal_trip_default():
    short = _effective_bootstrap_cadence(
        duration_seconds=13.783,
        configured_cadence_seconds=15.0,
        minimum_gap_seconds=13.783 / 31,
    )
    normal = _effective_bootstrap_cadence(
        duration_seconds=330.0,
        configured_cadence_seconds=15.0,
        minimum_gap_seconds=330.0 / 31,
    )

    assert short == pytest.approx(13.783 / 3)
    assert short * 2 < 13.783
    assert normal == 15.0


@pytest.mark.skipif(
    os.environ.get("TRAFFICVISSON_RUN_LOCAL_CLIP_REGRESSION")
    != "1",
    reason="requires the installed verified CLIP pack and v39 evidence",
)
def test_actual_v39_car_interior_hash_bound_clip_regression():
    rescan_root = Path(
        r"E:\TrafficVisson-QA\north-star-visual-v39-unusable-quality"
        r"\project-artifacts\11111111-1111-4111-8111-111111111111"
        r"\pipeline-jobs\74f30ebc-2b1a-42d1-bb3c-f644fd3fca68"
        r"\candidate-window-rescans"
        r"\candidate-window-rescan-862c3de4c78b7c292b92b281"
    )
    model_manifest = Path(
        r"C:\ProgramData\TrafficVisson\ModelPacks"
        r"\trafficvisson-laion-clip-b32-road-baseline-0.1.0"
        r"\trafficvisson-model-pack.json"
    )
    if not rescan_root.is_dir() or not model_manifest.is_file():
        pytest.skip("local actual-negative artifacts are unavailable")
    expected_actual_hashes = {
        (
            "windows/bf6010d7028ffd5be798/"
            "frame-000011.jpg"
        ): (
            "5e59bc4252a29d49b4b9566056083d78831ec6495941eb0fd"
            "5b5a7cc4c167ba6"
        ),
        (
            "windows/eefb5e5b2e55d96429e8/"
            "frame-000011.jpg"
        ): (
            "4cc693b1874d55b19c780fd6bb266b53bffcaccd95337f5b36"
            "27175b16b0a194"
        ),
    }
    result = load_candidate_window_rescan(
        rescan_root,
        expected_source_sha256=(
            "f95fb472e30185f57a229e395b7a877f4a3d5adaeee86b328339"
            "3d19ece04515"
        ),
        expected_input_revision=(
            "862c3de4c78b7c292b92b281eb7d4985946ac80ed61f529d66538"
            "5ffd9a30e92"
        ),
    )
    manifest_hashes = {
        frame.relative_path: frame.sha256 for frame in result.frames
    }
    assert {
        path: manifest_hashes[path]
        for path in expected_actual_hashes
    } == expected_actual_hashes

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256=(
            "f95fb472e30185f57a229e395b7a877f4a3d5adaeee86b328339"
            "3d19ece04515"
        ),
        semantic_observer=VerifiedLocalClipImageEmbedder(
            model_manifest
        ),
    )

    assert quality.state == "unusable_visual_evidence"
    assert quality.road_scene_semantic_status == "completed"
    assert quality.selected_evidence_ids == ()
    assert quality.eligible_parent_candidate_ids == ()
    assert set(quality.persistent_non_road_window_ids) == {
        "b9129320577b46ff0ea3",
        "bf6010d7028ffd5be798",
        "eefb5e5b2e55d96429e8",
    }
    assert all(
        decision.road_scene_supported_frame_count == 0
        for decision in quality.window_decisions
    )


@pytest.mark.skipif(
    os.environ.get("TRAFFICVISSON_RUN_LOCAL_CLIP_REGRESSION")
    != "1",
    reason="requires the installed verified CLIP pack and v41 evidence",
)
def test_actual_v41_road_view_is_not_overblocked_by_clip_regression():
    rescan_root = Path(
        r"E:\TrafficVisson-QA\north-star-visual-v41-road-positive-quality"
        r"\project-artifacts\11111111-1111-4111-8111-111111111111"
        r"\pipeline-jobs\4793d0fb-a501-41e5-8f8c-6b9157e563a8"
        r"\candidate-window-rescans"
        r"\candidate-window-rescan-302ed318f6562f0d96fbaa82"
    )
    model_manifest = Path(
        r"C:\ProgramData\TrafficVisson\ModelPacks"
        r"\trafficvisson-laion-clip-b32-road-baseline-0.1.0"
        r"\trafficvisson-model-pack.json"
    )
    if not rescan_root.is_dir() or not model_manifest.is_file():
        pytest.skip("local actual-positive artifacts are unavailable")
    result = load_candidate_window_rescan(
        rescan_root,
        expected_source_sha256=(
            "020ac3d642e1b02ad1d3834a5fd98b6e24ad38de00b74262bce93"
            "e4280eb16a4"
        ),
        expected_input_revision=(
            "302ed318f6562f0d96fbaa82c8aecb1628a86dee98390beca775c"
            "01fba367e85"
        ),
    )
    assert result.frames[0].sha256 == (
        "908480880225bab3e9616b4711682aff1c4abc90e4d428b93a195aa7e366679d"
    )

    quality = evaluate_candidate_window_visual_quality(
        result,
        source_sha256=(
            "020ac3d642e1b02ad1d3834a5fd98b6e24ad38de00b74262bce93"
            "e4280eb16a4"
        ),
        semantic_observer=VerifiedLocalClipImageEmbedder(
            model_manifest
        ),
    )

    assert quality.state == "usable"
    assert quality.road_scene_semantic_status == "completed"
    assert len(quality.selected_evidence_ids) == 3
    assert quality.eligible_parent_candidate_ids == (
        "a032a21c2b5fa286c684",
    )
    assert quality.blocked_parent_candidate_ids == ()
    assert quality.persistent_non_road_window_ids == ()
    checkpoint = quality.to_checkpoint()
    assert checkpoint["automatic_annotation_creation"] is False
    assert checkpoint["automatic_incident_creation"] is False


def test_unusable_pipeline_stops_before_models_questions_and_truth(
    tmp_path,
):
    class UnusableRuntime(FakeRuntime):
        def execute(self, stage, context, cancel):
            if stage != "candidate_rescan_5_10fps":
                return super().execute(stage, context, cancel)
            self.calls.append(stage)
            self.contexts.append((stage, context))
            return PipelineStageResult(
                "unusable_visual_evidence",
                checkpoint={
                    "visual_evidence_quality_state": (
                        "unusable_visual_evidence"
                    ),
                    "visual_evidence_quality_revision": "a" * 64,
                    "visual_evidence_quality": {
                        "schema_version": (
                            VISUAL_EVIDENCE_QUALITY_SCHEMA
                        ),
                        "state": "unusable_visual_evidence",
                        "quality_revision": "a" * 64,
                        "automatic_annotation_creation": False,
                        "automatic_incident_creation": False,
                        "automatic_training_membership": False,
                        "automatic_report_truth": False,
                    },
                    "annotation_count": 0,
                    "incident_count": 0,
                    "road_truth_candidate_count": 0,
                },
                metrics={
                    "quality_selected_evidence_count": 0,
                    "road_truth_candidate_count": 0,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                error_code="unusable_visual_evidence",
            )

    runtime = UnusableRuntime()
    service = PipelineJobService(
        runtime,
        data_root=tmp_path.resolve(),
    )
    project_id = __import__("uuid").uuid4()
    _configure(service, project_id)
    started, _ = _start(
        service,
        project_id,
        suffix="unusable-visual-evidence",
    )
    row = _wait(
        service,
        project_id,
        started["job_id"],
        {"completed", "failed"},
    )

    assert row["status"] == "completed"
    assert row["outcome_code"] == "unusable_visual_evidence"
    assert runtime.calls == list(
        PIPELINE_STAGES[
            : PIPELINE_STAGES.index(
                "candidate_rescan_5_10fps"
            )
            + 1
        ]
    )
    assert all(
        row["stages"][index]["status"] == "skipped"
        and row["stages"][index]["outcome"]
        == "unusable_visual_evidence"
        for index in range(
            PIPELINE_STAGES.index(
                "candidate_rescan_5_10fps"
            )
            + 1,
            len(PIPELINE_STAGES),
        )
    )
    assert (
        row["stages"][
            PIPELINE_STAGES.index(
                "candidate_rescan_5_10fps"
            )
        ]["checkpoint"]["annotation_count"]
        == 0
    )
    assert (
        row["stages"][
            PIPELINE_STAGES.index(
                "candidate_rescan_5_10fps"
            )
        ]["checkpoint"]["incident_count"]
        == 0
    )
    assert PipelineJobResponse.model_validate(row).outcome_code == (
        "unusable_visual_evidence"
    )
