"""Bounded provenance audit for the official PDF render of sample HWPX.

The HWPX package remains the only source for editable structure, table/cell
addresses, photo slots, captions, and weak photo cases.  Its same-quarter PDF
is an official-render companion used only to cross-check page existence and
renderability.  PDF images are never extracted into additional cases.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


PDF_COMPANION_SCHEMA = "trafficvisson.pdf-companion-audit.v1"
PDF_COMPANION_ROLE = "official_render_cross_validation_only"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PDF_HEADER = b"%PDF-"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_READ_CHUNK_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class PdfCompanionDefinition:
    quarter: int
    document_name: str
    expected_pdf_sha256: str
    expected_page_count: int = 48

    def __post_init__(self) -> None:
        if self.quarter not in {3, 4}:
            raise ValueError("PDF companion quarter is invalid")
        if (
            not isinstance(self.document_name, str)
            or Path(self.document_name).name != self.document_name
            or not self.document_name.casefold().endswith(".pdf")
        ):
            raise ValueError("PDF companion document name is invalid")
        if (
            not isinstance(self.expected_pdf_sha256, str)
            or _SHA256.fullmatch(self.expected_pdf_sha256) is None
        ):
            raise ValueError("PDF companion SHA-256 is invalid")
        if (
            isinstance(self.expected_page_count, bool)
            or not isinstance(self.expected_page_count, int)
            or not 1 <= self.expected_page_count <= 256
        ):
            raise ValueError("PDF companion page count is invalid")


DEFAULT_PDF_COMPANIONS: Tuple[PdfCompanionDefinition, ...] = (
    PdfCompanionDefinition(
        quarter=3,
        document_name="3분기 점검 내용 정리.pdf",
        expected_pdf_sha256=(
            "9a4b3f093b9fd8c320c55b1b80ac29a359a32c458f814ef6f3bc1cb2763c1e89"
        ),
    ),
    PdfCompanionDefinition(
        quarter=4,
        document_name="4분기 점검 내용 정리.pdf",
        expected_pdf_sha256=(
            "0b705c9f1961f0ca5bde1cb34cffc4f21893ab024a9aee001fe4c37fa2267856"
        ),
    ),
)


@dataclass(frozen=True)
class PdfCompanionLimits:
    max_pdf_bytes: int = 64 * 1024 * 1024
    max_pages: int = 128
    max_page_text_chars: int = 2 * 1024 * 1024
    max_total_text_chars: int = 16 * 1024 * 1024
    render_dpi: int = 36
    max_render_pages: int = 3
    max_render_page_bytes: int = 16 * 1024 * 1024
    max_render_total_bytes: int = 48 * 1024 * 1024
    render_timeout_seconds: float = 60.0
    max_process_output_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        integer_ranges = (
            ("max_pdf_bytes", self.max_pdf_bytes, 1_024, 512 * 1024**2),
            ("max_pages", self.max_pages, 1, 1_024),
            (
                "max_page_text_chars",
                self.max_page_text_chars,
                1,
                16 * 1024 * 1024,
            ),
            (
                "max_total_text_chars",
                self.max_total_text_chars,
                1,
                128 * 1024 * 1024,
            ),
            ("render_dpi", self.render_dpi, 18, 300),
            ("max_render_pages", self.max_render_pages, 1, 16),
            (
                "max_render_page_bytes",
                self.max_render_page_bytes,
                1_024,
                128 * 1024 * 1024,
            ),
            (
                "max_render_total_bytes",
                self.max_render_total_bytes,
                1_024,
                512 * 1024 * 1024,
            ),
            (
                "max_process_output_bytes",
                self.max_process_output_bytes,
                1_024,
                16 * 1024 * 1024,
            ),
        )
        for name, value, minimum, maximum in integer_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if (
            isinstance(self.render_timeout_seconds, bool)
            or not isinstance(self.render_timeout_seconds, (int, float))
            or not math.isfinite(float(self.render_timeout_seconds))
            or not 1 <= float(self.render_timeout_seconds) <= 600
        ):
            raise ValueError(
                "render_timeout_seconds is outside the supported range"
            )
        if self.max_render_page_bytes > self.max_render_total_bytes:
            raise ValueError(
                "render total bytes must fit at least one page"
            )


@dataclass(frozen=True)
class _PdfIdentity:
    size_bytes: int
    modified_time_ns: int
    device: int
    inode: int


class PdfCompanionAuditError(RuntimeError):
    """Stable, path-free PDF companion failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(path: Path, maximum_bytes: int) -> _PdfIdentity:
    try:
        metadata = path.stat()
    except OSError as error:
        raise PdfCompanionAuditError(
            "pdf_unavailable",
            "The PDF companion is unavailable",
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
    ):
        raise PdfCompanionAuditError(
            "pdf_invalid_file",
            "The PDF companion is not a bounded regular file",
        )
    return _PdfIdentity(
        size_bytes=int(metadata.st_size),
        modified_time_ns=int(metadata.st_mtime_ns),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
    )


