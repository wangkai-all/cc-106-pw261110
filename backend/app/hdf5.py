"""HDF5 inspection and chunked reading.

All file access is read-only and streaming: nothing ever reads a whole dataset
into memory just to display it.  Public functions in this module are the only
place that opens HDF5 files.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import h5py
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Attribute names that conventionally point at the companion time array.
TIME_REF_ATTRS = ("time_ref", "time_dataset", "TIME_REF", "coordinates")
# Attribute names carrying a fill / missing-value marker.
FILL_ATTRS = ("_FillValue", "missing_value", "fill_value", "FILLVAL")
# Attribute names for scale / offset.
SCALE_ATTRS = ("scale_factor", "scale", "slope")
OFFSET_ATTRS = ("add_offset", "offset", "intercept")
# Attribute name for units.
UNIT_ATTRS = ("units", "unit", "UNIT")
# Attribute name for time zone (IANA name or fixed offset like "+08:00").
TZ_ATTRS = ("timezone", "tz", "TIMEZONE")

# Time units we understand.  Values map to a numpy timedelta64 base unit.
TIME_UNITS = ("ns", "us", "ms", "s", "min", "h", "d")
_TIME_UNIT_NS = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "min": 60 * 1_000_000_000,
    "h": 3600 * 1_000_000_000,
    "d": 86400 * 1_000_000_000,
}


class HdfError(Exception):
    """Any expected failure while working with an HDF5 file."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _attr_value(attrs: h5py.AttributeManager, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in attrs:
            val = attrs[name]
            if isinstance(val, np.ndarray):
                val = val.item() if val.shape == () else val.tolist()
            if isinstance(val, bytes):
                val = val.decode("utf-8", "replace")
            if isinstance(val, np.generic):
                val = val.item()
            return val
    return None


def _all_attrs(attrs: h5py.AttributeManager) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if isinstance(v, np.ndarray):
            v = v.tolist() if v.size > 1 else v.item()
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        if isinstance(v, np.generic):
            v = v.item()
        out[k] = v
    return out


def _decode_name(name: Any) -> str:
    if isinstance(name, bytes):
        return name.decode("utf-8", "replace")
    return str(name)


def _looks_like_time_axis(ds: h5py.Dataset) -> bool:
    if ds.ndim != 1:
        return False
    leaf = ds.name.rstrip("/").rsplit("/", 1)[-1].lower()
    if leaf in ("time", "timestamp", "t", "datetime", "short_time"):
        return True
    # referenced as a time axis by anything, or carrying a time-only unit attr
    if _attr_value(ds.attrs, TIME_REF_ATTRS) is not None:
        return True
    unit = _attr_value(ds.attrs, UNIT_ATTRS)
    return isinstance(unit, str) and unit.strip().lower() in TIME_UNITS


def _time_unit(tds: h5py.Dataset) -> str:
    """Time-array unit, validated against the supported unit list."""
    unit = _attr_value(tds.attrs, UNIT_ATTRS)
    if isinstance(unit, str) and unit.strip().lower() in TIME_UNITS:
        return unit.strip().lower()
    return "s"


# ---------------------------------------------------------------------------
# Tree scan
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    type: str  # "group" | "dataset" | "broken_link"
    path: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    # dataset-only fields
    shape: list[int] | None = None
    dtype: str | None = None
    chunks: list[int] | None = None
    compression: str | None = None
    fill_value: Any = None
    length: int | None = None  # size along the time (first) axis
    n_channels: int | None = None
    channel_names: list[str] | None = None
    children: list["NodeInfo"] = field(default_factory=list)
    issue: str | None = None  # soft/external link target missing etc.


def scan_file(path: str) -> dict[str, Any]:
    """Walk an HDF5 file and return a serializable group/dataset tree."""
    try:
        with h5py.File(path, "r") as f:
            root = _scan_group(f, f)
            suggested = _suggest_time_configs(f)
            return {
                "root": _node_to_dict(root),
                "time_suggestions": suggested,
            }
    except OSError as exc:  # corrupt / non-HDF5 file
        raise HdfError(f"无法打开 HDF5 文件: {exc}") from exc


