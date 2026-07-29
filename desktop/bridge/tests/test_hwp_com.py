from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading

import pytest

from app.hwp_com import (
    HWP_PROG_ID,
    PLAN_VERSION,
    SECURITY_MODULE,
    HwpAutomationError,
    HwpComService,
    HwpStaWorker,
    Win32HwpAdapter,
    registry_preflight,
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
    assert template.read_bytes() == original
    assert {entry[1] for entry in log} == {pythoncom.initialized_thread}
    assert [entry[0] for entry in log] == ["register", "open", "read", "close"]


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


def test_detects_template_change_during_apply_and_scrubs_detail(
    document_files, sta_worker
):
    template, image, map_image = document_files
    output = template.with_name("result.hwpx")
    service = HwpComService(
        worker=sta_worker[0],
        adapter_factory=lambda: FakeAdapter([], mutate_template=True),
    )
    with pytest.raises(HwpAutomationError) as captured:
        service.execute(
            template,
            edit_plan(image, map_image),
            accept=True,
            output_path=output,
        )
    message = str(captured.value)
    assert str(template) not in message
    assert "COM" not in message
    assert not output.exists()


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
    os.environ.get("TRAFFICVISSON_RUN_HWP_PREFLIGHT") != "1",
    reason="opt-in registry-only Windows preflight",
)
def test_optional_environment_registry_preflight_does_not_open_documents():
    result = registry_preflight()
    assert isinstance(result.available, bool)
