from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

import app.hwp_com as hwp_com
from app.hwp_com import (
    HWP_PROG_ID,
    PLAN_VERSION,
    SECURITY_MODULE,
    HwpAutomationError,
    HwpComService,
    HwpStaWorker,
    Win32HwpAdapter,
    fingerprint_document,
    registry_preflight,
)
from app.hwp_render_validation import (
    ExplicitApplyApproval,
    export_pdf_after_explicit_apply_approval,
)


class FakePythonCom:
    def __init__(self):
        self.initialized_thread = None
        self.uninitialized_thread = None

    def CoInitialize(self):
        self.initialized_thread = threading.get_ident()

    def CoUninitialize(self):
        self.uninitialized_thread = threading.get_ident()


class FakeRegistry:
    def __init__(self, prog=True, module=True):
        self.prog = prog
        self.module = module
        self.calls = []

    def prog_id_registered(self, value):
        self.calls.append(("prog", value, threading.get_ident()))
        return self.prog

    def module_registered(self, value):
        self.calls.append(("module", value, threading.get_ident()))
        return self.module


class FakeAdapter:
    def __init__(self, log, hwpml=None, mutate_template=False):
        self.log = log
        self.hwpml = hwpml or (
            '<HWPML><P/><P/><TABLE/><FIELD name="보고서_제목"/>'
            '<BOOKMARK Name="현장사진"/><PICTURE/></HWPML>'
        )
        self.current = None
        self.mutate_template = mutate_template

    def _record(self, name, *values):
        self.log.append((name, threading.get_ident(), *values))

    def register_security_module(self):
        self._record("register")

    def open_document(self, path):
        self.current = path
        self._record("open", path.name)

    def read_hwpml(self, max_chars):
        self._record("read", max_chars)
        return self.hwpml[:max_chars], len(self.hwpml) > max_chars

    def fill_text(self, target, text):
        self._record("fill_text", target.kind, target.name, text)
        if self.mutate_template and self.current is not None:
            self.current.write_bytes(self.current.read_bytes() + b"changed")

    def fill_cell(self, target, text):
        self._record("fill_cell", target.kind, target.name, text)

    def add_table_row(self, target, cells):
        self._record(
            "add_table_row",
            target.kind,
            target.name,
            tuple((item.kind, item.name, text) for item, text in cells),
        )

    def insert_image(self, target, source_path):
        self._record("insert_image", target.name, source_path.name)

    def insert_map(self, target, source_path):
        self._record("insert_map", target.name, source_path.name)

    def save_as(self, output_path):
        self._record("save", output_path.name)
        output_path.write_bytes(b"edited-hwp-output")

    def close_document(self):
        self._record("close_document")
        self.current = None

    def close(self):
        self._record("close")


class FakePdfAdapter(FakeAdapter):
    def save_as(self, output_path):
        self._record("save", output_path.name)
        output_path.write_bytes(b"%PDF-1.7\n%%EOF\n")


@pytest.fixture
def sta_worker():
    pythoncom = FakePythonCom()
    worker = HwpStaWorker(pythoncom)
    yield worker, pythoncom
    worker.close()


@pytest.fixture
def document_files(tmp_path):
    template = tmp_path / "template.hwpx"
    image = tmp_path / "evidence.png"
    map_image = tmp_path / "map.jpg"
    template.write_bytes(b"original-template")
    image.write_bytes(b"png")
    map_image.write_bytes(b"jpg")
    return template, image, map_image


def edit_plan(image, map_image):
    return {
        "version": PLAN_VERSION,
        "operations": [
            {
                "op": "fill_text",
                "target": {"kind": "field", "name": "보고서_제목"},
                "text": "도로 점검 결과",
            },
            {
                "op": "fill_cell",
                "target": {"kind": "field", "name": "조치_1"},
                "text": "삭초 필요",
            },
            {
                "op": "add_table_row",
                "target": {"kind": "bookmark", "name": "점검표"},
                "cells": [
                    {
                        "target": {"kind": "field", "name": "신규_위치"},
                        "text": "천안방향",
                    }
                ],
            },
            {
                "op": "insert_image",
                "target": {"kind": "bookmark", "name": "현장사진"},
                "source_path": str(image),
            },
            {
                "op": "insert_map",
                "target": {"kind": "bookmark", "name": "위치도"},
                "source_path": str(map_image),
            },
        ],
    }


