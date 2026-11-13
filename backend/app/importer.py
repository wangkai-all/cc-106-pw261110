"""Import orchestration: validate, stream blocks into the unified table."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import hdf5
from .db import Database

BLOCK_SIZE = 8192


def _to_utc_iso(t: np.datetime64 | None) -> str | None:
    if t is None or np.isnat(t):
        return None
    secs = t.astype("datetime64[ns]").astype("int64") / 1e9
    return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()


def config_diff(db: Database, dataset_id: int,
                new_norm: dict[str, Any]) -> dict[str, Any] | None:
    """Compare the incoming normalized config with the previous version."""
    prev = db.previous_config(dataset_id)
    if prev is None:
        return None
    old = json.loads(prev["config_json"])
    keys = sorted(set(old) | set(new_norm))
    changes = []
    for k in keys:
        ov, nv = old.get(k), new_norm.get(k)
        if ov != nv:
            changes.append({"field": k, "old": ov, "new": nv})
    if not changes:
        return None
    return {
        "previous_version": prev["version"],
        "changes": changes,
    }


def import_slice(db: Database, *, file_row: Any, dataset_row: Any,
                 config: dict[str, Any], start: int, stop: int,
                 channels: list[int]) -> dict[str, Any]:
    """Import rows [start, stop) x channels.

    Dedup rule: the natural key (dataset, channel, array_index) is unique.
    Re-importing the same slice with the same config adds nothing.  A changed
    config creates a new version and the per-row diff (insert/updated/unchanged)
    is counted and reported.
    """
    dataset_id = int(dataset_row["id"])
    file_id = int(file_row["id"])
    stored_path = file_row["stored_path"]
    dataset_path = dataset_row["dataset_path"]
    all_names = json.loads(dataset_row["channel_names"])
    names = [all_names[c] for c in channels]

    # 1) validation — paused datasets cannot be imported
    problems = hdf5.validate_dataset(stored_path, dataset_path, config)
    if problems:
        db.upsert_dataset(
            file_id, dataset_path,
            json.loads(dataset_row["shape"]), dataset_row["dtype"],
            all_names, "paused", problems,
        )
        db.commit()
        raise hdf5.HdfError("; ".join(problems))

    # 2) config versioning + diff against previous config
    norm = db.normalize_config(config)
    config_id, version, created = db.get_or_create_config(dataset_id, config)
    diff = config_diff(db, dataset_id, norm) if created else None

    # 3) identical slice + identical config => no new readings at all
    identical = db.find_identical_job(config_id, start, stop, channels)

    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "missing": 0}
    if identical is None:
        use_scale = bool(config.get("use_scale", True))
        config_fill = config.get("fill_value")
        try:
            # Stream time and value blocks in lockstep so only one block of
            # each is in memory at a time — never the whole slice.
            time_stream = hdf5.iter_time_blocks(
                stored_path, config, start, stop, BLOCK_SIZE
            )
            value_stream = hdf5.iter_value_blocks(
                stored_path, dataset_path, start, stop, channels,
                BLOCK_SIZE, config_fill=config_fill, use_scale=use_scale,
            )
            for (t_start, tarr), (blk_start, vals) in zip(
                time_stream, value_stream
            ):
                assert t_start == blk_start
                for row in range(vals.shape[0]):
                    arr_index = blk_start + row
                    ts = _to_utc_iso(tarr[row])
                    for col, ch_idx in enumerate(channels):
                        is_missing = bool(vals.mask[row, col])
                        raw = None
                        value = None
                        if not is_missing:
                            value = float(vals[row, col])
                        outcome = db.upsert_reading(
                            dataset_id=dataset_id,
                            channel_index=ch_idx,
                            channel_name=names[col],
                            source_file_id=file_id,
                            source_dataset_path=dataset_path,
                            array_index=arr_index,
                            timestamp_utc=ts,
                            raw_value=raw,
                            value=value,
                            is_missing=is_missing,
                            config_id=config_id,
                            config_version=version,
                        )
                        counts[outcome] += 1
                        if is_missing:
                            counts["missing"] += 1
        except Exception:
            db.rollback()
            raise

    status = "duplicate" if identical is not None else (
        "updated" if diff else "imported"
    )
    job_id = db.insert_job(
        config_id=config_id,
        dataset_id=dataset_id,
        index_start=start,
        index_stop=stop,
        channels_json=json.dumps(channels),
        status=status,
        inserted=counts["inserted"],
        updated=counts["updated"],
        skipped_duplicates=counts["unchanged"],
        diff_json=json.dumps(diff, ensure_ascii=False) if diff else None,
        error=None,
    )
    db.commit()
    return {
        "job_id": job_id,
        "status": status,
        "config_version": version,
        "config_created": created,
        "diff": diff,
        "counts": counts,
        "slice": {"start": start, "stop": stop, "channels": channels,
                  "channel_names": names},
    }
