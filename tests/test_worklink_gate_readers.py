from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from mimir.worklink import evidence as ev


@pytest.mark.parametrize("reader", ["junit", "lastfailed", "nodeids"])
@pytest.mark.parametrize("unsafe", ["fifo", "symlink", "ancestor"])
@pytest.mark.timeout(3)
def test_report_consumers_reject_unsafe_files(tmp_path, reader, unsafe):
    root = tmp_path.resolve()
    (root / "junit.xml").write_text('<testsuite tests="1" failures="1"/>')
    cache = root / "cache/v/cache"
    cache.mkdir(parents=True)
    path = root / "junit.xml" if reader == "junit" else cache / reader
    content = '<testsuite tests="1"/>' if reader == "junit" else ('["x::y"]' if reader == "nodeids" else '{"x::y":true}')
    path.unlink(missing_ok=True)
    if unsafe == "fifo":
        os.mkfifo(path)
    elif unsafe == "symlink":
        outside = root / "outside"
        outside.write_text(content)
        path.symlink_to(outside)
    else:
        path.write_text(content)
        alias = root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    if reader == "junit":
        assert ev.read_pytest_result("pytest", root).report_error == "junit_read_error"
    else:
        assert ev._pytest_cache_ids(root, reader) == ()
        assert ev.read_pytest_result("pytest", root).failed_tests == ()


@pytest.mark.parametrize("reader", ["junit", "lastfailed", "nodeids"])
def test_report_consumers_reject_growth(tmp_path, monkeypatch, reader):
    root = tmp_path.resolve()
    (root / "junit.xml").write_text('<testsuite tests="1"/>')
    cache = root / "cache/v/cache"
    cache.mkdir(parents=True)
    path = root / "junit.xml" if reader == "junit" else cache / reader
    path.write_bytes(b" " if reader == "junit" else b"[]")
    payload = b'<testsuite tests="1"/>' if reader == "junit" else (b'["x::y"]' if reader == "nodeids" else b'{"x::y":true}')
    original = ev._gate_open

    @contextlib.contextmanager
    def growing(target):
        with original(target) as source:
            class Growing:
                def fileno(self):
                    return source.fileno()

                def read(self, size):
                    if target == path:
                        path.write_bytes(payload)
                    return source.read(size)
            yield Growing()

    monkeypatch.setattr(ev, "_gate_open", growing)
    if reader == "junit":
        assert ev.read_pytest_result("pytest", root).report_error == "junit_oversize"
    else:
        assert ev._pytest_cache_ids(root, reader) == ()
        path.write_bytes(b"[]")
        assert ev.read_pytest_result("pytest", root).failed_tests == ()


def test_reader_anchors_open_descriptor_against_symlink_swap(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "report"
    path.write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    original = os.open

    def swapping(name, flags, **kwargs):
        fd = original(name, flags, **kwargs)
        if name == "report":
            path.unlink()
            path.symlink_to(outside)
        return fd

    monkeypatch.setattr(ev.os, "open", swapping)
    assert ev._gate_read(path, 100) == b"safe"


def test_reader_bounds_growth_even_when_stat_lies(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "report"
    path.write_bytes(b"long data")
    original = ev.os.fstat

    def small(fd):
        values = list(original(fd))
        values[6] = 1
        return os.stat_result(values)

    monkeypatch.setattr(ev.os, "fstat", small)
    with pytest.raises(ValueError):
        ev._gate_read(path, 2)


def test_report_root_is_resolved_at_creation(tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    target = tmp_path / "real"
    target.mkdir()
    alias.symlink_to(target, target_is_directory=True)
    with ev._gate_report_directory(alias, True) as root:
        assert root == root.resolve(strict=True)
        (root / "junit.xml").write_bytes(b'<testsuite tests="1"/>')
        assert ev.read_pytest_result("pytest", root).counts.total == 1


def test_reader_rejects_parent_traversal(tmp_path):
    root = tmp_path.resolve()
    (root / "dir").mkdir()
    (root / "report").write_bytes(b"unsafe")
    with pytest.raises(ValueError):
        ev._gate_read(root / "dir/../report", 100)


def test_reader_rejects_relative_path(tmp_path):
    path = tmp_path.resolve() / "report"
    path.write_bytes(b"unsafe")
    # Without the absolute guard, traversal skips the first relative component.
    relative = Path("ignored") / path.relative_to("/")
    with pytest.raises(ValueError):
        ev._gate_read(relative, 100)


@pytest.mark.parametrize("oversize", [False, True])
def test_reader_bounds_io(tmp_path, monkeypatch, oversize):
    path = tmp_path.resolve() / "report"
    path.write_bytes(b"long data" if oversize else b"ok")
    original = ev._gate_open

    @contextlib.contextmanager
    def observed(target):
        with original(target) as source:
            class Observed:
                def fileno(self):
                    return source.fileno()

                def read(self, size=-1):
                    assert not oversize, "oversized file must be rejected before reading"
                    assert size == 3, "read must be bounded to max_bytes + 1"
                    return source.read(size)
            yield Observed()

    monkeypatch.setattr(ev, "_gate_open", observed)
    if oversize:
        with pytest.raises(ValueError):
            ev._gate_read(path, 2)
    else:
        assert ev._gate_read(path, 2) == b"ok"


@pytest.mark.timeout(3)
def test_reader_rejects_empty_fifo_before_read(tmp_path):
    path = tmp_path.resolve() / "pipe"
    os.mkfifo(path)
    with pytest.raises(OSError):
        ev._gate_read(path, 100)


def test_reader_rechecks_size_after_read(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "report"
    path.write_bytes(b"old")
    original = ev._gate_open

    @contextlib.contextmanager
    def growing(target):
        with original(target) as source:
            class Growing:
                def fileno(self):
                    return source.fileno()

                def read(self, size):
                    data = source.read(size)
                    path.write_bytes(b"longer")
                    return data
            yield Growing()

    monkeypatch.setattr(ev, "_gate_open", growing)
    with pytest.raises(ValueError):
        ev._gate_read(path, 100)
