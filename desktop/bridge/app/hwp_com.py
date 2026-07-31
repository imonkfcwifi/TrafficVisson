"""Conservative Windows HWP/HWPX automation for TrafficSuperVision.

The module is intentionally independent from the HTTP bridge and from any
other product.  Trusted bridge code resolves user selections before calling
this service.  COM is created only on one dedicated STA thread; registry
preflight never launches Hangul.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import stat as stat_module
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import uuid4


HWP_PROG_ID = "HWPFrame.HwpObject"
SECURITY_MODULE = "FilePathCheckerModule"
SECURITY_MODULE_DLL = "FilePathCheckDLL"
PLAN_VERSION = "trafficvisson-document-edit-plan-v1"
ALLOWED_OPERATIONS = frozenset(
    {"fill_text", "fill_cell", "add_table_row", "insert_image", "insert_map"}
)
DOCUMENT_SUFFIXES = frozenset({".hwp", ".hwpx"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp"})
MAX_OPERATIONS = 500
MAX_TEXT_CHARS = 100_000
MAX_HWPML_CHARS = 2_000_000
MAX_STRUCTURE_NAMES = 100
MAX_EXPORTED_PDF_BYTES = 512 * 1024 * 1024
COPY_CHUNK_BYTES = 4 * 1024 * 1024


class HwpAutomationError(RuntimeError):
    """A caller-safe failure with no filesystem path or COM implementation detail."""


class HwpPreflightReader(Protocol):
    def prog_id_registered(self, prog_id: str) -> bool: ...

    def module_registered(self, module_name: str) -> bool: ...


class HwpAdapter(Protocol):
    def register_security_module(self) -> None: ...

    def open_document(self, path: Path) -> None: ...

    def read_hwpml(self, max_chars: int) -> Tuple[str, bool]: ...

    def fill_text(self, target: "EditTarget", text: str) -> None: ...

    def fill_cell(self, target: "EditTarget", text: str) -> None: ...

    def add_table_row(
        self, target: "EditTarget", cells: Sequence[Tuple["EditTarget", str]]
    ) -> None: ...

    def insert_image(self, target: "EditTarget", source_path: Path) -> None: ...

    def insert_map(self, target: "EditTarget", source_path: Path) -> None: ...

    def save_as(self, output_path: Path) -> None: ...

    def close_document(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class HwpPreflight:
    available: bool
    prog_id_registered: bool
    security_module_registered: bool
    message: str


@dataclass(frozen=True)
class EditTarget:
    kind: str
    name: str


@dataclass(frozen=True)
class ValidatedOperation:
    op: str
    target: EditTarget
    text: Optional[str] = None
    cells: Tuple[Tuple[EditTarget, str], ...] = ()
    source_path: Optional[Path] = None
    source_identity: Optional["_FileIdentity"] = None


@dataclass(frozen=True)
class StructureSummary:
    observed_characters: int
    truncated: bool
    paragraph_count: int
    table_count: int
    picture_count: int
    field_names: Tuple[str, ...]
    bookmark_names: Tuple[str, ...]


@dataclass(frozen=True)
class HwpPreviewResult:
    mode: str
    operation_count: int
    operation_types: Mapping[str, int]
    structure: StructureSummary
    message: str
    document_fingerprint: "HwpDocumentFingerprint"


@dataclass(frozen=True)
class HwpApplyResult:
    mode: str
    rollback_token: str
    output_path: Path
    backup_path: Path
    size_bytes: int
    sha256: str
    readback: StructureSummary
    message: str


@dataclass(frozen=True)
class HwpRollbackResult:
    rolled_back: bool
    output_path: Path
    size_bytes: int
    sha256: str
    message: str


@dataclass(frozen=True)
class ExactHwpxPublishResult:
    """COM reopen/save result for one approved exact-writer HWPX."""

    output_path: Path
    size_bytes: int
    sha256: str
    readback: StructureSummary


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class HwpDocumentFingerprint:
    """Verified source identity kept inside the trusted bridge boundary."""

    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


@dataclass
class _RollbackRecord:
    output_path: Path
    output_identity: _FileIdentity
    backup_path: Path
    backup_identity: _FileIdentity
    used: bool = False


class RegistryPreflightReader:
    """Read registration metadata only.  This class never imports a COM client."""

    def prog_id_registered(self, prog_id: str) -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, prog_id + r"\CLSID", 0, winreg.KEY_READ
            ) as key:
                clsid, _kind = winreg.QueryValueEx(key, None)
            if not isinstance(clsid, str) or not clsid.strip():
                return False
            for server_key in ("LocalServer32", "InprocServer32"):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CLASSES_ROOT,
                        r"CLSID\%s\%s" % (clsid.strip(), server_key),
                        0,
                        winreg.KEY_READ,
                    ):
                        return True
                except OSError:
                    continue
        except (ImportError, OSError):
            return False
        return False

    def module_registered(self, module_name: str) -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg

            relative = r"Software\HNC\HwpAutomation\Modules"
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, relative, 0, winreg.KEY_READ) as key:
                        value, _kind = winreg.QueryValueEx(key, module_name)
                    if isinstance(value, str) and value.strip():
                        return True
                except OSError:
                    continue
        except ImportError:
            return False
        return False


def registry_preflight(
    reader: Optional[HwpPreflightReader] = None,
) -> HwpPreflight:
    """Return registry availability without constructing or activating COM."""

    source = reader or RegistryPreflightReader()
    try:
        prog_id = bool(source.prog_id_registered(HWP_PROG_ID))
        module = bool(source.module_registered(SECURITY_MODULE))
    except Exception:
        return HwpPreflight(
            False,
            False,
            False,
            "한/글 자동화 등록 정보를 안전하게 확인하지 못했습니다.",
        )
    available = prog_id and module
    if available:
        message = "한/글 자동화와 파일 경로 확인 모듈이 등록되어 있습니다."
    elif not prog_id:
        message = "한/글 자동화가 등록되어 있지 않습니다."
    else:
        message = "한/글 파일 경로 확인 모듈이 등록되어 있지 않습니다."
    return HwpPreflight(available, prog_id, module, message)


class HwpStaWorker:
    """Serialize every COM call onto one initialized STA thread."""

    def __init__(self, pythoncom_module: Optional[object] = None) -> None:
        self._pythoncom_module = pythoncom_module
        self._jobs: "queue.Queue[object]" = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._startup_error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run, name="TrafficSuperVision-HWP-STA", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise HwpAutomationError("한/글 작업 스레드를 시작하지 못했습니다.")
        if self._startup_error is not None:
            raise HwpAutomationError("한/글 작업 스레드를 초기화하지 못했습니다.")

    def _run(self) -> None:
        initialized = False
        try:
            pythoncom = self._pythoncom_module
            if pythoncom is None:
                import pythoncom as loaded_pythoncom  # type: ignore

                pythoncom = loaded_pythoncom
            # CoInitialize establishes a single-threaded apartment by default.
            # Keeping the matching CoUninitialize in this thread is mandatory.
            pythoncom.CoInitialize()
            initialized = True
        except BaseException as error:
            self._startup_error = error
        finally:
            self._ready.set()

        if not initialized:
            return
        try:
            while True:
                item = self._jobs.get()
                if item is _STOP:
                    return
                function, result_queue = item  # type: ignore[misc]
                try:
                    result_queue.put((True, function()))
                except BaseException as error:
                    result_queue.put((False, error))
        finally:
            try:
                pythoncom.CoUninitialize()  # type: ignore[union-attr]
            except BaseException:
                pass

    def submit(self, function: Callable[[], Any]) -> Any:
        if self._closed or not self._thread.is_alive():
            raise HwpAutomationError("한/글 작업 스레드를 사용할 수 없습니다.")
        result_queue: "queue.Queue[Tuple[bool, object]]" = queue.Queue(maxsize=1)
        self._jobs.put((function, result_queue))
        succeeded, value = result_queue.get()
        if succeeded:
            return value
        raise value  # type: ignore[misc]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive():
            self._jobs.put(_STOP)
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "HwpStaWorker":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


_STOP = object()


class Win32HwpAdapter:
    """Small explicit allowlist around the HWP automation object."""

    def __init__(self, dispatch_ex: Optional[Callable[[str], object]] = None) -> None:
        if dispatch_ex is None:
            from win32com.client import DispatchEx  # type: ignore

            dispatch_ex = DispatchEx
        self._hwp = dispatch_ex(HWP_PROG_ID)
        self._opened = False

    def register_security_module(self) -> None:
        result = self._hwp.RegisterModule(SECURITY_MODULE_DLL, SECURITY_MODULE)
        if result is False:
            raise RuntimeError("security module registration failed")

    def open_document(self, path: Path) -> None:
        result = self._hwp.Open(os.fspath(path), "", "forceopen:true")
        if result is False:
            raise RuntimeError("document open failed")
        self._opened = True

    def read_hwpml(self, max_chars: int) -> Tuple[str, bool]:
        value = self._hwp.GetTextFile("HWPML2X", "")
        if not isinstance(value, str):
            raise RuntimeError("HWPML readback failed")
        return value[:max_chars], len(value) > max_chars

    def _move(self, target: EditTarget) -> None:
        if target.kind == "field":
            if self._hwp.MoveToField(target.name) is False:
                raise RuntimeError("field target not found")
        else:
            # The member name is fixed; plan data is never used as a COM member.
            if self._hwp.MoveToBookmark(target.name) is False:
                raise RuntimeError("bookmark target not found")

    def fill_text(self, target: EditTarget, text: str) -> None:
        self._move(target)
        if target.kind == "field":
            self._hwp.PutFieldText(target.name, text)
        else:
            self._hwp.InsertText(text)

    def fill_cell(self, target: EditTarget, text: str) -> None:
        self.fill_text(target, text)

    def add_table_row(
        self, target: EditTarget, cells: Sequence[Tuple[EditTarget, str]]
    ) -> None:
        self._move(target)
        if self._hwp.HAction.Run("TableAppendRow") is False:
            raise RuntimeError("row append failed")
        for cell_target, value in cells:
            self.fill_cell(cell_target, value)

    def insert_image(self, target: EditTarget, source_path: Path) -> None:
        self._move(target)
        result = self._hwp.InsertPicture(
            os.fspath(source_path), True, 0, False, False, 0, 0
        )
        if result is False:
            raise RuntimeError("image insertion failed")

    def insert_map(self, target: EditTarget, source_path: Path) -> None:
        self.insert_image(target, source_path)

    def save_as(self, output_path: Path) -> None:
        suffix = output_path.suffix.casefold()
        document_format = {
            ".hwp": "HWP",
            ".hwpx": "HWPX",
            ".pdf": "PDF",
        }.get(suffix)
        if document_format is None:
            raise RuntimeError("unsupported save format")
        if self._hwp.SaveAs(os.fspath(output_path), document_format, "") is False:
            raise RuntimeError("save failed")

    def close_document(self) -> None:
        if self._opened:
            self._hwp.Clear(1)
            self._opened = False

    def close(self) -> None:
        try:
            self.close_document()
        finally:
            self._hwp.Quit()


class HwpComService:
    def __init__(
        self,
        *,
        worker: Optional[HwpStaWorker] = None,
        adapter_factory: Callable[[], HwpAdapter] = Win32HwpAdapter,
        preflight_reader: Optional[HwpPreflightReader] = None,
    ) -> None:
        self._worker = worker or HwpStaWorker()
        self._owns_worker = worker is None
        self._adapter_factory = adapter_factory
        self._preflight_reader = preflight_reader
        self._rollback_records: Dict[str, _RollbackRecord] = {}
        self._records_lock = threading.Lock()

    def preflight(self) -> HwpPreflight:
        return registry_preflight(self._preflight_reader)

    def execute(
        self,
        template_path: Path,
        edit_plan: Mapping[str, Any],
        *,
        accept: bool = False,
        output_path: Optional[Path] = None,
        expected_fingerprint: Optional[HwpDocumentFingerprint] = None,
    ) -> HwpPreviewResult | HwpApplyResult:
        """Preview by default; mutate only after an explicit ``accept=True``."""

        template, initial_identity = _validate_existing_file(
            Path(template_path), DOCUMENT_SUFFIXES, "문서 양식"
        )
        initial_sha256 = _sha256(template, initial_identity)
        current_fingerprint = _document_fingerprint(
            template, initial_identity, initial_sha256
        )
        if (
            expected_fingerprint is not None
            and not _fingerprints_match(current_fingerprint, expected_fingerprint)
        ):
            raise HwpAutomationError(
                "미리보기 이후 문서 양식이 변경되어 적용을 중단했습니다."
            )
        operations = _validate_plan(edit_plan)
        _validate_operation_files(operations)

        if not accept:
            if output_path is not None:
                raise HwpAutomationError(
                    "미리보기에서는 출력 문서 경로를 지정할 수 없습니다."
                )
            return self._safe_submit(
                lambda: self._preview(
                    template,
                    initial_identity,
                    initial_sha256,
                    operations,
                )
            )

        if output_path is None:
            raise HwpAutomationError("적용할 새 출력 문서 경로가 필요합니다.")
        output = _validate_new_output(Path(output_path), template)
        return self._safe_submit(
            lambda: self._apply(
                template,
                initial_identity,
                initial_sha256,
                output,
                operations,
            )
        )

    def rollback(self, rollback_token: str) -> HwpRollbackResult:
        if not isinstance(rollback_token, str) or not re.fullmatch(
            r"[0-9a-f]{32}", rollback_token
        ):
            raise HwpAutomationError("유효한 롤백 식별자가 아닙니다.")
        with self._records_lock:
            record = self._rollback_records.get(rollback_token)
            if record is None or record.used:
                raise HwpAutomationError("사용 가능한 롤백 기록이 없습니다.")
        return self._safe_submit(lambda: self._rollback(rollback_token, record))

    def export_applied_hwpx_to_pdf(
        self,
        *,
        approval_id: str,
        source_hwpx: Path,
        destination_pdf: Path,
    ) -> Path:
        """Export an explicitly approved owned HWPX through an owned copy.

        The approval ledger remains outside this COM service.  The caller
        passes only its opaque proof after validating it.  This method still
        fails closed on an empty/malformed proof, changed source, existing
        destination, non-PDF output, or any COM failure.
        """

        if not isinstance(approval_id, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", approval_id
        ) is None:
            raise HwpAutomationError(
                "유효한 HWP 적용 승인 식별자가 필요합니다."
            )
        source, source_identity = _validate_existing_file(
            Path(source_hwpx),
            frozenset({".hwpx"}),
            "승인된 출력 문서",
        )
        source_sha256 = _sha256(source, source_identity)
        destination = _validate_new_pdf_output(
            Path(destination_pdf),
            source,
        )
        return self._safe_submit(
            lambda: self._export_pdf(
                source,
                source_identity,
                source_sha256,
                destination,
            )
        )

    def publish_approved_exact_hwpx(
        self,
        *,
        approval_id: str,
        source_hwpx: Path,
        destination_hwpx: Path,
    ) -> ExactHwpxPublishResult:
        """Reopen/save an approved exact HWPX and atomically publish it."""

        if not isinstance(approval_id, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", approval_id
        ) is None:
            raise HwpAutomationError(
                "유효한 HWP 적용 승인 식별자가 필요합니다."
            )
        source, source_identity = _validate_existing_file(
            Path(source_hwpx),
            frozenset({".hwpx"}),
            "승인된 정확 HWPX 미리보기",
        )
        source_sha256 = _sha256(source, source_identity)
        destination = _validate_new_exact_output(
            Path(destination_hwpx),
            source,
        )
        return self._safe_submit(
            lambda: self._publish_exact_hwpx(
                source,
                source_identity,
                source_sha256,
                destination,
            )
        )

    def close(self) -> None:
        if self._owns_worker:
            self._worker.close()

    def _safe_submit(self, function: Callable[[], Any]) -> Any:
        try:
            return self._worker.submit(function)
        except HwpAutomationError:
            raise
        except BaseException:
            raise HwpAutomationError(
                "한/글 문서 작업을 안전하게 완료하지 못했습니다."
            ) from None

    def _preview(
        self,
        template: Path,
        initial_identity: _FileIdentity,
        initial_sha256: str,
        operations: Tuple[ValidatedOperation, ...],
    ) -> HwpPreviewResult:
        _assert_identity(template, initial_identity)
        _validate_operation_files(operations)
        working = template.with_name(
            ".%s.trafficvisson-preview-%s%s"
            % (template.stem, uuid4().hex, template.suffix)
        )
        adapter: Optional[HwpAdapter] = None
        try:
            # Hangul never receives the user-owned template path.  Even a
            # read-only preview is performed against an owned disposable copy
            # because third-party COM automation may update document metadata.
            _exclusive_copy(template, working)
            _assert_identity(template, initial_identity)
            if not hmac.compare_digest(
                _sha256(template, initial_identity), initial_sha256
            ):
                raise HwpAutomationError(
                    "미리보기 중 문서 양식이 변경되어 승인을 발급하지 않았습니다."
                )
            adapter = self._adapter_factory()
            adapter.register_security_module()
            adapter.open_document(working)
            hwpml, truncated = adapter.read_hwpml(MAX_HWPML_CHARS)
            summary = _summarize_hwpml(hwpml, truncated)
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                finally:
                    _unlink_owned_unverified(working)
            else:
                _unlink_owned_unverified(working)
        _assert_identity(template, initial_identity)
        final_sha256 = _sha256(template, initial_identity)
        if not hmac.compare_digest(final_sha256, initial_sha256):
            raise HwpAutomationError(
                "미리보기 중 문서 양식이 변경되어 승인을 발급하지 않았습니다."
            )
        return HwpPreviewResult(
            mode="preview",
            operation_count=len(operations),
            operation_types=_operation_counts(operations),
            structure=summary,
            message="문서 구조와 편집 위치를 확인했습니다. 원본은 변경되지 않았습니다.",
            document_fingerprint=_document_fingerprint(
                template, initial_identity, final_sha256
            ),
        )

    def _apply(
        self,
        template: Path,
        initial_identity: _FileIdentity,
        initial_sha256: str,
        output: Path,
        operations: Tuple[ValidatedOperation, ...],
    ) -> HwpApplyResult:
        _assert_identity(template, initial_identity)
        if not hmac.compare_digest(
            _sha256(template, initial_identity), initial_sha256
        ):
            raise HwpAutomationError(
                "미리보기 이후 문서 양식이 변경되어 적용을 중단했습니다."
            )
        _assert_output_absent(output)
        _validate_operation_files(operations)
        working = template.with_name(
            ".%s.trafficvisson-working-%s%s"
            % (template.stem, uuid4().hex, template.suffix)
        )
        backup = template.with_name(
            ".%s.trafficvisson-backup-%s%s"
            % (template.stem, uuid4().hex, template.suffix)
        )
        staging = output.with_name(
            ".%s.trafficvisson-output-%s%s"
            % (output.stem, uuid4().hex, output.suffix)
        )
        recovery_journal = template.with_name(
            ".%s.trafficvisson-recovery-%s.json"
            % (template.stem, uuid4().hex)
        )
        adapter: Optional[HwpAdapter] = None
        published_output = False
        published_identity: Optional[_FileIdentity] = None
        phase = "copy_working"
        try:
            # COM is allowed to touch only this owned working copy.  The
            # pristine rollback copy is created later, after the working copy
            # is released, so large templates do not require three full
            # transient copies at once.
            _exclusive_copy(template, working)
            _assert_identity(template, initial_identity)
            if not hmac.compare_digest(
                _sha256(template, initial_identity), initial_sha256
            ):
                raise HwpAutomationError(
                    "미리보기 이후 문서 양식이 변경되어 적용을 중단했습니다."
                )
            phase = "edit_working"
            adapter = self._adapter_factory()
            adapter.register_security_module()
            adapter.open_document(working)
            for operation in operations:
                _apply_operation(adapter, operation)
            phase = "save_staging"
            adapter.save_as(staging)
            adapter.close_document()

            phase = "readback_staging"
            staging_identity = _identity(staging)
            if staging_identity.size <= 0:
                raise RuntimeError("empty output")
            adapter.open_document(staging)
            hwpml, truncated = adapter.read_hwpml(MAX_HWPML_CHARS)
            readback = _summarize_hwpml(hwpml, truncated)
            adapter.close_document()
            staging_identity = _identity(staging)
            digest = _sha256(staging, staging_identity)
            _assert_identity(template, initial_identity)
            if not hmac.compare_digest(
                _sha256(template, initial_identity), initial_sha256
            ):
                raise HwpAutomationError(
                    "적용 중 원본 문서가 변경되어 출력을 게시하지 않았습니다."
                )
            # Release COM before removing its working copy or creating the
            # rollback snapshot.  This keeps peak disk use bounded to two
            # document-sized owned files.
            adapter.close()
            adapter = None
            phase = "prepare_rollback"
            _unlink_owned_unverified(working)
            if _regular_file_exists(working):
                raise RuntimeError("working copy cleanup failed")
            _exclusive_copy(template, backup)
            backup_identity = _identity(backup)
            if not hmac.compare_digest(
                _sha256(backup, backup_identity), initial_sha256
            ):
                raise RuntimeError("rollback copy verification failed")
            _assert_identity(template, initial_identity)
            if not hmac.compare_digest(
                _sha256(template, initial_identity), initial_sha256
            ):
                raise HwpAutomationError(
                    "적용 중 원본 문서가 변경되어 출력을 게시하지 않았습니다."
                )
            _assert_output_absent(output)
            # os.rename is intentionally used instead of os.replace: it must
            # fail rather than overwrite if another actor won the destination.
            phase = "publish_output"
            os.rename(staging, output)
            published_output = True
            output_identity = _identity(output)
            published_identity = output_identity
            if (
                output_identity.size != staging_identity.size
                or _sha256(output, output_identity) != digest
            ):
                raise RuntimeError("published output verification failed")

            token = uuid4().hex
            with self._records_lock:
                self._rollback_records[token] = _RollbackRecord(
                    output, output_identity, backup, backup_identity
                )
            return HwpApplyResult(
                mode="applied",
                rollback_token=token,
                output_path=output,
                backup_path=backup,
                size_bytes=output_identity.size,
                sha256=digest,
                readback=readback,
                message="새 문서를 저장하고 다시 열어 검증했습니다.",
            )
        except BaseException:
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass
                adapter = None
            published_output_removed = False
            if published_output and published_identity is not None:
                published_output_removed = _unlink_if_identity(
                    output, published_identity
                )
            # Retain exactly one useful owned document plus a tiny journal.
            # This avoids deleting the only recovery evidence while bounding
            # failure-time disk growth for very large HWP/HWPX templates.
            retained = _first_regular_file((backup, working, staging))
            for candidate in (backup, working, staging):
                if retained is None or candidate != retained:
                    _unlink_owned_unverified(candidate)
            if retained is not None:
                _write_recovery_journal(
                    recovery_journal,
                    template=template,
                    output=output,
                    retained=retained,
                    source_sha256=initial_sha256,
                    phase=phase,
                    published_output_removed=published_output_removed,
                )
            raise
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass

    def _rollback(
        self, token: str, record: _RollbackRecord
    ) -> HwpRollbackResult:
        _assert_identity(record.output_path, record.output_identity)
        _assert_identity(record.backup_path, record.backup_identity)
        temporary = record.output_path.with_name(
            ".%s.trafficvisson-rollback-%s.tmp"
            % (record.output_path.name, uuid4().hex)
        )
        try:
            _exclusive_copy(record.backup_path, temporary)
            os.replace(temporary, record.output_path)
            restored_identity = _identity(record.output_path)
            digest = _sha256(record.output_path, restored_identity)
            backup_digest = _sha256(record.backup_path, record.backup_identity)
            if digest != backup_digest:
                raise RuntimeError("rollback verification failed")
            _assert_identity(record.backup_path, record.backup_identity)
            record.backup_path.unlink()
            with self._records_lock:
                current = self._rollback_records.get(token)
                if current is record:
                    current.used = True
            return HwpRollbackResult(
                rolled_back=True,
                output_path=record.output_path,
                size_bytes=restored_identity.size,
                sha256=digest,
                message="TrafficSuperVision이 만든 출력 문서를 원본 양식 상태로 되돌렸습니다.",
            )
        finally:
            _unlink_owned_unverified(temporary)

    def _publish_exact_hwpx(
        self,
        source: Path,
        source_identity: _FileIdentity,
        source_sha256: str,
        destination: Path,
    ) -> ExactHwpxPublishResult:
        _assert_identity(source, source_identity)
        if not hmac.compare_digest(
            _sha256(source, source_identity),
            source_sha256,
        ):
            raise HwpAutomationError(
                "승인된 정확 HWPX 미리보기가 변경되었습니다."
            )
        _assert_output_absent(destination)
        working = source.with_name(
            ".%s.trafficvisson-exact-working-%s%s"
            % (source.stem, uuid4().hex, source.suffix)
        )
        staging = destination.with_name(
            ".%s.trafficvisson-exact-output-%s%s"
            % (destination.stem, uuid4().hex, destination.suffix)
        )
        adapter: Optional[HwpAdapter] = None
        published_identity: Optional[_FileIdentity] = None
        try:
            _exclusive_copy(source, working)
            adapter = self._adapter_factory()
            adapter.register_security_module()
            adapter.open_document(working)
            adapter.save_as(staging)
            adapter.close_document()
            adapter.open_document(staging)
            hwpml, truncated = adapter.read_hwpml(MAX_HWPML_CHARS)
            readback = _summarize_hwpml(hwpml, truncated)
            adapter.close_document()
            adapter.close()
            adapter = None

            staging_identity = _identity(staging)
            if staging_identity.size <= 0:
                raise RuntimeError("empty exact HWPX output")
            digest = _sha256(staging, staging_identity)
            _assert_identity(source, source_identity)
            if not hmac.compare_digest(
                _sha256(source, source_identity),
                source_sha256,
            ):
                raise HwpAutomationError(
                    "COM 적용 중 정확 HWPX 미리보기가 변경되었습니다."
                )
            _assert_output_absent(destination)
            os.rename(staging, destination)
            published_identity = _identity(destination)
            if (
                published_identity.size != staging_identity.size
                or _sha256(destination, published_identity) != digest
            ):
                raise RuntimeError(
                    "published exact HWPX verification failed"
                )
            return ExactHwpxPublishResult(
                output_path=destination,
                size_bytes=published_identity.size,
                sha256=digest,
                readback=readback,
            )
        except BaseException:
            if published_identity is not None:
                _unlink_if_identity(destination, published_identity)
            raise
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass
            _unlink_owned_unverified(working)
            _unlink_owned_unverified(staging)

    def _export_pdf(
        self,
        source: Path,
        source_identity: _FileIdentity,
        source_sha256: str,
        destination: Path,
    ) -> Path:
        _assert_identity(source, source_identity)
        if not hmac.compare_digest(
            _sha256(source, source_identity), source_sha256
        ):
            raise HwpAutomationError(
                "승인된 출력 문서가 변경되어 PDF 내보내기를 중단했습니다."
            )
        _assert_output_absent(destination)
        working = source.with_name(
            ".%s.trafficvisson-pdf-source-%s%s"
            % (source.stem, uuid4().hex, source.suffix)
        )
        staging = destination.with_name(
            ".%s.trafficvisson-pdf-output-%s.pdf"
            % (destination.stem, uuid4().hex)
        )
        adapter: Optional[HwpAdapter] = None
        published_identity: Optional[_FileIdentity] = None
        try:
            _exclusive_copy(source, working)
            _assert_identity(source, source_identity)
            if not hmac.compare_digest(
                _sha256(source, source_identity), source_sha256
            ):
                raise HwpAutomationError(
                    "승인된 출력 문서가 변경되어 PDF 내보내기를 중단했습니다."
                )
            adapter = self._adapter_factory()
            adapter.register_security_module()
            adapter.open_document(working)
            adapter.save_as(staging)
            adapter.close_document()

            staging_identity = _validate_pdf_file(staging)
            staging_sha256 = _sha256(staging, staging_identity)
            _assert_identity(source, source_identity)
            if not hmac.compare_digest(
                _sha256(source, source_identity), source_sha256
            ):
                raise HwpAutomationError(
                    "PDF 내보내기 중 승인된 출력 문서가 변경되었습니다."
                )
            adapter.close()
            adapter = None
            _unlink_owned_unverified(working)
            if _regular_file_exists(working):
                raise RuntimeError("PDF working copy cleanup failed")

            _assert_output_absent(destination)
            os.rename(staging, destination)
            published_identity = _validate_pdf_file(destination)
            if (
                published_identity.size != staging_identity.size
                or not hmac.compare_digest(
                    _sha256(destination, published_identity),
                    staging_sha256,
                )
            ):
                raise RuntimeError("published PDF verification failed")
            return destination
        except BaseException:
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass
                adapter = None
            if published_identity is not None:
                _unlink_if_identity(destination, published_identity)
            raise
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass
            _unlink_owned_unverified(working)
            _unlink_owned_unverified(staging)


def _validate_plan(
    edit_plan: Mapping[str, Any],
) -> Tuple[ValidatedOperation, ...]:
    if not isinstance(edit_plan, Mapping):
        raise HwpAutomationError("문서 편집 계획의 형식이 올바르지 않습니다.")
    if edit_plan.get("version") != PLAN_VERSION:
        raise HwpAutomationError("지원하지 않는 문서 편집 계획 버전입니다.")
    if set(edit_plan) - {"version", "operations"}:
        raise HwpAutomationError("문서 편집 계획에 허용되지 않은 항목이 있습니다.")
    raw_operations = edit_plan.get("operations")
    if isinstance(raw_operations, (str, bytes)) or not isinstance(
        raw_operations, Sequence
    ):
        raise HwpAutomationError("문서 편집 작업 목록이 필요합니다.")
    if len(raw_operations) > MAX_OPERATIONS:
        raise HwpAutomationError("문서 편집 작업 수가 허용 범위를 초과했습니다.")

    operations = []
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise HwpAutomationError("문서 편집 작업의 형식이 올바르지 않습니다.")
        op = raw.get("op")
        if op not in ALLOWED_OPERATIONS:
            raise HwpAutomationError("허용되지 않은 문서 편집 작업입니다.")
        allowed_keys = (
            {"op", "target", "text"}
            if op in {"fill_text", "fill_cell"}
            else {"op", "target", "cells"}
            if op == "add_table_row"
            else {"op", "target", "source_path"}
        )
        if set(raw) - allowed_keys:
            raise HwpAutomationError("문서 편집 작업에 허용되지 않은 항목이 있습니다.")
        target = _validate_target(raw.get("target"))
        if op in {"fill_text", "fill_cell"}:
            operations.append(
                ValidatedOperation(op, target, text=_validate_text(raw.get("text")))
            )
        elif op == "add_table_row":
            raw_cells = raw.get("cells", ())
            if isinstance(raw_cells, (str, bytes)) or not isinstance(
                raw_cells, Sequence
            ):
                raise HwpAutomationError("표 행의 셀 목록이 올바르지 않습니다.")
            if len(raw_cells) > 100:
                raise HwpAutomationError("표 행의 셀 수가 허용 범위를 초과했습니다.")
            cells = []
            for raw_cell in raw_cells:
                if not isinstance(raw_cell, Mapping):
                    raise HwpAutomationError("표 셀 작업의 형식이 올바르지 않습니다.")
                if set(raw_cell) - {"target", "text"}:
                    raise HwpAutomationError(
                        "표 셀 작업에 허용되지 않은 항목이 있습니다."
                    )
                cells.append(
                    (
                        _validate_target(raw_cell.get("target")),
                        _validate_text(raw_cell.get("text")),
                    )
                )
            operations.append(ValidatedOperation(op, target, cells=tuple(cells)))
        else:
            raw_path = raw.get("source_path")
            if not isinstance(raw_path, (str, os.PathLike)):
                raise HwpAutomationError("삽입할 이미지가 필요합니다.")
            source, source_identity = _validate_existing_file(
                Path(raw_path), IMAGE_SUFFIXES, "삽입 이미지"
            )
            operations.append(
                ValidatedOperation(
                    op,
                    target,
                    source_path=source,
                    source_identity=source_identity,
                )
            )
    return tuple(operations)


def _validate_target(raw: object) -> EditTarget:
    if not isinstance(raw, Mapping):
        raise HwpAutomationError("문서 편집 위치가 필요합니다.")
    if set(raw) - {"kind", "name"}:
        raise HwpAutomationError("문서 편집 위치에 허용되지 않은 항목이 있습니다.")
    kind = raw.get("kind")
    name = raw.get("name")
    if kind not in {"field", "bookmark"}:
        raise HwpAutomationError("허용되지 않은 문서 편집 위치입니다.")
    if not isinstance(name, str) or not re.fullmatch(r"[\w가-힣 .:()\-\[\]]{1,128}", name):
        raise HwpAutomationError("문서 편집 위치 이름이 올바르지 않습니다.")
    return EditTarget(kind, name)


def _validate_text(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS or "\x00" in value:
        raise HwpAutomationError("삽입할 문장의 형식이 올바르지 않습니다.")
    return value


def _validate_operation_files(operations: Sequence[ValidatedOperation]) -> None:
    for operation in operations:
        if operation.source_path is not None:
            source, identity = _validate_existing_file(
                operation.source_path, IMAGE_SUFFIXES, "삽입 이미지"
            )
            if (
                source != operation.source_path
                or operation.source_identity is None
                or identity != operation.source_identity
            ):
                raise HwpAutomationError("작업 중 삽입 이미지가 변경되어 중단했습니다.")


def _apply_operation(adapter: HwpAdapter, operation: ValidatedOperation) -> None:
    if operation.op == "fill_text":
        adapter.fill_text(operation.target, operation.text or "")
    elif operation.op == "fill_cell":
        adapter.fill_cell(operation.target, operation.text or "")
    elif operation.op == "add_table_row":
        adapter.add_table_row(operation.target, operation.cells)
    elif operation.op == "insert_image":
        adapter.insert_image(operation.target, operation.source_path)  # type: ignore[arg-type]
    elif operation.op == "insert_map":
        adapter.insert_map(operation.target, operation.source_path)  # type: ignore[arg-type]
    else:
        raise RuntimeError("unreachable operation")


def _operation_counts(
    operations: Sequence[ValidatedOperation],
) -> Mapping[str, int]:
    counts: Dict[str, int] = {}
    for operation in operations:
        counts[operation.op] = counts.get(operation.op, 0) + 1
    return dict(sorted(counts.items()))


def _summarize_hwpml(hwpml: str, truncated: bool) -> StructureSummary:
    if not isinstance(hwpml, str):
        raise RuntimeError("invalid HWPML")
    sample = hwpml[:MAX_HWPML_CHARS]
    tag = lambda name: len(
        re.findall(r"<(?:\w+:)?%s(?=[\s/>])" % name, sample, re.I)
    )
    fields = _bounded_names(
        re.findall(
            r"<(?:\w+:)?FIELD\b[^>]*\b(?:name|Name)=[\"']([^\"']+)[\"']",
            sample,
            re.I,
        )
    )
    bookmarks = _bounded_names(
        re.findall(
            r"<(?:\w+:)?BOOKMARK\b[^>]*\b(?:name|Name)=[\"']([^\"']+)[\"']",
            sample,
            re.I,
        )
    )
    return StructureSummary(
        observed_characters=len(sample),
        truncated=truncated or len(hwpml) > MAX_HWPML_CHARS,
        paragraph_count=tag("P"),
        table_count=tag("TABLE"),
        picture_count=tag("PICTURE"),
        field_names=fields,
        bookmark_names=bookmarks,
    )


def _bounded_names(values: Sequence[str]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:128]
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
        if len(result) >= MAX_STRUCTURE_NAMES:
            break
    return tuple(result)


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_existing_file(
    path: Path, suffixes: frozenset[str], label: str
) -> Tuple[Path, _FileIdentity]:
    raw = os.fspath(path)
    if not raw or "\x00" in raw or not path.is_absolute():
        raise HwpAutomationError("%s 경로가 올바르지 않습니다." % label)
    if path.suffix.casefold() not in suffixes:
        raise HwpAutomationError("%s 형식이 지원되지 않습니다." % label)
    absolute = Path(os.path.abspath(raw))
    _assert_no_link_components(absolute)
    identity = _identity(absolute)
    return absolute, identity


def _validate_new_output(path: Path, template: Path) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw or not path.is_absolute():
        raise HwpAutomationError("출력 문서 경로가 올바르지 않습니다.")
    absolute = Path(os.path.abspath(raw))
    if absolute.suffix.casefold() not in DOCUMENT_SUFFIXES:
        raise HwpAutomationError("출력 문서 형식이 지원되지 않습니다.")
    if absolute == template:
        raise HwpAutomationError("원본 문서를 덮어쓸 수 없습니다.")
    if absolute.parent != template.parent:
        raise HwpAutomationError("출력 문서는 원본 양식과 같은 폴더에 저장해야 합니다.")
    _assert_no_link_components(absolute.parent)
    _assert_output_absent(absolute)
    return absolute


def _validate_new_exact_output(path: Path, source: Path) -> Path:
    """Validate a server-owned exact-report publication destination."""

    raw = os.fspath(path)
    if not raw or "\x00" in raw or not path.is_absolute():
        raise HwpAutomationError("출력 문서 경로가 올바르지 않습니다.")
    absolute = Path(os.path.abspath(raw))
    if absolute.suffix.casefold() != ".hwpx":
        raise HwpAutomationError("정확 보고서 출력 형식은 HWPX여야 합니다.")
    if absolute == source:
        raise HwpAutomationError("원본 문서를 덮어쓸 수 없습니다.")
    _assert_no_link_components(absolute.parent)
    try:
        parent = os.stat(absolute.parent, follow_symlinks=False)
    except OSError as error:
        raise HwpAutomationError(
            "출력 문서 폴더를 안전하게 확인하지 못했습니다."
        ) from error
    if not stat_module.S_ISDIR(parent.st_mode) or _is_reparse(parent):
        raise HwpAutomationError("출력 문서 폴더가 올바르지 않습니다.")
    _assert_output_absent(absolute)
    return absolute


def _validate_new_pdf_output(path: Path, source: Path) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw or not path.is_absolute():
        raise HwpAutomationError("PDF 출력 경로가 올바르지 않습니다.")
    absolute = Path(os.path.abspath(raw))
    if absolute.suffix.casefold() != ".pdf" or absolute == source:
        raise HwpAutomationError("PDF 출력 형식이 지원되지 않습니다.")
    _assert_no_link_components(absolute.parent)
    try:
        parent = os.stat(absolute.parent, follow_symlinks=False)
    except OSError as error:
        raise HwpAutomationError(
            "PDF 출력 폴더를 안전하게 확인하지 못했습니다."
        ) from error
    if not stat_module.S_ISDIR(parent.st_mode) or _is_reparse(parent):
        raise HwpAutomationError("PDF 출력 폴더가 올바르지 않습니다.")
    _assert_output_absent(absolute)
    return absolute


def _validate_pdf_file(path: Path) -> _FileIdentity:
    identity = _identity(path)
    if (
        identity.size <= 5
        or identity.size > MAX_EXPORTED_PDF_BYTES
    ):
        raise RuntimeError("exported PDF size is invalid")
    with path.open("rb") as source:
        magic = source.read(5)
    _assert_identity(path, identity)
    if magic != b"%PDF-":
        raise RuntimeError("exported file is not a PDF")
    return identity


def _assert_no_link_components(path: Path) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            component = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise HwpAutomationError("파일 경로를 안전하게 확인하지 못했습니다.") from error
        if stat_module.S_ISLNK(component.st_mode) or _is_reparse(component):
            raise HwpAutomationError("링크 또는 재분석 지점 경로는 사용할 수 없습니다.")


def _identity(path: Path) -> _FileIdentity:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise HwpAutomationError("파일을 안전하게 확인하지 못했습니다.") from error
    if not stat_module.S_ISREG(value.st_mode) or _is_reparse(value):
        raise HwpAutomationError("일반 파일만 사용할 수 있습니다.")
    return _FileIdentity(
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _assert_identity(path: Path, expected: _FileIdentity) -> None:
    if _identity(path) != expected:
        raise HwpAutomationError("작업 중 파일이 변경되어 중단했습니다.")


def _document_fingerprint(
    path: Path, identity: _FileIdentity, sha256: str
) -> HwpDocumentFingerprint:
    return HwpDocumentFingerprint(
        path=path,
        device=identity.device,
        inode=identity.inode,
        size=identity.size,
        modified_ns=identity.modified_ns,
        sha256=sha256,
    )


def fingerprint_document(path: Path) -> HwpDocumentFingerprint:
    """Validate and hash an HWP/HWPX source without launching Hangul."""

    verified_path, identity = _validate_existing_file(
        Path(path), DOCUMENT_SUFFIXES, "문서 양식"
    )
    return _document_fingerprint(
        verified_path,
        identity,
        _sha256(verified_path, identity),
    )


def _fingerprints_match(
    actual: HwpDocumentFingerprint, expected: HwpDocumentFingerprint
) -> bool:
    return (
        actual.path == expected.path
        and actual.device == expected.device
        and actual.inode == expected.inode
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and hmac.compare_digest(actual.sha256, expected.sha256)
    )


def _assert_output_absent(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise HwpAutomationError("출력 문서 경로를 확인하지 못했습니다.") from error
    raise HwpAutomationError("출력 문서가 이미 존재합니다.")


def _exclusive_copy(source: Path, destination: Path) -> None:
    _assert_output_absent(destination)
    try:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, COPY_CHUNK_BYTES)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)
    except BaseException:
        _unlink_owned_unverified(destination)
        raise


def _sha256(path: Path, expected: _FileIdentity) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    _assert_identity(path, expected)
    return digest.hexdigest()


def _unlink_owned_unverified(path: Path) -> None:
    try:
        value = os.lstat(path)
        if stat_module.S_ISREG(value.st_mode) and not _is_reparse(value):
            path.unlink()
    except OSError:
        pass


def _unlink_if_identity(path: Path, expected: _FileIdentity) -> bool:
    try:
        if _identity(path) == expected:
            path.unlink()
            return True
    except (OSError, HwpAutomationError):
        pass
    return False


def _regular_file_exists(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except OSError:
        return False
    return stat_module.S_ISREG(value.st_mode) and not _is_reparse(value)


def _first_regular_file(paths: Sequence[Path]) -> Optional[Path]:
    for path in paths:
        if _regular_file_exists(path):
            return path
    return None


def _write_recovery_journal(
    journal: Path,
    *,
    template: Path,
    output: Path,
    retained: Path,
    source_sha256: str,
    phase: str,
    published_output_removed: bool,
) -> None:
    payload = {
        "schema_version": "trafficvisson-hwp-recovery-v1",
        "phase": phase,
        "source_file_name": template.name,
        "intended_output_file_name": output.name,
        "retained_artifact_file_name": retained.name,
        "source_sha256": source_sha256,
        "published_output_removed": published_output_removed,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    try:
        with journal.open("xb") as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        _unlink_owned_unverified(journal)


__all__ = [
    "ALLOWED_OPERATIONS",
    "EditTarget",
    "ExactHwpxPublishResult",
    "HwpAdapter",
    "HwpApplyResult",
    "HwpAutomationError",
    "HwpComService",
    "HwpDocumentFingerprint",
    "HwpPreflight",
    "HwpPreviewResult",
    "HwpRollbackResult",
    "HwpStaWorker",
    "PLAN_VERSION",
    "RegistryPreflightReader",
    "StructureSummary",
    "Win32HwpAdapter",
    "fingerprint_document",
    "registry_preflight",
]
