"""End-to-end tests for the HDF5 import pipeline.

Run: .venv/bin/python -I -m pytest backend/tests -q
(The test package inserts the repo root onto sys.path itself, so -I works.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.app import hdf5  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app import importer  # noqa: E402
from backend.scripts import make_sample  # noqa: E402


@pytest.fixture(scope="module")
def sample_path(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("h5") / "sample.h5"
    make_sample.build(str(p))
    return str(p)


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# tree scan
# ---------------------------------------------------------------------------

def test_scan_lists_groups_datasets_attrs(sample_path):
    result = hdf5.scan_file(sample_path)
    paths = []

    def walk(node):
        if node["type"] == "dataset":
            paths.append(node["path"])
        for c in node.get("children", []):
            walk(c)

    walk(result["root"])
    assert "/sensors/temp" in paths
    assert "/sensors/pressure" in paths
    temp = _find(result["root"], "/sensors/temp")
    assert temp["compression"] == "gzip"
    assert temp["shape"] == [2000, 3]
    assert temp["channel_names"] == ["temp_A", "temp_B", "temp_C"]
    assert temp["attrs"]["scale_factor"] == 0.1
    assert result["time_suggestions"]["/sensors/temp"]["mode"] == "array"
    sug = result["time_suggestions"]["/sensors/pressure"]
    assert sug["mode"] == "sampled"
    assert sug["interval"] == 0.5
    # measurement unit (hPa) must not be reused as the time unit
    assert sug["unit"] == "s"
    # the time axis itself is not offered as a configurable dataset
    assert "/time" not in result["time_suggestions"]


def test_non_measurable_arrays_flagged(sample_path):
    result = hdf5.scan_file(sample_path)
    serial = _find(result["root"], "/meta/serial")
    calibration = _find(result["root"], "/meta/calibration")
    assert serial["issue"]
    assert calibration["issue"]
    temp = _find(result["root"], "/sensors/temp")
    assert not temp.get("issue")


def test_scan_bad_file_raises(tmp_path):
    bad = tmp_path / "not-hdf5.h5"
    bad.write_bytes(b"this is definitely not hdf5")
    with pytest.raises(hdf5.HdfError):
        hdf5.scan_file(str(bad))


# ---------------------------------------------------------------------------
# validation: pause rules
# ---------------------------------------------------------------------------

def test_time_length_mismatch_pauses(sample_path):
    cfg = {"mode": "array", "array_path": "/problems/short_time", "unit": "s"}
    problems = hdf5.validate_dataset(
        sample_path, "/problems/time_mismatch", cfg
    )
    assert any("不匹配" in p for p in problems)


def test_duplicate_channel_names_pause(sample_path):
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    problems = hdf5.validate_dataset(
        sample_path, "/problems/dup_channels", cfg
    )
    assert any("通道名重复" in p for p in problems)


def test_valid_dataset_passes(sample_path):
    cfg = {"mode": "array", "array_path": "/time", "unit": "s",
           "timezone": "Asia/Shanghai"}
    assert hdf5.validate_dataset(sample_path, "/sensors/temp", cfg) == []


def test_sampled_mode_requires_positive_interval(sample_path):
    cfg = {"mode": "sampled", "start": 0.0, "interval": 0.0}
    problems = hdf5.validate_dataset(
        sample_path, "/sensors/pressure", cfg
    )
    assert any("采样间隔" in p for p in problems)


# ---------------------------------------------------------------------------
# streaming reads: fill values never leak into curves
# ---------------------------------------------------------------------------

def test_fill_values_are_masked_not_plotted(sample_path):
    vals, meta = hdf5.read_value_block(
        sample_path, "/sensors/temp", 0, 600, [2]
    )
    # row 500 carries the -9999 fill marker on channel index 2
    assert vals.mask[500, 0]
    assert vals[500, 0] is np.ma.masked
    assert meta["fill_markers"]  # marker discovery worked
    # NaN gap on channel 1 (rows 100-109)
    vals2, _ = hdf5.read_value_block(
        sample_path, "/sensors/temp", 0, 200, [1]
    )
    assert vals2.mask[100:110, 0].all()


def test_integer_fill_marker_masked(sample_path):
    vals, _ = hdf5.read_value_block(
        sample_path, "/sensors/pressure", 0, 400, [0]
    )
    assert vals.mask[300:305, 0].all()
    assert not vals.mask[0, 0]  # normal row is present


def test_scale_and_offset_applied(sample_path):
    vals, _ = hdf5.read_value_block(
        sample_path, "/sensors/temp", 0, 10, [0]
    )
    raw_expected = None
    import h5py
    with h5py.File(sample_path, "r") as f:
        raw_expected = float(f["/sensors/temp"][0, 0])
    assert vals[0, 0] == pytest.approx(raw_expected * 0.1 + 273.15)


def test_scale_can_be_disabled(sample_path):
    vals, _ = hdf5.read_value_block(
        sample_path, "/sensors/temp", 0, 10, [0], use_scale=False
    )
    import h5py
    with h5py.File(sample_path, "r") as f:
        raw_expected = float(f["/sensors/temp"][0, 0])
    assert vals[0, 0] == pytest.approx(raw_expected)


def test_missing_channel_fully_masked(sample_path):
    vals, _ = hdf5.read_value_block(
        sample_path, "/sensors/vibration", 0, 500, [2]
    )
    assert vals.mask.all()


# ---------------------------------------------------------------------------
# time axes
# ---------------------------------------------------------------------------

def test_non_contiguous_time_has_nat_gap(sample_path):
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    axis = hdf5.build_time_axis(sample_path, cfg, 0, 1000)
    assert np.isnat(axis[777])
    assert not np.isnat(axis[0])


def test_sampled_time_axis(sample_path):
    cfg = {"mode": "sampled", "start": 100.0, "interval": 0.5, "unit": "s"}
    axis = hdf5.build_time_axis(sample_path, cfg, 0, 5)
    expected = (np.array([100, 100.5, 101, 101.5, 102]) * 1e9).astype(
        "datetime64[ns]"
    )
    np.testing.assert_array_equal(axis, expected)


def test_timezone_formatting(sample_path):
    cfg = {"mode": "sampled", "start": 0.0, "interval": 3600.0,
           "unit": "s", "timezone": "Asia/Shanghai"}
    res = hdf5.preview(
        sample_path, "/sensors/pressure", [0], ["p"], cfg, 0, 3, 10
    )
    # epoch + 0/1/2h in UTC, displayed at +08:00
    assert res.times[0].endswith("+08:00")
    assert res.times[0].startswith("1970-01-01T08:00:00")


# ---------------------------------------------------------------------------
# preview: chunked + decimated, never whole-file
# ---------------------------------------------------------------------------

def test_preview_decimates_and_keeps_nulls(sample_path):
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    res = hdf5.preview(
        sample_path, "/sensors/temp", [0, 1], ["temp_A", "temp_B"],
        cfg, 0, 2000, max_points=200,
    )
    assert res.returned <= 200
    assert res.decimated is True
    assert len(res.times) == res.returned
    # every null in the series corresponds to a masked/missing sample
    assert any(v is None for v in res.series["temp_B"])


def test_preview_does_not_load_entire_file(sample_path, monkeypatch):
    """A contiguous full-axis read means the whole file was materialized;
    strided hyperslabs (step>1) are the allowed decimation path."""
    import h5py

    real_getitem = h5py.Dataset.__getitem__
    full_reads = []
    elements_read = {"n": 0}

    def guard(self, args):
        if isinstance(self, h5py.Dataset) and isinstance(args, slice):
            start, stop, step = args.indices(10**12)
            step = step or 1
            elements_read["n"] += max(0, (stop - start + step - 1) // step)
            if step == 1 and stop - start >= self.shape[0] and self.shape[0] > 100:
                full_reads.append(self.name)
        return real_getitem(self, args)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", guard)
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    hdf5.preview(
        sample_path, "/sensors/temp", [0], ["temp_A"], cfg, 100, 300, 50
    )
    assert full_reads == [], f"发生了整文件连续读取: {full_reads}"
    # 200-row range decimated to <=50 points per dataset (values + time)
    assert elements_read["n"] <= 120, elements_read["n"]


# ---------------------------------------------------------------------------
# import: dedup, config versions, diffs, provenance
# ---------------------------------------------------------------------------

def _register(db, sample_path):
    file_id = db.insert_file("sample.h5", sample_path, "abc", 1)
    scan = hdf5.scan_file(sample_path)
    temp = _find(scan["root"], "/sensors/temp")
    ds_id = db.upsert_dataset(
        file_id, temp["path"], temp["shape"], temp["dtype"],
        temp["channel_names"], "active", [],
    )
    return db.get_file(file_id), db.get_dataset(ds_id)


def test_import_then_reimport_adds_no_duplicates(db, sample_path):
    file_row, ds_row = _register(db, sample_path)
    cfg = {"mode": "array", "array_path": "/time", "unit": "s",
           "timezone": "Asia/Shanghai", "use_scale": True}
    r1 = importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg,
        start=0, stop=100, channels=[0, 1],
    )
    assert r1["status"] == "imported"
    assert r1["counts"]["inserted"] == 200
    total_after_first = db.count_readings(ds_row["id"])
    assert total_after_first == 200

    r2 = importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg,
        start=0, stop=100, channels=[0, 1],
    )
    assert r2["status"] == "duplicate"
    assert db.count_readings(ds_row["id"]) == total_after_first


def test_changed_config_creates_version_and_diff(db, sample_path):
    file_row, ds_row = _register(db, sample_path)
    cfg1 = {"mode": "array", "array_path": "/time", "unit": "s",
            "use_scale": True}
    importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg1,
        start=0, stop=50, channels=[0],
    )
    cfg2 = dict(cfg1, use_scale=False)  # changed config
    r = importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg2,
        start=0, stop=50, channels=[0],
    )
    assert r["config_version"] == 2
    assert r["diff"] is not None
    fields = {c["field"] for c in r["diff"]["changes"]}
    assert "use_scale" in fields
    # still the same 50 rows (natural key), values updated rather than duplicated
    assert db.count_readings(ds_row["id"]) == 50
    assert r["counts"]["updated"] >= 1

    versions = db.list_configs(ds_row["id"])
    assert [v["version"] for v in versions] == [1, 2]


def test_reading_provenance_points_back_to_source(db, sample_path):
    file_row, ds_row = _register(db, sample_path)
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg,
        start=10, stop=13, channels=[2],
    )
    rows = db.query_readings(ds_row["id"])
    r0 = rows[0]
    assert r0["source_dataset_path"] == "/sensors/temp"
    assert r0["array_index"] == 10
    assert r0["channel_index"] == 2
    assert r0["timestamp_utc"] is not None
    assert r0["config_version"] == 1


def test_import_paused_dataset_rejected(db, sample_path):
    file_row, ds_row = _register(db, sample_path)
    bad_cfg = {"mode": "array",
               "array_path": "/problems/short_time", "unit": "s"}
    with pytest.raises(hdf5.HdfError):
        importer.import_slice(
            db, file_row=file_row, dataset_row=ds_row, config=bad_cfg,
            start=0, stop=10, channels=[0],
        )
    assert db.get_dataset(ds_row["id"])["status"] == "paused"


def test_import_streams_blocks_not_whole_file(db, sample_path, monkeypatch):
    import h5py

    real_getitem = h5py.Dataset.__getitem__
    full_reads = []

    def guard(self, args):
        if isinstance(self, h5py.Dataset) and isinstance(args, slice):
            start, stop, _ = args.indices(10**12)
            if stop - start >= self.shape[0] and self.shape[0] > 100:
                full_reads.append(self.name)
        return real_getitem(self, args)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", guard)
    file_row, ds_row = _register(db, sample_path)
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg,
        start=0, stop=400, channels=[0, 1],
    )
    assert full_reads == [], f"导入时发生了整文件读取: {full_reads}"


def test_missing_values_stored_as_missing_not_numbers(db, sample_path):
    file_row, ds_row = _register(db, sample_path)
    cfg = {"mode": "array", "array_path": "/time", "unit": "s"}
    importer.import_slice(
        db, file_row=file_row, dataset_row=ds_row, config=cfg,
        start=495, stop=505, channels=[2],
    )
    rows = db.query_readings(ds_row["id"])
    row_500 = next(r for r in rows if r["array_index"] == 500)
    assert row_500["is_missing"] == 1
    assert row_500["value"] is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find(node, path):
    if node.get("path") == path:
        return node
    for c in node.get("children", []):
        found = _find(c, path)
        if found:
            return found
    return None
