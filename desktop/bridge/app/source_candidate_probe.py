"""Read-only observation of known local evaluation evidence.

The probe reads only a fixed set of bounded files below ProgramData. It never
discovers arbitrary paths, downloads content, imports labels, or loads model
weights. A local state is considered verified only when every expected evidence
file exists and matches its pinned SHA-256.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional


MAX_EVIDENCE_BYTES = 2 * 1024 * 1024

SOURCE_EVIDENCE_SPECS: Mapping[str, Mapping[str, Any]] = {
    "idea-research-grounding-dino-tiny": {
        "files": (
            (
                "ModelPacks/"
                "trafficvisson-idea-grounding-dino-tiny-evaluation-0.1.0/"
                "trafficvisson-grounding-dino-pack.json",
                "09f90e0a0122558a4f7263efb3fdc3c0d341e501984aba1880308ce3265811ec",
            ),
            (
                "ModelEvaluations/"
                "grounding-dino-tiny-20260728-deterministic-30-v2/"
                "README.md",
                "691a44cead25eb106030b6e65411142524d666b198d1e465df83ae6f0845a9b1",
            ),
            (
                "ModelEvaluations/"
                "grounding-dino-tiny-20260728-deterministic-30-v2/"
                "manifest.json",
                "aec33df6518ea5758a583fbfb9b9ee0a6d2c1184e875397ffbbb486793aa7e21",
            ),
        ),
        "evaluation": {
            "evaluated_frame_count": 30,
            "proposal_frame_count": 25,
            "proposal_count": 47,
            "false_positive_count": None,
            "verdict": "review_only",
        },
    },
    "laion-clip-b32-road-baseline": {
        "files": (
            (
                "ModelPacks/"
                "trafficvisson-laion-clip-b32-road-baseline-0.1.0/"
                "trafficvisson-model-pack.json",
                "c73aa8c341d8b6276140a4cfbd088009f2a2ec49270aa36320be3c1c09ca72c9",
            ),
            (
                "ModelEvaluations/"
                "clip-road-baseline-20260728-all-existing-candidates-v3-sidecar/"
                "EVALUATION.md",
                "9e97c61e83c06ce79067fa6f82fb40db8ee346caae8096f9a88a448600302659",
            ),
            (
                "ModelEvaluations/"
                "clip-road-baseline-20260728-all-existing-candidates-v3-sidecar/"
                "manifest.json",
                "04635e22568f30fb816466ce3cc0a7293c72237a602ae1b87bb81d65567649cc",
            ),
        ),
        "evaluation": {
            "evaluated_frame_count": 349,
            "proposal_frame_count": 29,
            "proposal_count": 29,
            "false_positive_count": None,
            "verdict": "review_only",
        },
    },
    "peterhdd-pothole-yolov8-local-cache": {
        "files": (
            (
                "ModelPacks/"
                "trafficvisson-peterhdd-yolov8s-pothole-research-0.1.1/"
                "trafficvisson-model-pack.json",
                "8eab0b0baa353866e42ef7866d911a23e7e77ade67c2ed3ac081aef17174c0a4",
            ),
            (
                "ModelEvaluations/"
                "peterhdd-yolov8s-pothole-20260728-all-existing-candidates-v3-sidecar/"
                "EVALUATION.md",
                "de4d5a4a314535fc79cc3591bba9ea4eb1d955ecf1eef3c2b849516f97bdea36",
            ),
            (
                "ModelEvaluations/"
                "peterhdd-yolov8s-pothole-20260728-all-existing-candidates-v3-sidecar/"
                "manifest.json",
                "202b2403cd25a21b8797ba02efc3d8438c99e2d43b091bb023d7a6212c944d61",
            ),
        ),
        "evaluation": {
            "evaluated_frame_count": 349,
            "proposal_frame_count": 19,
            "proposal_count": 19,
            "false_positive_count": 19,
            "verdict": "rejected",
        },
    },
}


class SourceCandidateProbe:
    """Observe pinned local evidence without exposing its filesystem path."""

    def __init__(self, trafficvisson_program_data: Optional[Path]) -> None:
        self._root = (
            Path(trafficvisson_program_data)
            if trafficvisson_program_data is not None
            else None
        )

    @classmethod
    def from_environment(cls) -> "SourceCandidateProbe":
        program_data = os.environ.get("PROGRAMDATA")
        if not program_data:
            return cls(None)
        root = Path(program_data)
        if not root.is_absolute():
            return cls(None)
        return cls(root / "TrafficVisson")

    def _read_exact(
        self,
        root: Path,
        relative_path: str,
        expected_sha256: str,
    ) -> bool:
        parts = PurePosixPath(relative_path).parts
        if (
            not parts
            or PurePosixPath(relative_path).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
        ):
            return False
        candidate = root.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
            if os.path.commonpath((str(root), str(resolved))) != str(root):
                return False
            if not resolved.is_file():
                return False
            with resolved.open("rb") as source:
                raw = source.read(MAX_EVIDENCE_BYTES + 1)
            if not raw or len(raw) > MAX_EVIDENCE_BYTES:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        return hashlib.sha256(raw).hexdigest() == expected_sha256

    def snapshot(self) -> Dict[str, Any]:
        try:
            root = self._root.resolve(strict=True) if self._root else None
        except (OSError, RuntimeError):
            root = None
        sources: Dict[str, Any] = {}
        for source_id, spec in SOURCE_EVIDENCE_SPECS.items():
            files = spec["files"]
            if root is None:
                state = "absent"
            else:
                paths = [
                    root.joinpath(*PurePosixPath(relative_path).parts)
                    for relative_path, _ in files
                ]
                present = [path.exists() for path in paths]
                if not any(present):
                    state = "absent"
                elif not all(present):
                    state = "unverified_local"
                elif all(
                    self._read_exact(root, relative_path, expected_sha256)
                    for relative_path, expected_sha256 in files
                ):
                    state = "verified_snapshot"
                else:
                    state = "unverified_local"
            sources[source_id] = {
                "state": state,
                "evaluation": (
                    dict(spec["evaluation"])
                    if state == "verified_snapshot"
                    else None
                ),
            }
        return {
            "schema_version": "trafficvisson.local-source-observation.v1",
            "sources": sources,
        }