def test_registry_preflight_is_registry_only():
    registry = FakeRegistry()
    result = registry_preflight(registry)
    assert result.available is True
    assert [(kind, value) for kind, value, _thread in registry.calls] == [
        ("prog", HWP_PROG_ID),
        ("module", SECURITY_MODULE),
    ]


def test_registry_preflight_reports_missing_security_module():
    result = registry_preflight(FakeRegistry(prog=True, module=False))
    assert result.available is False
    assert result.prog_id_registered is True
    assert result.security_module_registered is False


def test_service_preflight_never_constructs_adapter(sta_worker):
    registry = FakeRegistry()

    def forbidden_adapter():
        raise AssertionError("preflight must not activate COM")

    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=forbidden_adapter,
        preflight_reader=registry,
    )
    assert service.preflight().available is True


def test_win32_adapter_uses_fixed_dispatch_and_module_names():
    calls = []

    class FakeHwp:
        def RegisterModule(self, dll_name, module_name):
            calls.append(("register", dll_name, module_name))
            return True

        def Quit(self):
            calls.append(("quit",))

    def dispatch(prog_id):
        calls.append(("dispatch", prog_id))
        return FakeHwp()

    adapter = Win32HwpAdapter(dispatch)
    adapter.register_security_module()
    adapter.close()
    assert calls == [
        ("dispatch", HWP_PROG_ID),
        ("register", "FilePathCheckDLL", SECURITY_MODULE),
        ("quit",),
    ]


def test_win32_adapter_uses_explicit_pdf_save_format(tmp_path):
    calls = []

    class FakeHwp:
        def SaveAs(self, path, document_format, options):
            calls.append(("save", Path(path).name, document_format, options))
            return True

        def Quit(self):
            calls.append(("quit",))

    adapter = Win32HwpAdapter(lambda _prog_id: FakeHwp())
    adapter.save_as(tmp_path / "report.pdf")
    adapter.close()

    assert calls == [
        ("save", "report.pdf", "PDF", ""),
        ("quit",),
    ]


def test_publish_approved_exact_hwpx_reopens_saves_and_publishes_atomically(
    sta_worker,
    tmp_path,
):
    source_root = tmp_path / "draft"
    destination_root = tmp_path / "application"
    source_root.mkdir()
    destination_root.mkdir()
    source = (source_root / "exact-preview.hwpx").resolve()
    destination = (
        destination_root / "published.hwpx"
    ).resolve()
    source.write_bytes(b"PK\x03\x04exact-preview")
    log = []
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakeAdapter(log),
    )

    result = service.publish_approved_exact_hwpx(
        approval_id="approval-1",
        source_hwpx=source,
        destination_hwpx=destination,
    )

    assert result.output_path == destination
    assert destination.read_bytes() == b"edited-hwp-output"
    assert result.sha256 == hashlib.sha256(
        b"edited-hwp-output"
    ).hexdigest()
    assert source.read_bytes() == b"PK\x03\x04exact-preview"
    assert [item[0] for item in log] == [
        "register",
        "open",
        "save",
        "close_document",
        "open",
        "read",
        "close_document",
        "close",
    ]


def test_worker_initializes_and_uninitializes_same_sta_thread(sta_worker):
    worker, pythoncom = sta_worker
    called_thread = worker.submit(threading.get_ident)
    assert called_thread == pythoncom.initialized_thread
    assert called_thread != threading.get_ident()
    worker.close()
    assert pythoncom.uninitialized_thread == called_thread


def test_preview_is_default_and_does_not_write(document_files, sta_worker):
    template, image, map_image = document_files
    original = template.read_bytes()
    worker, pythoncom = sta_worker
    log = []
    service = HwpComService(
        worker=worker, adapter_factory=lambda: FakeAdapter(log)
    )

    result = service.execute(template, edit_plan(image, map_image))

    assert result.mode == "preview"
    assert result.operation_count == 5
    assert result.structure.paragraph_count == 2
    assert result.structure.table_count == 1
    assert result.structure.field_names == ("보고서_제목",)
    assert result.structure.bookmark_names == ("현장사진",)
    assert result.document_fingerprint.sha256 == hashlib.sha256(original).hexdigest()
    assert template.read_bytes() == original
    assert {entry[1] for entry in log} == {pythoncom.initialized_thread}
    assert [entry[0] for entry in log] == ["register", "open", "read", "close"]
    opened_name = next(entry[2] for entry in log if entry[0] == "open")
    assert opened_name != template.name
    assert ".trafficvisson-preview-" in opened_name
    assert not list(template.parent.glob(".*.trafficvisson-preview-*"))


