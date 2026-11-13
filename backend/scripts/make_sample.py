"""Generate a local sample HDF5 file covering every edge case in the spec.

Run:  .venv/bin/python -I -m backend.scripts.make_sample [out_path]

The file is fully self-contained (no network) and includes:
  /sensors/temp          2D gzip dataset, explicit non-contiguous time array,
                         NaN gaps, per-channel labels, scale/offset, fill value
  /sensors/pressure      1D gzip dataset, sampled time (start + interval),
                         integer fill-value markers, fixed-offset timezone
  /sensors/vibration     2D dataset with a missing (all-fill) channel
  /problems/time_mismatch dataset whose time array length does not match
  /problems/dup_channels  dataset whose label array contains duplicate names
  /time, /labels         shared companion arrays
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import h5py
import numpy as np

rng = np.random.default_rng(20261113)
N = 2000


def _non_contiguous_time(n: int) -> np.ndarray:
    """Seconds since epoch with irregular gaps and one NaT (NaN) hole."""
    base = np.sort(rng.uniform(0, 2, size=n).cumsum())  # uneven spacing
    base[777] = np.nan  # missing timestamp -> must appear as a gap, not crash
    return base.astype(np.float64)


def build(path: str) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        # ---- shared time axis (explicit, non-contiguous) ----
        t = _non_contiguous_time(N)
        t_ds = f.create_dataset("time", data=t, compression="gzip")
        t_ds.attrs["units"] = "s"
        t_ds.attrs["timezone"] = "Asia/Shanghai"

        # ---- /sensors group ----
        g = f.create_group("sensors")
        g.attrs["station"] = "SAMPLE-01"

        # 2D temperature: 3 channels, gzip, scale/offset, fill + NaN
        temp = np.empty((N, 3), dtype=np.float32)
        for c in range(3):
            temp[:, c] = (
                20 + 5 * np.sin(np.linspace(0, 12, N) + c)
                + rng.normal(0, 0.2, N)
            )
        temp[100:110, 1] = np.nan            # NaN gap on channel 2
        temp[500, 2] = -9999.0               # explicit fill marker
        labels = np.array(["temp_A", "temp_B", "temp_C"], dtype="S")
        lab_ds = g.create_dataset("labels", data=labels)
        d = g.create_dataset(
            "temp", data=temp, chunks=(256, 3), compression="gzip",
            fillvalue=-9999.0,
        )
        d.attrs["time_ref"] = "/time"
        d.attrs["units"] = "K"
        d.attrs["scale_factor"] = 0.1
        d.attrs["add_offset"] = 273.15
        d.attrs["missing_value"] = -9999.0
        d.attrs["channel_names_ref"] = "/sensors/labels"

        # 1D pressure: sampled time (start + interval), integer fill markers
        pressure = (1013 + rng.normal(0, 2, N)).astype(np.int16)
        pressure[300:305] = -32768           # integer fill markers
        p = g.create_dataset(
            "pressure", data=pressure, chunks=(512,), compression="gzip",
            fillvalue=np.int16(-32768),
        )
        p.attrs["start_time"] = 1_700_000_000.0
        p.attrs["sample_interval"] = 0.5    # seconds
        p.attrs["units"] = "hPa"
        p.attrs["timezone"] = "+08:00"
        p.attrs["missing_value"] = -32768

        # vibration: one channel is entirely missing (all fill)
        vib = rng.normal(0, 1, (N, 2)).astype(np.float32)
        vib_full = np.full((N, 1), -1.0, dtype=np.float32)  # missing channel
        vib = np.concatenate([vib, vib_full], axis=1)
        v = g.create_dataset(
            "vibration", data=vib, chunks=(256, 3), compression="gzip",
            fillvalue=-1.0,
        )
        v.attrs["time_ref"] = "/time"
        v.attrs["missing_value"] = -1.0

        # ---- /problems group: datasets that must be PAUSED ----
        pg = f.create_group("problems")

        # time length mismatch (time array too short)
        bad_time = np.linspace(0, 10, N - 50)
        bt = pg.create_dataset("short_time", data=bad_time, compression="gzip")
        bt.attrs["units"] = "s"
        mismatch = rng.normal(0, 1, (N, 2)).astype(np.float32)
        m = pg.create_dataset(
            "time_mismatch", data=mismatch, chunks=(256, 2),
            compression="gzip",
        )
        m.attrs["time_ref"] = "/problems/short_time"

        # duplicate channel names
        dup_labels = np.array(["x", "x", "y"], dtype="S")
        pg.create_dataset("dup_label_array", data=dup_labels)
        dup_data = rng.normal(0, 1, (N, 3)).astype(np.float32)
        dd = pg.create_dataset(
            "dup_channels", data=dup_data, chunks=(256, 3),
            compression="gzip",
        )
        dd.attrs["time_ref"] = "/time"
        dd.attrs["channel_names"] = ["x", "x", "y"]

        # ---- scalar / non-numeric dataset (should show but not import) ----
        f.attrs["file_format_version"] = "1.0-sample"
        meta = f.create_group("meta")
        meta.create_dataset("serial", data=np.bytes_("SN-0001"))
        meta.create_dataset("calibration", data=np.float64(1.0007))

    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/sample.h5"
    p = build(out)
    print(f"sample written: {p}")