def _node_to_dict(node: NodeInfo) -> dict[str, Any]:
    d = {
        "type": node.type,
        "path": node.path,
        "name": node.name,
        "attrs": node.attrs,
    }
    if node.type == "dataset":
        d.update(
            shape=node.shape,
            dtype=node.dtype,
            chunks=node.chunks,
            compression=node.compression,
            fill_value=node.fill_value,
            length=node.length,
            n_channels=node.n_channels,
            channel_names=node.channel_names,
        )
    if node.issue:
        d["issue"] = node.issue
    if node.children:
        d["children"] = [_node_to_dict(c) for c in node.children]
    return d


def _scan_group(f: h5py.File, g: h5py.Group) -> NodeInfo:
    info = NodeInfo(type="group", path=g.name, name=_leaf_name(g.name),
                    attrs=_all_attrs(g.attrs))
    for key in g.keys():
        try:
            item = g[key]
        except (KeyError, OSError) as exc:  # dangling external/soft link
            info.children.append(NodeInfo(
                type="broken_link",
                path=f"{g.name.rstrip('/')}/{key}",
                name=key,
                issue=f"链接目标不可访问: {exc}",
            ))
            continue
        if isinstance(item, h5py.Group):
            info.children.append(_scan_group(f, item))
        elif isinstance(item, h5py.Dataset):
            info.children.append(_scan_dataset(item))
    return info


def _leaf_name(path: str) -> str:
    if path == "/":
        return "/"
    return path.rstrip("/").rsplit("/", 1)[-1]


def _scan_dataset(ds: h5py.Dataset) -> NodeInfo:
    shape = list(ds.shape)
    ndim = len(shape)
    n_channels: int | None = None
    channel_names: list[str] | None = None
    if ndim == 1:
        n_channels = 1
        channel_names = [_leaf_name(ds.name)]
    elif ndim == 2:
        n_channels = shape[1]
        channel_names = _resolve_channel_names(ds, n_channels)
    fill = ds.fillvalue
    if isinstance(fill, np.generic):
        fill = fill.item()
    chunks = list(ds.chunks) if ds.chunks else None
    # Only numeric 1D/2D arrays can be previewed/imported as measurements.
    measurable = ds.ndim in (1, 2) and ds.dtype.kind in ("i", "u", "f", "b")
    return NodeInfo(
        type="dataset",
        path=ds.name,
        name=_leaf_name(ds.name),
        attrs=_all_attrs(ds.attrs),
        shape=shape,
        dtype=str(ds.dtype),
        chunks=chunks,
        compression=ds.compression,
        fill_value=fill,
        length=shape[0] if shape else None,
        n_channels=n_channels,
        channel_names=channel_names,
        issue=None if measurable else "非数值或非标量维度数组，仅可浏览，不能预览/导入",
    )


def _resolve_channel_names(ds: h5py.Dataset, n: int) -> list[str]:
    """Look for a per-channel label array; fall back to ch_1..ch_n."""
    labels_attr = _attr_value(ds.attrs, ("channel_names", "labels", "columns"))
    if isinstance(labels_attr, list) and len(labels_attr) == n:
        return [_decode_name(x) for x in labels_attr]
    # A "labels" dataset in the parent group is another common convention.
    parent = ds.parent
    for candidate in ("channel_names", "labels", "channels"):
        if candidate in parent and isinstance(parent[candidate], h5py.Dataset):
            lab_ds = parent[candidate]
            if lab_ds.shape == (n,) or (lab_ds.ndim == 1 and lab_ds.shape[0] == n):
                return [_decode_name(x) for x in lab_ds[...].tolist()]
    return [f"ch_{i + 1}" for i in range(n)]


# ---------------------------------------------------------------------------
# Time configuration inference
# ---------------------------------------------------------------------------