def test_explicit_approved_hwpx_pdf_export_uses_owned_copy(
    document_files,
    sta_worker,
):
    source, _image, _map_image = document_files
    source_before = source.read_bytes()
    destination = source.with_name("approved-report.pdf")
    log = []
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakePdfAdapter(log),
    )

    result = service.export_applied_hwpx_to_pdf(
        approval_id="apply-proof-123",
        source_hwpx=source,
        destination_pdf=destination,
    )

    assert result == destination
    assert destination.read_bytes().startswith(b"%PDF-")
    assert source.read_bytes() == source_before
    assert [entry[0] for entry in log] == [
        "register",
        "open",
        "save",
        "close_document",
        "close",
    ]
    opened_name = next(entry[2] for entry in log if entry[0] == "open")
    assert opened_name != source.name
    assert ".trafficvisson-pdf-source-" in opened_name
    assert not list(source.parent.glob(".*.trafficvisson-pdf-source-*"))
    assert not list(source.parent.glob(".*.trafficvisson-pdf-output-*"))


def test_render_export_helper_accepts_hwp_com_service_only_after_hash_bound_approval(
    document_files,
    sta_worker,
):
    source, _image, _map_image = document_files
    destination = source.with_name("hash-bound-report.pdf")
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakePdfAdapter([]),
    )
    fingerprint = fingerprint_document(source)

    exported = export_pdf_after_explicit_apply_approval(
        exporter=service,
        approval=ExplicitApplyApproval(
            approval_id="apply-proof-render-1",
            output_hwpx_sha256=fingerprint.sha256,
            granted=True,
        ),
        output_hwpx_fingerprint=fingerprint,
        destination_pdf=destination,
    )

    assert exported == destination
    assert destination.read_bytes().startswith(b"%PDF-")


def test_pdf_export_rejects_missing_approval_before_adapter(
    document_files,
    sta_worker,
):
    source, _image, _map_image = document_files
    constructed = []

    def adapter_factory():
        constructed.append(True)
        return FakePdfAdapter([])

    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=adapter_factory,
    )
    with pytest.raises(HwpAutomationError, match="승인"):
        service.export_applied_hwpx_to_pdf(
            approval_id="",
            source_hwpx=source,
            destination_pdf=source.with_name("must-not-exist.pdf"),
        )

    assert constructed == []


def test_pdf_export_fails_closed_on_non_pdf_bytes(
    document_files,
    sta_worker,
):
    source, _image, _map_image = document_files
    destination = source.with_name("must-not-exist.pdf")
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakeAdapter([]),
    )

    with pytest.raises(HwpAutomationError):
        service.export_applied_hwpx_to_pdf(
            approval_id="apply-proof-invalid-output",
            source_hwpx=source,
            destination_pdf=destination,
        )

    assert not destination.exists()
    assert not list(source.parent.glob(".*.trafficvisson-pdf-source-*"))
    assert not list(source.parent.glob(".*.trafficvisson-pdf-output-*"))


def test_apply_rejects_document_changed_after_preview_before_adapter(
    document_files, sta_worker
):
    template, image, map_image = document_files
    log = []
    service = HwpComService(
        worker=sta_worker[0], adapter_factory=lambda: FakeAdapter(log)
    )
    preview = service.execute(template, edit_plan(image, map_image))
    calls_after_preview = list(log)
    template.write_bytes(b"changed-after-preview")

    with pytest.raises(HwpAutomationError, match="미리보기 이후"):
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=template.with_name("must-not-exist.hwpx"),
            expected_fingerprint=preview.document_fingerprint,
        )

    assert log == calls_after_preview
    assert not template.with_name("must-not-exist.hwpx").exists()


