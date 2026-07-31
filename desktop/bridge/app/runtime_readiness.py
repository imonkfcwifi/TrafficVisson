"""Scrubbed readiness inventory for TrafficSuperVision's installed runtime.

The inventory never imports model packages, reads provider authentication
files, downloads weights, or executes a model.  Python package versions come
from distribution metadata; command-model and provider states come from their
existing bounded, allowlisted status services.

``ready`` means the component's own local integrity/readiness contract passed.
``incomplete`` means some runtime is installed but the required model weights
or CUDA smoke test is absent.  ``unavailable`` and ``failed`` remain distinct,
and zero detections are intentionally a PipelineJob result rather than a
runtime-readiness state.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import platform
import re
import shutil
import stat
from typing import Any, Callable, Mapping, Optional, Sequence

from .external_model_runtime import external_model_runtime_status
from .provider_status import ProviderStatusService


RUNTIME_READINESS_SCHEMA = "trafficvisson.runtime-readiness.v1"

_COMPONENT_IDS = (
    "electron_host",
    "python_bridge",
    "pytorch_cuda",
    "grounding_dino",
    "sam2",
    "opencv",
    "ffmpeg",
    "korean_ocr",
    "embedding_search",
    "codex_cli",
    "claude_code",
    "hancom_com",
    "local_model_provider",
)
_COMPONENT_STATES = frozenset(
    {
        "ready",
        "incomplete",
        "unavailable",
        "failed",
        "external_required",
        "disabled",
    }
)
_SAFE_VERSION_CHARS = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.-+_ "
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SAFE_REVISION = re.compile(r"^[a-f0-9]{40,64}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_CUDA_VERSION = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}$")


@dataclass(frozen=True)
class RuntimeComponent:
    component_id: str
    display_name: str
    state: str
    management: str
    required_for: tuple[str, ...]
    message: str
    version: Optional[str] = None
    error_code: Optional[str] = None
    model_pack: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.component_id not in _COMPONENT_IDS:
            raise ValueError("runtime component is unsupported")
        if self.state not in _COMPONENT_STATES:
            raise ValueError("runtime component state is unsupported")
        if not 1 <= len(self.display_name) <= 80:
            raise ValueError("runtime component display name is invalid")
        if not 1 <= len(self.message) <= 500:
            raise ValueError("runtime component message is invalid")
        if self.version is not None and _safe_version(self.version) is None:
            raise ValueError("runtime component version is invalid")

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "component_id": self.component_id,
            "display_name": self.display_name,
            "state": self.state,
            "management": self.management,
            "required_for": list(self.required_for),
            "message": self.message,
        }
        if self.version is not None:
            row["version"] = self.version
        if self.error_code is not None:
            row["error_code"] = self.error_code
        if self.model_pack is not None:
            row["model_pack"] = dict(self.model_pack)
        return row


class RuntimeReadinessService:
    def __init__(
        self,
        *,
        provider_status: ProviderStatusService,
        vision_status: Callable[[], Mapping[str, Any]],
        command_model_status: Callable[[], Mapping[str, Any]],
        hwp_preflight: Callable[[], object],
        sam2_status: Optional[Callable[[], Mapping[str, Any]]] = None,
        ffmpeg_path: Optional[str] = None,
        distribution_version: Callable[[str], str] = metadata.version,
        which: Callable[[str], Optional[str]] = shutil.which,
        python_version: str = platform.python_version(),
        electron_version: Optional[str] = None,
        model_runtime_status: Callable[
            [], Mapping[str, Any]
        ] = external_model_runtime_status,
    ) -> None:
        self._provider_status = provider_status
        self._vision_status = vision_status
        self._command_model_status = command_model_status
        self._sam2_status = sam2_status
        self._hwp_preflight = hwp_preflight
        self._ffmpeg_path = ffmpeg_path
        self._distribution_version = distribution_version
        self._which = which
        self._python_version = _safe_version(python_version)
        self._electron_version = _safe_version(electron_version)
        self._model_runtime_status = model_runtime_status

    def snapshot(self) -> dict[str, Any]:
        vision, vision_status_failed = self._read_status(self._vision_status)
        command_model, command_status_failed = self._read_status(
            self._command_model_status
        )
        if self._sam2_status is None:
            sam2 = {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            }
            sam2_status_failed = False
        else:
            sam2, sam2_status_failed = self._read_status(self._sam2_status)
        model_runtime, model_runtime_status_failed = self._read_status(
            self._model_runtime_status
        )
        components = [
            RuntimeComponent(
                component_id="electron_host",
                display_name="Electron Windows host",
                state="ready" if self._electron_version else "incomplete",
                management="installer",
                required_for=("desktop_ui", "bridge_supervision"),
                message=(
                    (
                        "현재 브리지는 Electron 호스트의 인증된 루프백 "
                        "연결에서 실행 중입니다."
                    )
                    if self._electron_version
                    else (
                        "Electron 호스트 버전 증명이 전달되지 않아 "
                        "호스트 준비 완료로 확정하지 않습니다."
                    )
                ),
                version=self._electron_version,
                error_code=(
                    None
                    if self._electron_version
                    else "electron_version_not_reported"
                ),
            ),
            RuntimeComponent(
                component_id="python_bridge",
                display_name="Python local bridge",
                state="ready",
                management="installer",
                required_for=("analysis", "gps", "csv", "hwp_com"),
                message="번들 또는 고정 Python 브리지가 실행 중입니다.",
                version=self._python_version,
            ),
            self._pytorch_component(
                model_runtime,
                status_failed=model_runtime_status_failed,
            ),
            self._command_model_component(
                command_model, status_failed=command_status_failed
            ),
            self._sam2_component(
                sam2,
                status_failed=sam2_status_failed,
                model_runtime=model_runtime,
                model_runtime_status_failed=model_runtime_status_failed,
            ),
            self._python_component(
                component_id="opencv",
                display_name="OpenCV",
                distributions=("opencv-python-headless", "opencv-python"),
                required_for=("frame_processing", "privacy_derivatives"),
                installed_message="OpenCV 배포판 메타데이터를 확인했습니다.",
                missing_message="OpenCV 런타임이 설치되지 않았습니다.",
                installed_state="ready",
            ),
            self._ffmpeg_component(),
            self._capability_component(
                component_id="korean_ocr",
                display_name="Korean OCR",
                capability="ocr",
                vision=vision,
                fallback_distributions=("easyocr", "paddleocr"),
                required_for=("guide_sign_identity", "tunnel_identity"),
                status_failed=vision_status_failed,
                model_runtime=model_runtime,
                runtime_capability="korean_ocr",
                model_runtime_status_failed=model_runtime_status_failed,
            ),
            self._capability_component(
                component_id="embedding_search",
                display_name="Image embedding search",
                capability="candidate_classification",
                vision=vision,
                fallback_distributions=("transformers", "sentence-transformers"),
                required_for=("similar_case_search",),
                status_failed=vision_status_failed,
                model_runtime=model_runtime,
                runtime_capability="embedding_search",
                model_runtime_status_failed=model_runtime_status_failed,
            ),
            self._provider_component("codex", "codex_cli", "Codex CLI"),
            self._provider_component(
                "claude", "claude_code", "Claude Code"
            ),
            self._hwp_component(),
            RuntimeComponent(
                component_id="local_model_provider",
                display_name="Local Model Provider",
                state="disabled",
                management="provider_slot",
                required_for=("future_structured_judgment",),
                message=(
                    "계약 슬롯만 등록되어 있습니다. 보고서 판단 Provider로 "
                    "자동 활성화되지 않습니다."
                ),
                error_code="provider_not_implemented",
            ),
        ]
        counts = {
            state: sum(item.state == state for item in components)
            for state in sorted(_COMPONENT_STATES)
        }
        return {
            "schema_version": RUNTIME_READINESS_SCHEMA,
            "provider_policy": {
                "bulk_frame_detection": "local_models_only",
                "structured_judgment_providers": ["codex", "claude"],
                "provider_authentication_owner": "official_cli",
                "authentication_files_read_by_app": False,
                "local_model_provider": "disabled",
            },
            "pipeline_outcome_contract": {
                "model_unavailable": "unavailable",
                "model_runtime_failed": "failed",
                "successful_zero_predictions": "zero_detections",
            },
            "counts": counts,
            "components": [item.as_dict() for item in components],
        }

    def _command_model_component(
        self,
        status: Mapping[str, Any],
        *,
        status_failed: bool = False,
    ) -> RuntimeComponent:
        if status_failed:
            return RuntimeComponent(
                component_id="grounding_dino",
                display_name="Grounding DINO",
                state="failed",
                management="model_pack",
                required_for=("natural_language_box_proposals",),
                message="Grounding DINO Model Pack 상태 확인에 실패했습니다.",
                error_code="model_pack_status_failed",
            )
        state = status.get("state")
        if state == "ready":
            pack = _verified_model_pack(status.get("model_pack"))
            if pack is None:
                return RuntimeComponent(
                    component_id="grounding_dino",
                    display_name="Grounding DINO",
                    state="incomplete",
                    management="model_pack",
                    required_for=("natural_language_box_proposals",),
                    message=(
                        "실행기는 있으나 모델명·출처 revision·라이선스·"
                        "artifact SHA-256·GPU 요구조건 증빙이 완전하지 않습니다."
                    ),
                    error_code="model_pack_provenance_incomplete",
                )
            return RuntimeComponent(
                component_id="grounding_dino",
                display_name="Grounding DINO",
                state="ready",
                management="model_pack",
                required_for=("natural_language_box_proposals",),
                message=(
                    "고정 해시 Model Pack이 검토용 박스 제안에만 "
                    "활성화되어 있습니다."
                ),
                model_pack=pack,
            )
        if state == "failed":
            return RuntimeComponent(
                component_id="grounding_dino",
                display_name="Grounding DINO",
                state="failed",
                management="model_pack",
                required_for=("natural_language_box_proposals",),
                message="설치된 Grounding DINO Model Pack 무결성 검증에 실패했습니다.",
                error_code="model_pack_invalid",
            )
        return RuntimeComponent(
            component_id="grounding_dino",
            display_name="Grounding DINO",
            state="unavailable",
            management="model_pack",
            required_for=("natural_language_box_proposals",),
            message="검증된 Grounding DINO Model Pack이 설치되지 않았습니다.",
            error_code="model_pack_not_configured",
        )

    def _sam2_component(
        self,
        status: Mapping[str, Any],
        *,
        status_failed: bool = False,
        model_runtime: Mapping[str, Any],
        model_runtime_status_failed: bool,
    ) -> RuntimeComponent:
        if status_failed:
            return RuntimeComponent(
                component_id="sam2",
                display_name="SAM 2 mask and tracking",
                state="failed",
                management="model_pack",
                required_for=("mask_refinement", "temporal_tracking"),
                message="SAM 2 Model Pack 상태 확인에 실패했습니다.",
                error_code="model_pack_status_failed",
            )
        state = status.get("state")
        if state == "ready":
            runtime_gate = self._model_runtime_gate(
                model_runtime,
                "sam2",
                component_id="sam2",
                display_name="SAM 2 mask and tracking",
                required_for=("mask_refinement", "temporal_tracking"),
                status_failed=model_runtime_status_failed,
            )
            if runtime_gate is not None:
                return runtime_gate
            pack = _verified_model_pack(status.get("model_pack"))
            if pack is not None:
                return RuntimeComponent(
                    component_id="sam2",
                    display_name="SAM 2 mask and tracking",
                    state="ready",
                    management="model_pack",
                    required_for=("mask_refinement", "temporal_tracking"),
                    message=(
                        "검증된 SAM 2 Model Pack이 사용자 확인 전 "
                        "마스크·추적 제안에만 활성화되어 있습니다."
                    ),
                    model_pack=pack,
                )
            state = "failed"
        if state == "failed":
            return RuntimeComponent(
                component_id="sam2",
                display_name="SAM 2 mask and tracking",
                state="failed",
                management="model_pack",
                required_for=("mask_refinement", "temporal_tracking"),
                message=(
                    "설치된 SAM 2 구성의 매니페스트 provenance 또는 "
                    "무결성 검증에 실패했습니다."
                ),
                error_code="model_pack_invalid",
            )
        return RuntimeComponent(
            component_id="sam2",
            display_name="SAM 2 mask and tracking",
            state="unavailable",
            management="model_pack",
            required_for=("mask_refinement", "temporal_tracking"),
            message=(
                "검증된 SAM 2 가중치·라이선스·GPU 매니페스트가 "
                "설치되지 않았습니다."
            ),
            error_code="model_pack_not_configured",
        )

    def _python_component(
        self,
        *,
        component_id: str,
        display_name: str,
        distributions: Sequence[str],
        required_for: tuple[str, ...],
        installed_message: str,
        missing_message: str,
        installed_state: str,
    ) -> RuntimeComponent:
        for name in distributions:
            version = self._distribution(name)
            if version is not None:
                return RuntimeComponent(
                    component_id=component_id,
                    display_name=display_name,
                    state=installed_state,
                    management="installer_or_model_pack",
                    required_for=required_for,
                    message=installed_message,
                    version=version,
                )
        return RuntimeComponent(
            component_id=component_id,
            display_name=display_name,
            state="unavailable",
            management="installer_or_model_pack",
            required_for=required_for,
            message=missing_message,
            error_code="runtime_not_installed",
        )

    def _pytorch_component(
        self,
        runtime: Mapping[str, Any],
        *,
        status_failed: bool,
    ) -> RuntimeComponent:
        if status_failed:
            return RuntimeComponent(
                component_id="pytorch_cuda",
                display_name="PyTorch CUDA",
                state="failed",
                management="installer_or_model_pack",
                required_for=("training", "sam2", "korean_ocr"),
                message="외부 모델 런타임 상태 확인에 실패했습니다.",
                error_code="model_runtime_status_failed",
            )
        runtime_state = runtime.get("state")
        capabilities = {
            value
            for value in (runtime.get("capabilities") or ())
            if isinstance(value, str)
        }
        if runtime_state == "ready" and "pytorch_cuda" in capabilities:
            version = self._distribution("torch")
            if (
                version is not None
                and runtime.get("cuda_verified") is True
                and isinstance(
                    runtime.get("manifest_sha256"), str
                )
                and _SHA256.fullmatch(
                    str(runtime["manifest_sha256"])
                )
                is not None
            ):
                return RuntimeComponent(
                    component_id="pytorch_cuda",
                    display_name="PyTorch CUDA",
                    state="ready",
                    management="installer_or_model_pack",
                    required_for=("training", "sam2", "korean_ocr"),
                    message=(
                        "버전·라이선스·artifact 해시가 고정된 외부 "
                        "런타임에서 CUDA와 최소 VRAM을 확인했습니다."
                    ),
                    version=version,
                )
            return RuntimeComponent(
                component_id="pytorch_cuda",
                display_name="PyTorch CUDA",
                state="failed",
                management="installer_or_model_pack",
                required_for=("training", "sam2", "korean_ocr"),
                message="외부 모델 런타임의 CUDA 증빙이 완전하지 않습니다.",
                error_code="model_runtime_provenance_incomplete",
            )
        if runtime_state == "failed":
            return RuntimeComponent(
                component_id="pytorch_cuda",
                display_name="PyTorch CUDA",
                state="failed",
                management="installer_or_model_pack",
                required_for=("training", "sam2", "korean_ocr"),
                message=(
                    "구성된 외부 모델 런타임이 무결성·ABI·"
                    "라이선스·CUDA 검증을 통과하지 못했습니다."
                ),
                error_code="model_runtime_pack_invalid",
            )
        return self._python_component(
            component_id="pytorch_cuda",
            display_name="PyTorch CUDA",
            distributions=("torch",),
            required_for=("training", "sam2", "korean_ocr"),
            installed_message=(
                "PyTorch는 설치되어 있으나 CUDA 장치·VRAM 스모크 "
                "검증 전에는 GPU 준비 완료로 표시하지 않습니다."
            ),
            missing_message="PyTorch CUDA 런타임이 Model Pack에 없습니다.",
            installed_state="incomplete",
        )

    @staticmethod
    def _model_runtime_gate(
        runtime: Mapping[str, Any],
        capability: str,
        *,
        component_id: str,
        display_name: str,
        required_for: tuple[str, ...],
        status_failed: bool,
    ) -> Optional[RuntimeComponent]:
        if status_failed:
            return RuntimeComponent(
                component_id=component_id,
                display_name=display_name,
                state="failed",
                management="model_pack",
                required_for=required_for,
                message="외부 모델 런타임 상태 확인에 실패했습니다.",
                error_code="model_runtime_status_failed",
            )
        state = runtime.get("state")
        if state == "failed":
            return RuntimeComponent(
                component_id=component_id,
                display_name=display_name,
                state="failed",
                management="model_pack",
                required_for=required_for,
                message=(
                    "선택된 모델은 외부 런타임 무결성·ABI·"
                    "라이선스·CUDA 검증 실패로 실행할 수 없습니다."
                ),
                error_code="model_runtime_pack_invalid",
            )
        if state == "ready":
            capabilities = {
                value
                for value in (runtime.get("capabilities") or ())
                if isinstance(value, str)
            }
            if capability not in capabilities:
                return RuntimeComponent(
                    component_id=component_id,
                    display_name=display_name,
                    state="failed",
                    management="model_pack",
                    required_for=required_for,
                    message=(
                        "선택된 외부 런타임이 이 모델 기능을 "
                        "명시적으로 포함하지 않습니다."
                    ),
                    error_code="model_runtime_capability_missing",
                )
        return None

    def _capability_component(
        self,
        *,
        component_id: str,
        display_name: str,
        capability: str,
        vision: Mapping[str, Any],
        fallback_distributions: Sequence[str],
        required_for: tuple[str, ...],
        status_failed: bool = False,
        model_runtime: Mapping[str, Any],
        runtime_capability: str,
        model_runtime_status_failed: bool,
    ) -> RuntimeComponent:
        if status_failed:
            return RuntimeComponent(
                component_id=component_id,
                display_name=display_name,
                state="failed",
                management="model_pack",
                required_for=required_for,
                message="로컬 비전 Model Pack 상태 확인에 실패했습니다.",
                error_code="model_pack_status_failed",
            )
        capabilities = {
            value
            for value in (vision.get("capabilities") or ())
            if isinstance(value, str)
        }
        if vision.get("enabled") is True and capability in capabilities:
            runtime_gate = self._model_runtime_gate(
                model_runtime,
                runtime_capability,
                component_id=component_id,
                display_name=display_name,
                required_for=required_for,
                status_failed=model_runtime_status_failed,
            )
            if runtime_gate is not None:
                return runtime_gate
            pack = _verified_model_pack(vision.get("model_pack"))
            if pack is not None:
                return RuntimeComponent(
                    component_id=component_id,
                    display_name=display_name,
                    state="ready",
                    management="model_pack",
                    required_for=required_for,
                    message="검증된 로컬 Model Pack 기능이 활성화되어 있습니다.",
                    model_pack=pack,
                )
            return RuntimeComponent(
                component_id=component_id,
                display_name=display_name,
                state="incomplete",
                management="model_pack",
                required_for=required_for,
                message=(
                    "가중치 무결성은 확인됐지만 모델명·고정 출처 revision·"
                    "라이선스·artifact SHA-256·GPU 요구조건 증빙이 "
                    "완전하지 않아 준비 완료로 승격하지 않습니다."
                ),
                error_code="model_pack_provenance_incomplete",
            )
        for distribution in fallback_distributions:
            version = self._distribution(distribution)
            if version is not None:
                return RuntimeComponent(
                    component_id=component_id,
                    display_name=display_name,
                    state="incomplete",
                    management="model_pack",
                    required_for=required_for,
                    message=(
                        "런타임은 있으나 고정 가중치·해시·라이선스 "
                        "Model Pack이 구성되지 않았습니다."
                    ),
                    version=version,
                    error_code="model_pack_not_configured",
                )
        return RuntimeComponent(
            component_id=component_id,
            display_name=display_name,
            state="unavailable",
            management="model_pack",
            required_for=required_for,
            message="필요한 런타임과 검증된 Model Pack이 없습니다.",
            error_code="model_pack_not_configured",
        )

    def _ffmpeg_component(self) -> RuntimeComponent:
        raw = self._ffmpeg_path or self._which("ffmpeg")
        if raw:
            path = Path(raw)
            try:
                if (
                    path.is_absolute()
                    and path.name.casefold() in {"ffmpeg", "ffmpeg.exe"}
                    and not path.is_symlink()
                    and not _is_reparse_point(path)
                    and path.resolve(strict=True).is_file()
                ):
                    return RuntimeComponent(
                        component_id="ffmpeg",
                        display_name="FFmpeg",
                        state="ready",
                        management="installer",
                        required_for=(
                            "media_probe",
                            "dense_index",
                            "preview_clip",
                        ),
                        message=(
                            "고정 실행 파일이 확인되었습니다. 실제 버전과 "
                            "인코더는 작업 시작 시 다시 검증합니다."
                        ),
                    )
            except OSError:
                pass
        return RuntimeComponent(
            component_id="ffmpeg",
            display_name="FFmpeg",
            state="unavailable",
            management="installer",
            required_for=("media_probe", "dense_index", "preview_clip"),
            message="고정 FFmpeg 실행 파일이 없습니다.",
            error_code="runtime_not_installed",
        )

    def _provider_component(
        self, provider: str, component_id: str, display_name: str
    ) -> RuntimeComponent:
        status = self._provider_status.get_status(provider)
        if status.state == "ready":
            state = "ready"
            error_code = None
        elif status.state in {
            "not_installed",
            "not_connected",
            "installed",
            "expired",
            "network_unavailable",
        }:
            state = "external_required"
            error_code = status.state
        else:
            state = "failed"
            error_code = "provider_status_failed"
        return RuntimeComponent(
            component_id=component_id,
            display_name=display_name,
            state=state,
            management="official_cli",
            required_for=("structured_judgment", "report_draft"),
            message=status.message,
            version=status.version,
            error_code=error_code,
        )

    def _hwp_component(self) -> RuntimeComponent:
        try:
            preflight = self._hwp_preflight()
        except Exception:
            return RuntimeComponent(
                component_id="hancom_com",
                display_name="Hancom HWP COM",
                state="failed",
                management="external_installation",
                required_for=("hwp_apply", "hwp_rollback"),
                message="한/글 COM 등록 상태 확인에 실패했습니다.",
                error_code="hwp_preflight_failed",
            )
        available = (
            preflight.get("available")
            if isinstance(preflight, Mapping)
            else getattr(preflight, "available", False)
        )
        if available is True:
            return RuntimeComponent(
                component_id="hancom_com",
                display_name="Hancom HWP COM",
                state="ready",
                management="external_installation",
                required_for=("hwp_apply", "hwp_rollback"),
                message="한/글 COM과 보안 모듈이 준비되었습니다.",
            )
        return RuntimeComponent(
            component_id="hancom_com",
            display_name="Hancom HWP COM",
            state="external_required",
            management="external_installation",
            required_for=("hwp_apply", "hwp_rollback"),
            message="한컴오피스와 COM 등록이 필요합니다.",
            error_code="hwp_not_available",
        )

    def _distribution(self, name: str) -> Optional[str]:
        try:
            return _safe_version(self._distribution_version(name))
        except metadata.PackageNotFoundError:
            return None
        except Exception:
            return None

    @staticmethod
    def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        return dict(value)

    @classmethod
    def _read_status(
        cls, callback: Callable[[], Mapping[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        try:
            value = callback()
        except Exception:
            return {}, True
        return cls._safe_mapping(value), not isinstance(value, Mapping)


def _safe_version(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 64
        or any(character not in _SAFE_VERSION_CHARS for character in candidate)
    ):
        return None
    return candidate


def _bounded_public_text(value: object, maximum: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(ord(character) < 0x20 for character in candidate)
    ):
        return None
    return candidate


def _verified_model_pack(value: object) -> Optional[dict[str, Any]]:
    """Return only a complete, path-free model-pack provenance record."""

    required = {
        "provenance_verified",
        "provider",
        "pack_id",
        "pack_version",
        "manifest_sha256",
        "model_name",
        "model_version",
        "model_artifact_sha256",
        "source_repo_id",
        "source_revision",
        "weights_spdx",
        "runtime_spdx",
        "commercial_use_confirmed",
        "redistribution_allowed",
        "gpu_required",
        "cpu_fallback",
        "recommended_min_vram_mb",
        "cuda_min_version",
        "review_only",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        return None
    provider = value.get("provider")
    pack_id = value.get("pack_id")
    pack_version = _safe_version(value.get("pack_version"))
    manifest_sha256 = value.get("manifest_sha256")
    model_name = _bounded_public_text(value.get("model_name"), 128)
    model_version = _bounded_public_text(value.get("model_version"), 128)
    model_artifact_sha256 = value.get("model_artifact_sha256")
    source_repo_id = value.get("source_repo_id")
    source_revision = value.get("source_revision")
    weights_spdx = value.get("weights_spdx")
    runtime_spdx = value.get("runtime_spdx")
    recommended_vram = value.get("recommended_min_vram_mb")
    cuda_min_version = value.get("cuda_min_version")
    if (
        value.get("provenance_verified") is not True
        or not isinstance(provider, str)
        or _SAFE_IDENTIFIER.fullmatch(provider) is None
        or not isinstance(pack_id, str)
        or _SAFE_IDENTIFIER.fullmatch(pack_id) is None
        or pack_version is None
        or not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
        or model_name is None
        or model_version is None
        or not isinstance(model_artifact_sha256, str)
        or _SHA256.fullmatch(model_artifact_sha256) is None
        or not isinstance(source_repo_id, str)
        or _SAFE_SOURCE_ID.fullmatch(source_repo_id) is None
        or not isinstance(source_revision, str)
        or _SAFE_REVISION.fullmatch(source_revision) is None
        or not isinstance(weights_spdx, str)
        or _SPDX.fullmatch(weights_spdx) is None
        or not isinstance(runtime_spdx, str)
        or _SPDX.fullmatch(runtime_spdx) is None
        or value.get("commercial_use_confirmed") is not True
        or not isinstance(value.get("redistribution_allowed"), bool)
        or not isinstance(value.get("gpu_required"), bool)
        or not isinstance(value.get("cpu_fallback"), bool)
        or isinstance(recommended_vram, bool)
        or not isinstance(recommended_vram, int)
        or not 0 <= recommended_vram <= 131_072
        or not (
            cuda_min_version is None
            or (
                isinstance(cuda_min_version, str)
                and _CUDA_VERSION.fullmatch(cuda_min_version) is not None
            )
        )
        or (value["gpu_required"] and cuda_min_version is None)
        or value.get("review_only") is not True
    ):
        return None
    return {
        "provenance_verified": True,
        "provider": provider,
        "pack_id": pack_id,
        "pack_version": pack_version,
        "manifest_sha256": manifest_sha256,
        "model_name": model_name,
        "model_version": model_version,
        "model_artifact_sha256": model_artifact_sha256,
        "source_repo_id": source_repo_id,
        "source_revision": source_revision,
        "weights_spdx": weights_spdx,
        "runtime_spdx": runtime_spdx,
        "commercial_use_confirmed": True,
        "redistribution_allowed": value["redistribution_allowed"],
        "gpu_required": value["gpu_required"],
        "cpu_fallback": value["cpu_fallback"],
        "recommended_min_vram_mb": recommended_vram,
        "cuda_min_version": cuda_min_version,
        "review_only": True,
    }


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


__all__ = [
    "RUNTIME_READINESS_SCHEMA",
    "RuntimeComponent",
    "RuntimeReadinessService",
]