def _suggest_time_configs(f: h5py.File) -> dict[str, dict[str, Any]]:
    """For every numeric dataset, suggest how its time axis may be built."""
    suggestions: dict[str, dict[str, Any]] = {}

    def visit(name: str, obj: h5py.HLObject) -> None:
        if not isinstance(obj, h5py.Dataset) or obj.ndim == 0:
            return
        ds: h5py.Dataset = obj
        # The time axis itself is not a measurement dataset to configure.
        if _looks_like_time_axis(ds):
            return
        cfg: dict[str, Any] = {"mode": None}
        # 1) explicit time reference attribute
        ref = _attr_value(ds.attrs, TIME_REF_ATTRS)
        if isinstance(ref, str) and ref in f and isinstance(f[ref], h5py.Dataset):
            tds = f[ref]
            if tds.ndim == 1 and tds.shape[0] == ds.shape[0]:
                cfg = {
                    "mode": "array",
                    "array_path": ref,
                    "unit": _time_unit(tds),
                    "timezone": _attr_value(tds.attrs, TZ_ATTRS),
                }
        # 2) sibling dataset named like a time axis
        if cfg["mode"] is None:
            parent = ds.parent
            for tname in ("time", "timestamp", "t", "datetime"):
                if tname in parent and isinstance(parent[tname], h5py.Dataset):
                    tds = parent[tname]
                    if tds.ndim == 1 and tds.shape[0] == ds.shape[0]:
                        cfg = {
                            "mode": "array",
                            "array_path": tds.name,
                            "unit": _time_unit(tds),
                            "timezone": _attr_value(tds.attrs, TZ_ATTRS),
                        }
                        break
        # 3) start + interval attributes.  The time unit lives in dedicated
        #    attributes (time_unit/unit_time); the dataset's own "units" is the
        #    measurement unit and must not be reused for time.
        if cfg["mode"] is None:
            start = _attr_value(ds.attrs, ("start_time", "t0", "start"))
            interval = _attr_value(ds.attrs, ("sample_interval", "dt", "interval"))
            if start is not None and interval is not None:
                cfg = {
                    "mode": "sampled",
                    "start": float(start),
                    "interval": float(interval),
                    "unit": _attr_value(ds.attrs,
                                        ("time_unit", "unit_time")) or "s",
                    "timezone": _attr_value(ds.attrs, TZ_ATTRS),
                }
        if cfg["mode"] is not None:
            suggestions[ds.name] = cfg

    f.visititems(visit)
    return suggestions


# ---------------------------------------------------------------------------
# Validation (pausing rules)
# ---------------------------------------------------------------------------

def validate_dataset(path: str, dataset_path: str, time_config: dict[str, Any]) -> list[str]:
    """Return a list of blocking problems.  Empty list => usable."""
    problems: list[str] = []
    with h5py.File(path, "r") as f:
        if dataset_path not in f or not isinstance(f[dataset_path], h5py.Dataset):
            return [f"数据集不存在: {dataset_path}"]
        ds = f[dataset_path]
        if ds.ndim not in (1, 2):
            problems.append(f"仅支持 1D/2D 数组，当前为 {ds.ndim}D")
            return problems
        n = ds.shape[0]

        # time axis length must match the data length
        mode = time_config.get("mode")
        if mode == "array":
            ap = time_config["array_path"]
            if ap not in f or not isinstance(f[ap], h5py.Dataset):
                problems.append(f"时间数组不存在: {ap}")
            elif f[ap].shape[0] != n:
                problems.append(
                    f"时间轴长度 {f[ap].shape[0]} 与数据长度 {n} 不匹配，已暂停该数据集"
                )
        elif mode == "sampled":
            interval = float(time_config.get("interval", 0))
            if interval <= 0:
                problems.append("采样间隔必须为正数")
        else:
            problems.append("未指定时间方式（独立数组 或 起始时间+采样间隔）")

        # duplicate channel names pause the dataset
        if ds.ndim == 2:
            names = _resolve_channel_names(ds, ds.shape[1])
            dup = sorted({x for x in names if names.count(x) > 1})
            if dup:
                problems.append(f"通道名重复: {', '.join(dup)}，已暂停该数据集")
    return problems