def _hash_pdf(
    path: Path,
    expected_identity: _PdfIdentity,
    maximum_bytes: int,
) -> tuple[str, bytes, bytes]:
    digest = hashlib.sha256()
    total = 0
    header = b""
    tail = bytearray()
    try:
        with path.open("rb") as stream:
            while block := stream.read(_READ_CHUNK_BYTES):
                if not header:
                    header = block[:16]
                total += len(block)
                if total > maximum_bytes:
                    raise PdfCompanionAuditError(
                        "pdf_size_limit",
                        "The PDF companion exceeds its size bound",
                    )
                digest.update(block)
                tail.extend(block)
                if len(tail) > 4_096:
                    del tail[:-4_096]
    except PdfCompanionAuditError:
        raise
    except OSError as error:
        raise PdfCompanionAuditError(
            "pdf_unavailable",
            "The PDF companion could not be read",
        ) from error
    if (
        total != expected_identity.size_bytes
        or _identity(path, maximum_bytes) != expected_identity
    ):
        raise PdfCompanionAuditError(
            "pdf_source_changed",
            "The PDF companion changed while it was hashed",
        )
    if not header.startswith(_PDF_HEADER) or b"%%EOF" not in bytes(tail):
        raise PdfCompanionAuditError(
            "pdf_malformed",
            "The PDF companion failed its bounded magic check",
        )
    return digest.hexdigest(), header, bytes(tail)


def _normalized_page_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PdfCompanionAuditError(
            "pdf_text_invalid",
            "The PDF text extractor returned an invalid value",
        )
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    return "\n".join(
        line.rstrip() for line in normalized.split("\n")
    ).strip()


def _pypdf_text_audit(
    path: Path,
    *,
    definition: PdfCompanionDefinition,
    limits: PdfCompanionLimits,
) -> tuple[list[dict[str, Any]], str, int, str]:
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as error:
        raise PdfCompanionAuditError(
            "pdf_parser_unavailable",
            "The bounded PDF parser is unavailable",
        ) from error
    try:
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            raise PdfCompanionAuditError(
                "pdf_encrypted",
                "Encrypted PDF companions are not supported",
            )
        page_count = len(reader.pages)
        if (
            not 1 <= page_count <= limits.max_pages
            or page_count != definition.expected_page_count
        ):
            raise PdfCompanionAuditError(
                "pdf_page_count_mismatch",
                "The PDF companion page count does not match its source",
            )
        page_rows = []
        normalized_pages = []
        total_chars = 0
        for page_index, page in enumerate(reader.pages):
            text = _normalized_page_text(page.extract_text())
            if len(text) > limits.max_page_text_chars:
                raise PdfCompanionAuditError(
                    "pdf_text_limit",
                    "A PDF page exceeds its text bound",
                )
            total_chars += len(text)
            if total_chars > limits.max_total_text_chars:
                raise PdfCompanionAuditError(
                    "pdf_text_limit",
                    "The PDF companion exceeds its total text bound",
                )
            encoded = text.encode("utf-8")
            page_rows.append(
                {
                    "page_number": page_index + 1,
                    "text_sha256": hashlib.sha256(encoded).hexdigest(),
                    "text_char_count": len(text),
                    "text_extraction_status": "extracted",
                }
            )
            normalized_pages.append(text)
    except PdfCompanionAuditError:
        raise
    except Exception as error:
        raise PdfCompanionAuditError(
            "pdf_malformed",
            "The PDF companion could not be parsed safely",
        ) from error
    full_text = "\f".join(normalized_pages).encode("utf-8")
    return (
        page_rows,
        hashlib.sha256(full_text).hexdigest(),
        total_chars,
        getattr(pypdf, "__version__", "unknown")[:64],
    )