def test_apply_requires_accept_and_new_output(document_files, sta_worker):
    template, image, map_image = document_files
    service = HwpComService(
        worker=sta_worker[0], adapter_factory=lambda: FakeAdapter([])
    )
    with pytest.raises(HwpAutomationError, match="출력"):
        service.execute(
            template, edit_plan(image, map_image), accept=True
        )
    existing = template.with_name("existing.hwpx")
    existing.write_bytes(b"owned-by-user")
    with pytest.raises(HwpAutomationError, match="이미"):
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=existing,
        )
    assert existing.read_bytes() == b"owned-by-user"


def test_apply_allowlist_backup_readback_and_rollback(document_files, sta_worker):
    template, image, map_image = document_files
    original = template.read_bytes()
    output = template.with_name("report-result.hwpx")
    worker, pythoncom = sta_worker
    log = []
    service = HwpComService(
        worker=worker, adapter_factory=lambda: FakeAdapter(log)
    )

    result = service.execute(
        template,
        edit_plan(image, map_image),
        accept=True,
        output_path=output,
    )

    assert result.mode == "applied"
    assert template.read_bytes() == original
    assert result.backup_path.parent == template.parent
    assert result.backup_path.read_bytes() == original
    assert output.read_bytes() == b"edited-hwp-output"
    assert result.sha256 == hashlib.sha256(b"edited-hwp-output").hexdigest()
    assert [entry[0] for entry in log] == [
        "register",
        "open",
        "fill_text",
        "fill_cell",
        "add_table_row",
        "insert_image",
        "insert_map",
        "save",
        "close_document",
        "open",
        "read",
        "close_document",
        "close",
    ]
    assert {entry[1] for entry in log} == {pythoncom.initialized_thread}
    opened_names = [entry[2] for entry in log if entry[0] == "open"]
    assert template.name not in opened_names
    assert ".trafficvisson-working-" in opened_names[0]
    assert ".trafficvisson-output-" in opened_names[1]
    assert not list(template.parent.glob(".*.trafficvisson-working-*"))

    rollback = service.rollback(result.rollback_token)
    assert rollback.rolled_back is True
    assert output.read_bytes() == original
    assert not result.backup_path.exists()
    with pytest.raises(HwpAutomationError, match="롤백"):
        service.rollback(result.rollback_token)


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "run_macro", "target": {"kind": "field", "name": "x"}},
        {
            "op": "HAction.Run",
            "target": {"kind": "field", "name": "x"},
            "member": "Quit",
        },
        {"op": "__getattr__", "target": {"kind": "field", "name": "x"}},
        {
            "op": "fill_text",
            "target": {"kind": "field", "name": "x"},
            "text": "safe",
            "member": "Quit",
        },
    ],
)
def test_plan_refuses_macros_commands_and_arbitrary_com_members(
    tmp_path, sta_worker, operation
):
    template = tmp_path / "template.hwp"
    template.write_bytes(b"hwp")
    service = HwpComService(
        worker=sta_worker[0], adapter_factory=lambda: FakeAdapter([])
    )
    with pytest.raises(HwpAutomationError, match="허용되지 않은"):
        service.execute(
            template, {"version": PLAN_VERSION, "operations": [operation]}
        )


def test_rejects_relative_wrong_extension_and_output_outside_sibling(
    document_files, tmp_path, sta_worker
):
    template, image, map_image = document_files
    service = HwpComService(
        worker=sta_worker[0], adapter_factory=lambda: FakeAdapter([])
    )
    with pytest.raises(HwpAutomationError):
        service.execute(Path("relative.hwpx"), edit_plan(image, map_image))
    bad = tmp_path / "template.txt"
    bad.write_text("x")
    with pytest.raises(HwpAutomationError):
        service.execute(bad, edit_plan(image, map_image))
    other = tmp_path / "outside"
    other.mkdir()
    with pytest.raises(HwpAutomationError, match="같은 폴더"):
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=other / "result.hwpx",
        )


def test_rejects_symlink_template_when_supported(document_files, tmp_path, sta_worker):
    template, image, map_image = document_files
    link = tmp_path / "linked.hwpx"
    try:
        link.symlink_to(template)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    service = HwpComService(
        worker=sta_worker[0], adapter_factory=lambda: FakeAdapter([])
    )
    with pytest.raises(HwpAutomationError, match="링크"):
        service.execute(link, edit_plan(image, map_image))