# ---------------------------------------------------------------------------
# Time axis construction (streaming, block by block)
# ---------------------------------------------------------------------------

def _unit_ns(unit: str) -> int:
    if unit not in _TIME_UNIT_NS:
        raise HdfError(f"不支持的时间单位: {unit}（支持 {', '.join(TIME_UNITS)}）")
    return _TIME_UNIT_NS[unit]


def iter_time_blocks(
    path: str,
    time_config: dict[str, Any],
    start: int,
    stop: int,
    block_size: int,
) -> Iterator[tuple[int, np.ndarray]]:
    """Yield (block_start_index, datetime64[ns] array) for indices [start, stop).

    Non-contiguous / missing timestamps produce NaT entries rather than being
    silently interpolated.
    """
    mode = time_config["mode"]
    unit = time_config.get("unit", "s")
    unit_ns = _unit_ns(unit)
    if mode == "sampled":
        t0 = float(time_config["start"]) * unit_ns
        dt = float(time_config["interval"]) * unit_ns
        for blk_start in range(start, stop, block_size):
            blk_stop = min(blk_start + block_size, stop)
            idx = np.arange(blk_start, blk_stop, dtype=np.float64)
            ns = t0 + idx * dt
            yield blk_start, ns.astype("datetime64[ns]")
    elif mode == "array":
        array_path = time_config["array_path"]
        with h5py.File(path, "r") as f:
            tds = f[array_path]
            for blk_start in range(start, stop, block_size):
                blk_stop = min(blk_start + block_size, stop)
                raw = tds[blk_start:blk_stop]
                ns = np.asarray(raw, dtype=np.float64) * unit_ns
                # NaN / inf -> NaT (missing timestamp in a non-contiguous axis)
                dt64 = np.full(ns.shape, np.datetime64("NaT"), dtype="datetime64[ns]")
                finite = np.isfinite(ns)
                dt64[finite] = ns[finite].astype("datetime64[ns]")
                yield blk_start, dt64
    else:
        raise HdfError(f"未知时间方式: {mode}")


def build_time_axis(
    path: str, time_config: dict[str, Any], start: int, stop: int
) -> np.ndarray:
    """Materialize the time axis for one (small) range — used by preview/import."""
    n = stop - start
    block = max(1024, min(n, 65536))
    out = np.empty(n, dtype="datetime64[ns]")
    for blk_start, vals in iter_time_blocks(path, time_config, start, stop, block):
        out[blk_start - start: blk_start - start + len(vals)] = vals
    return out


# ---------------------------------------------------------------------------
# Value reading with fill-value / scale / offset handling
# ---------------------------------------------------------------------------

def _fill_markers(ds: h5py.Dataset, config_fill: Any) -> list[float]:
    markers: list[float] = []
    for attr in FILL_ATTRS:
        if attr in ds.attrs:
            v = ds.attrs[attr]
            if isinstance(v, np.ndarray):
                v = v.item() if v.shape == () else v.tolist()
            if isinstance(v, (int, float, np.generic)):
                markers.append(float(v))
    if config_fill is not None:
        try:
            markers.append(float(config_fill))
        except (TypeError, ValueError):
            pass
    if ds.fillvalue is not None:
        try:
            markers.append(float(ds.fillvalue))
        except (TypeError, ValueError):
            pass
    # de-duplicate
    return list(dict.fromkeys(markers))