def _validated_pdftoppm(
    configured: Optional[Path],
) -> Optional[Path]:
    raw = (
        str(configured)
        if configured is not None
        else os.environ.get("TRAFFICSUPERVISION_PDFTOPPM_PATH")
        or shutil.which("pdftoppm")
    )
    if not raw:
        return None
    candidate = Path(raw)
    candidates = [candidate]
    # The Codex desktop runtime exposes a non-executable .cmd shim.  Resolve
    # only the known adjacent bundled executable; never execute the wrapper.
    if candidate.suffix.casefold() == ".cmd":
        candidates.extend(
            (
                candidate.parent.parent.parent
                / "native"
                / "poppler"
                / "Library"
                / "bin"
                / "pdftoppm.exe",
                candidate.parent.parent
                / "Library"
                / "bin"
                / "pdftoppm.exe",
            )
        )
    for item in candidates:
        try:
            resolved = item.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            resolved.name.casefold() == "pdftoppm.exe"
            and not resolved.is_symlink()
            and not _is_reparse_point(resolved)
            and stat.S_ISREG(metadata.st_mode)
        ):
            return resolved
    return None


def _directory_size(path: Path, maximum: int) -> int:
    total = 0
    for current, directories, files in os.walk(
        path, followlinks=False
    ):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                raise PdfCompanionAuditError(
                    "pdf_render_invalid",
                    "The PDF renderer created a linked directory",
                )
        for name in files:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                raise PdfCompanionAuditError(
                    "pdf_render_invalid",
                    "The PDF renderer created a linked file",
                )
            total += child.stat().st_size
            if total > maximum:
                raise PdfCompanionAuditError(
                    "pdf_render_limit",
                    "The PDF render smoke exceeded its byte bound",
                )
    return total


def _run_renderer(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_process_output_bytes: int,
    max_render_bytes: int,
) -> bytes:
    stdout_path = cwd / ".pdftoppm.stdout"
    stderr_path = cwd / ".pdftoppm.stderr"
    process: Optional[subprocess.Popen[bytes]] = None
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else 0
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open(
            "xb"
        ) as stderr:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=str(cwd),
                close_fds=True,
                creationflags=creation_flags,
            )
            while process.poll() is None:
                if time.monotonic() > deadline:
                    process.kill()
                    process.wait(timeout=3)
                    raise PdfCompanionAuditError(
                        "pdf_render_timeout",
                        "The PDF render smoke exceeded its time bound",
                    )
                stdout.flush()
                stderr.flush()
                if (
                    stdout_path.stat().st_size
                    + stderr_path.stat().st_size
                    > max_process_output_bytes
                ):
                    process.kill()
                    process.wait(timeout=3)
                    raise PdfCompanionAuditError(
                        "pdf_render_limit",
                        "The PDF renderer exceeded its output bound",
                    )
                _directory_size(cwd, max_render_bytes)
                time.sleep(0.05)
        if process.returncode != 0:
            raise PdfCompanionAuditError(
                "pdf_render_failed",
                "The PDF renderer rejected the companion",
            )
        return (
            stdout_path.read_bytes() + stderr_path.read_bytes()
        )[:max_process_output_bytes]
    except PdfCompanionAuditError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if process is not None and process.poll() is None:
            process.kill()
        raise PdfCompanionAuditError(
            "pdf_render_failed",
            "The PDF renderer could not be executed",
        ) from error
    finally:
        for output in (stdout_path, stderr_path):
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass


def _png_audit(path: Path, maximum_bytes: int) -> dict[str, Any]:
    try:
        metadata = path.stat()
        body = path.read_bytes()
    except OSError as error:
        raise PdfCompanionAuditError(
            "pdf_render_failed",
            "A PDF render smoke artifact is unavailable",
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 24 <= len(body) <= maximum_bytes
        or not body.startswith(_PNG_HEADER)
        or body[12:16] != b"IHDR"
    ):
        raise PdfCompanionAuditError(
            "pdf_render_invalid",
            "A PDF render smoke artifact is invalid",
        )
    width = int.from_bytes(body[16:20], "big")
    height = int.from_bytes(body[20:24], "big")
    if not 1 <= width <= 20_000 or not 1 <= height <= 20_000:
        raise PdfCompanionAuditError(
            "pdf_render_invalid",
            "A PDF render smoke artifact has invalid dimensions",
        )
    return {
        "width": width,
        "height": height,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "media_type": "image/png",
    }


def _render_smoke(
    pdf_path: Path,
    *,
    page_count: int,
    renderer: Optional[Path],
    limits: PdfCompanionLimits,
) -> dict[str, Any]:
    if renderer is None:
        return {
            "status": "renderer_unavailable",
            "renderer": None,
            "renderer_version": None,
            "requested_pages": [],
            "rendered_pages": [],
        }
    requested_pages = tuple(
        sorted(
            {
                1,
                max(1, math.ceil(page_count / 2)),
                page_count,
            }
        )
    )[: limits.max_render_pages]
    temporary_root = Path(tempfile.gettempdir()).resolve()
    run_directory: Optional[Path] = None
    try:
        run_directory = Path(
            tempfile.mkdtemp(
                prefix="TrafficVissonPdfAudit-",
                dir=str(temporary_root),
            )
        )
        version_raw = _run_renderer(
            (str(renderer), "-v"),
            cwd=run_directory,
            timeout_seconds=min(
                15.0, limits.render_timeout_seconds
            ),
            max_process_output_bytes=(
                limits.max_process_output_bytes
            ),
            max_render_bytes=limits.max_render_total_bytes,
        ).decode("utf-8", errors="replace")
        version_line = next(
            (
                line.strip()
                for line in version_raw.splitlines()
                if "pdftoppm version" in line.casefold()
            ),
            "pdftoppm version unknown",
        )[:256]
        rendered = []
        for page_number in requested_pages:
            prefix = run_directory / f"page-{page_number:04d}"
            _run_renderer(
                (
                    str(renderer),
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-r",
                    str(limits.render_dpi),
                    "-png",
                    str(pdf_path),
                    str(prefix),
                ),
                cwd=run_directory,
                timeout_seconds=limits.render_timeout_seconds,
                max_process_output_bytes=(
                    limits.max_process_output_bytes
                ),
                max_render_bytes=limits.max_render_total_bytes,
            )
            png_path = prefix.with_suffix(".png")
            observation = _png_audit(
                png_path, limits.max_render_page_bytes
            )
            rendered.append(
                {"page_number": page_number, **observation}
            )
        if len(rendered) != len(requested_pages):
            raise PdfCompanionAuditError(
                "pdf_render_failed",
                "The PDF render smoke did not cover every requested page",
            )
        return {
            "status": "smoke_passed",
            "renderer": "pdftoppm",
            "renderer_version": version_line,
            "render_dpi": limits.render_dpi,
            "requested_pages": list(requested_pages),
            "rendered_pages": rendered,
        }
    finally:
        if (
            run_directory is not None
            and run_directory.parent == temporary_root
            and run_directory.name.startswith(
                "TrafficVissonPdfAudit-"
            )
        ):
            try:
                shutil.rmtree(run_directory)
            except OSError:
                pass