def test_apply_mutates_only_owned_working_copy_and_never_original(
    document_files, sta_worker
):
    template, image, map_image = document_files
    original = template.read_bytes()
    original_sha256 = hashlib.sha256(original).hexdigest()
    output = template.with_name("result.hwpx")
    log = []
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakeAdapter(log, mutate_template=True),
    )
    result = service.execute(
        template,
        edit_plan(image, map_image),
        accept=True,
        output_path=output,
    )

    assert template.read_bytes() == original
    assert hashlib.sha256(template.read_bytes()).hexdigest() == original_sha256
    assert template.name not in [
        entry[2] for entry in log if entry[0] == "open"
    ]
    assert output.read_bytes() == b"edited-hwp-output"
    service.rollback(result.rollback_token)


def test_apply_failure_keeps_original_hash_and_one_recovery_artifact(
    document_files, sta_worker
):
    template, image, map_image = document_files
    original = template.read_bytes()
    original_sha256 = hashlib.sha256(original).hexdigest()
    output = template.with_name("failed-result.hwpx")
    log = []

    class FailingAdapter(FakeAdapter):
        def fill_text(self, target, text):
            super().fill_text(target, text)
            raise RuntimeError("simulated HRESULT with private path")

    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FailingAdapter(log, mutate_template=True),
    )

    with pytest.raises(HwpAutomationError) as captured:
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=output,
        )

    assert "private path" not in str(captured.value)
    assert template.read_bytes() == original
    assert hashlib.sha256(template.read_bytes()).hexdigest() == original_sha256
    assert not output.exists()
    retained = list(template.parent.glob(".*.trafficvisson-working-*"))
    journals = list(template.parent.glob(".*.trafficvisson-recovery-*.json"))
    assert len(retained) == 1
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["schema_version"] == "trafficvisson-hwp-recovery-v1"
    assert journal["phase"] == "edit_working"
    assert journal["source_file_name"] == template.name
    assert journal["intended_output_file_name"] == output.name
    assert journal["retained_artifact_file_name"] == retained[0].name
    assert journal["source_sha256"] == original_sha256
    assert journal["published_output_removed"] is False
    assert retained[0].read_bytes().startswith(original)
    assert template.name not in [
        entry[2] for entry in log if entry[0] == "open"
    ]


def test_post_publish_failure_removes_owned_output_and_retains_pristine_backup(
    document_files, sta_worker, monkeypatch
):
    template, image, map_image = document_files
    original = template.read_bytes()
    original_sha256 = hashlib.sha256(original).hexdigest()
    output = template.with_name("post-publish-failure.hwpx")
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakeAdapter([]),
    )
    real_sha256 = hwp_com._sha256

    def reject_published_output(path, identity):
        digest = real_sha256(path, identity)
        if path == output:
            return "0" * 64
        return digest

    monkeypatch.setattr(hwp_com, "_sha256", reject_published_output)

    with pytest.raises(HwpAutomationError):
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=output,
        )

    assert template.read_bytes() == original
    assert hashlib.sha256(template.read_bytes()).hexdigest() == original_sha256
    assert not output.exists()
    backups = list(template.parent.glob(".*.trafficvisson-backup-*"))
    journals = list(template.parent.glob(".*.trafficvisson-recovery-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "publish_output"
    assert journal["retained_artifact_file_name"] == backups[0].name
    assert journal["published_output_removed"] is True


def test_adapter_failure_is_scrubbed(document_files, sta_worker):
    template, image, map_image = document_files

    def fail():
        raise RuntimeError("C:\\secret\\customer.hwpx HRESULT 0x80004005")

    service = HwpComService(worker=sta_worker[0], adapter_factory=fail)
    with pytest.raises(HwpAutomationError) as captured:
        service.execute(template, edit_plan(image, map_image))
    assert "secret" not in str(captured.value)
    assert "HRESULT" not in str(captured.value)


@pytest.mark.skipif(
    os.environ.get("TRAFFICSUPERVISION_RUN_HWP_PREFLIGHT") != "1",
    reason="opt-in registry-only Windows preflight",
)
def test_optional_environment_registry_preflight_does_not_open_documents():
    result = registry_preflight()
    assert isinstance(result.available, bool)