def _mask_and_scale(raw: np.ndarray, ds: h5py.Dataset,
                    config_fill: Any, use_scale: bool) -> np.ma.MaskedArray:
    """Turn a raw hyperslab into a masked float64 array.

    Fill markers (HDF5 fill value, _FillValue/missing_value attrs, or a
    confirmed config value) and NaNs are masked — never returned as data.
    """
    scale = _attr_value(ds.attrs, SCALE_ATTRS) if use_scale else None
    offset = _attr_value(ds.attrs, OFFSET_ATTRS) if use_scale else None
    scale = float(scale) if scale is not None else 1.0
    offset = float(offset) if offset is not None else 0.0
    markers = _fill_markers(ds, config_fill)

    work = np.ma.asarray(raw)
    base_mask = (work.mask if np.ma.is_masked(work)
                 else np.zeros(work.shape, dtype=bool))
    work = np.ma.MaskedArray(
        work.astype(np.float64, copy=True),
        mask=np.array(base_mask, dtype=bool, copy=True),
    )
    for marker in markers:
        work.mask |= np.isclose(work.data, marker, rtol=0.0, atol=0.0,
                                equal_nan=True)
    work.mask |= np.isnan(work.data)
    if use_scale and (scale != 1.0 or offset != 0.0):
        work = work * scale + offset
    return work


def read_value_block(
    path: str,
    dataset_path: str,
    start: int,
    stop: int,
    channels: list[int],
    *,
    config_fill: Any = None,
    use_scale: bool = True,
) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    """Read contiguous rows [start, stop) for the given channel indices.

    Returns a masked float64 array shaped (stop-start, len(channels)); masked
    positions are missing values (fill markers or HDF5-level mask).  Fill
    values are NEVER returned as real measurements.
    """
    with h5py.File(path, "r") as f:
        ds = f[dataset_path]
        if ds.ndim == 1:
            raw = ds[start:stop]
            raw = raw.reshape(-1, 1)
        else:
            raw = ds[start:stop, channels]
        work = _mask_and_scale(raw, ds, config_fill, use_scale)
        meta = {
            "scale": _attr_value(ds.attrs, SCALE_ATTRS) or 1.0,
            "offset": _attr_value(ds.attrs, OFFSET_ATTRS) or 0.0,
            "fill_markers": _fill_markers(ds, config_fill),
        }
    return work, meta


def read_strided(
    path: str,
    dataset_path: str,
    start: int,
    stop: int,
    stride: int,
    channels: list[int],
    *,
    config_fill: Any = None,
    use_scale: bool = True,
    block_size: int = 65536,
) -> np.ma.MaskedArray:
    """Read only the decimated rows start, start+stride, ... via HDF5 strided
    hyperslabs, block by block.  The full range is never materialized."""
    n_picked = math.ceil((stop - start) / stride)
    out_rows: list[np.ma.MaskedArray] = []
    with h5py.File(path, "r") as f:
        ds = f[dataset_path]
        # block by number of *selected* points, so each hyperslab is one
        # contiguous strided selection (no split in the middle of a stride).
        for blk in range(0, n_picked, block_size):
            blk_start = start + blk * stride
            blk_stop = min(blk_start + block_size * stride, stop)
            if ds.ndim == 1:
                raw = ds[blk_start:blk_stop:stride]
                raw = raw.reshape(-1, 1)
            else:
                raw = ds[blk_start:blk_stop:stride, channels]
            out_rows.append(_mask_and_scale(raw, ds, config_fill, use_scale))
    combined = np.ma.concatenate(out_rows, axis=0)
    # concatenate collapses all-False masks back to scalar nomask; force a
    # real 2-D boolean mask so callers can index vals.mask[row, col].
    mask = combined.mask
    if not isinstance(mask, np.ndarray):
        mask = np.zeros(combined.shape, dtype=bool)
    return np.ma.MaskedArray(combined.data, mask=mask)