def audit_pdf_companion(
    path: Path,
    *,
    definition: PdfCompanionDefinition,
    limits: Optional[PdfCompanionLimits] = None,
    pdftoppm_path: Optional[Path] = None,
) -> tuple[dict[str, Any], _PdfIdentity]:
    """Audit one exact allowlisted PDF without extracting its images."""

    policy = limits or PdfCompanionLimits()
    source = Path(path)
    if (
        not source.is_absolute()
        or source.name != definition.document_name
        or source.suffix.casefold() != ".pdf"
    ):
        raise PdfCompanionAuditError(
            "pdf_path_rejected",
            "The PDF companion path is not allowlisted",
        )
    identity_before = _identity(source, policy.max_pdf_bytes)
    digest, _header, _tail = _hash_pdf(
        source, identity_before, policy.max_pdf_bytes
    )
    if digest != definition.expected_pdf_sha256:
        raise PdfCompanionAuditError(
            "pdf_sha256_mismatch",
            "The PDF companion SHA-256 does not match its registration",
        )
    (
        pages,
        full_text_sha256,
        total_text_chars,
        parser_version,
    ) = _pypdf_text_audit(
        source,
        definition=definition,
        limits=policy,
    )
    render = _render_smoke(
        source,
        page_count=len(pages),
        renderer=_validated_pdftoppm(pdftoppm_path),
        limits=policy,
    )
    identity_after = _identity(source, policy.max_pdf_bytes)
    if identity_after != identity_before:
        raise PdfCompanionAuditError(
            "pdf_source_changed",
            "The PDF companion changed during its audit",
        )
    payload_without_hash = {
        "schema_version": PDF_COMPANION_SCHEMA,
        "role": PDF_COMPANION_ROLE,
        "quarter": definition.quarter,
        "document_name": definition.document_name,
        "pdf_sha256": digest,
        "expected_pdf_sha256": definition.expected_pdf_sha256,
        "size_bytes": identity_before.size_bytes,
        "status": "ready",
        "page_count": len(pages),
        "expected_page_count": definition.expected_page_count,
        "pages": pages,
        "full_text_sha256": full_text_sha256,
        "full_text_char_count": total_text_chars,
        "text_hash_method": (
            "sha256(nfc_lf_normalized_pypdf_text_utf8)"
        ),
        "full_text_hash_method": (
            "sha256(form_feed_joined_normalized_page_text_utf8)"
        ),
        "parser": "pypdf",
        "parser_version": parser_version,
        "strict_parsing": True,
        "render": render,
        "pdf_images_added_as_cases": False,
        "case_source": "paired_hwpx_photo_slots_only",
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
    }
    return (
        {
            **payload_without_hash,
            "audit_sha256": hashlib.sha256(
                json.dumps(
                    payload_without_hash,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        identity_before,
    )


def _error_snapshot(
    definition: PdfCompanionDefinition,
    code: str,
) -> dict[str, Any]:
    return {
        "schema_version": PDF_COMPANION_SCHEMA,
        "role": PDF_COMPANION_ROLE,
        "quarter": definition.quarter,
        "document_name": definition.document_name,
        "pdf_sha256": None,
        "expected_pdf_sha256": definition.expected_pdf_sha256,
        "size_bytes": None,
        "status": code,
        "page_count": None,
        "expected_page_count": definition.expected_page_count,
        "pages": [],
        "full_text_sha256": None,
        "full_text_char_count": 0,
        "text_hash_method": (
            "sha256(nfc_lf_normalized_pypdf_text_utf8)"
        ),
        "full_text_hash_method": (
            "sha256(form_feed_joined_normalized_page_text_utf8)"
        ),
        "parser": "pypdf",
        "parser_version": None,
        "strict_parsing": True,
        "render": {
            "status": "not_run",
            "renderer": None,
            "renderer_version": None,
            "requested_pages": [],
            "rendered_pages": [],
        },
        "pdf_images_added_as_cases": False,
        "case_source": "paired_hwpx_photo_slots_only",
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
        "audit_sha256": None,
    }


class PdfCompanionCatalog:
    """Lazy exact-quarter PDF companion registry."""

    def __init__(
        self,
        sample_root: Path,
        *,
        definitions: Sequence[PdfCompanionDefinition] = (
            DEFAULT_PDF_COMPANIONS
        ),
        limits: Optional[PdfCompanionLimits] = None,
        pdftoppm_path: Optional[Path] = None,
    ) -> None:
        root = Path(sample_root)
        if not root.is_absolute():
            raise ValueError(
                "PDF companion sample_root must be absolute"
            )
        if not definitions:
            raise ValueError(
                "At least one PDF companion definition is required"
            )
        quarters = [item.quarter for item in definitions]
        names = [item.document_name.casefold() for item in definitions]
        if (
            len(quarters) != len(set(quarters))
            or len(names) != len(set(names))
        ):
            raise ValueError(
                "PDF companion definitions must be unique"
            )
        self._root = root.resolve()
        self._definitions = {
            item.quarter: item for item in definitions
        }
        self._limits = limits or PdfCompanionLimits()
        self._pdftoppm_path = pdftoppm_path
        self._cache: Dict[
            int, tuple[_PdfIdentity, str, dict[str, Any]]
        ] = {}
        self._lock = threading.RLock()

    def snapshot(self, quarter: int) -> dict[str, Any]:
        with self._lock:
            definition = self._definitions.get(quarter)
            if definition is None:
                raise ValueError(
                    "PDF companion quarter is not registered"
                )
            path = (self._root / definition.document_name).resolve()
            if (
                path.parent != self._root
                or path.name != definition.document_name
            ):
                return _error_snapshot(
                    definition, "pdf_path_rejected"
                )
            if not path.exists():
                self._cache.pop(quarter, None)
                return _error_snapshot(
                    definition, "pdf_missing"
                )
            try:
                current_identity = _identity(
                    path, self._limits.max_pdf_bytes
                )
                current_digest, _, _ = _hash_pdf(
                    path,
                    current_identity,
                    self._limits.max_pdf_bytes,
                )
                if (
                    current_digest
                    != definition.expected_pdf_sha256
                ):
                    self._cache.pop(quarter, None)
                    return _error_snapshot(
                        definition, "pdf_sha256_mismatch"
                    )
                cached = self._cache.get(quarter)
                if (
                    cached is not None
                    and cached[0] == current_identity
                    and cached[1] == current_digest
                ):
                    return copy.deepcopy(cached[2])
                result, identity = audit_pdf_companion(
                    path,
                    definition=definition,
                    limits=self._limits,
                    pdftoppm_path=self._pdftoppm_path,
                )
            except PdfCompanionAuditError as error:
                self._cache.pop(quarter, None)
                return _error_snapshot(definition, error.code)
            self._cache[quarter] = (
                identity,
                result["pdf_sha256"],
                copy.deepcopy(result),
            )
            return copy.deepcopy(result)


__all__ = [
    "DEFAULT_PDF_COMPANIONS",
    "PDF_COMPANION_ROLE",
    "PDF_COMPANION_SCHEMA",
    "PdfCompanionAuditError",
    "PdfCompanionCatalog",
    "PdfCompanionDefinition",
    "PdfCompanionLimits",
    "audit_pdf_companion",
]