def read_strided_time(
    path: str,
    time_config: dict[str, Any],
    start: int,
    stop: int,
    stride: int,
    block_size: int = 65536,
) -> np.ndarray:
    """datetime64[ns] axis over the decimated (strided) rows only."""
    picks = np.arange(start, stop, stride, dtype=np.float64)
    unit_ns = _unit_ns(time_config.get("unit", "s"))
    if time_config["mode"] == "sampled":
        ns = (float(time_config["start"]) + picks
              * float(time_config["interval"])) * unit_ns
        return ns.astype("datetime64[ns]")

    array_path = time_config["array_path"]
    n_picked = math.ceil((stop - start) / stride)
    parts: list[np.ndarray] = []
    with h5py.File(path, "r") as f:
        tds = f[array_path]
        for blk in range(0, n_picked, block_size):
            blk_start = start + blk * stride
            blk_stop = min(blk_start + block_size * stride, stop)
            raw = tds[blk_start:blk_stop:stride]
            ns = np.asarray(raw, dtype=np.float64) * unit_ns
            dt64 = np.full(ns.shape, np.datetime64("NaT"),
                           dtype="datetime64[ns]")
            finite = np.isfinite(ns)
            dt64[finite] = ns[finite].astype("datetime64[ns]")
            parts.append(dt64)
    return np.concatenate(parts, axis=0)


def iter_value_blocks(
    path: str,
    dataset_path: str,
    start: int,
    stop: int,
    channels: list[int],
    block_size: int,
    **kwargs: Any,
) -> Iterator[tuple[int, np.ma.MaskedArray]]:
    """Streaming variant of :func:`read_value_block`, block at a time."""
    for blk_start in range(start, stop, block_size):
        blk_stop = min(blk_start + block_size, stop)
        vals, _ = read_value_block(
            path, dataset_path, blk_start, blk_stop, channels, **kwargs
        )
        yield blk_start, vals


# ---------------------------------------------------------------------------
# Preview (down-sampled so the wire payload stays small)
# ---------------------------------------------------------------------------

@dataclass
class PreviewResult:
    times: list[str | None]
    series: dict[str, list[float | None]]
    raw_count: int
    returned: int
    decimated: bool


def preview(
    path: str,
    dataset_path: str,
    channels: list[int],
    channel_names: list[str],
    time_config: dict[str, Any],
    start: int,
    stop: int,
    max_points: int = 1500,
) -> PreviewResult:
    """Read [start, stop) and decimate to at most ``max_points`` per channel.

    Uses strided hyperslabs so only the plotted rows touch the disk; the whole
    dataset is never loaded.
    """
    if stop <= start:
        raise HdfError("预览范围为空")
    n = stop - start
    stride = max(1, math.ceil(n / max_points))

    times = read_strided_time(path, time_config, start, stop, stride)
    vals = read_strided(
        path, dataset_path, start, stop, stride, channels,
        config_fill=time_config.get("fill_value"),
        use_scale=time_config.get("use_scale", True),
    )

    series: dict[str, list[float | None]] = {}
    for col, name in enumerate(channel_names):
        series[name] = [
            None if vals.mask[row, col] else float(vals[row, col])
            for row in range(vals.shape[0])
        ]

    tz = time_config.get("timezone")
    time_str = [_format_time(t, tz) for t in times]
    return PreviewResult(
        times=time_str,
        series=series,
        raw_count=n,
        returned=vals.shape[0],
        decimated=stride > 1,
    )


def _format_time(t: np.datetime64, tz_name: str | None) -> str | None:
    if np.isnat(t):
        return None
    dt = datetime.fromtimestamp(t.astype("datetime64[ns]").astype("int64") / 1e9,
                                tz=timezone.utc)
    if tz_name:
        dt = dt.astimezone(_resolve_tz(tz_name))
    return dt.isoformat()


def _resolve_tz(tz_name: str) -> timezone | Any:
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz_name)
    except Exception:
        # fixed offset like "+08:00" / "UTC+8"
        s = tz_name.replace("UTC", "").strip()
        sign = 1
        if s.startswith(("-", "+")):
            sign = -1 if s[0] == "-" else 1
            s = s[1:]
        parts = s.split(":")
        try:
            hours = int(parts[0])
            mins = int(parts[1]) if len(parts) > 1 else 0
            return timezone(sign * timedelta(hours=hours, minutes=mins))
        except ValueError:
            return timezone.utc
