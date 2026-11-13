"""End-to-end API smoke test against a running server (stdlib only).

Run with the server up:  .venv/bin/python -I backend/scripts/e2e_check.py
"""
import json
import sys
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8077"


def call(method, path, payload=None, raw=None, ctype="application/json"):
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = ctype
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if not cond:
        check.failed += 1
check.failed = 0


# ---- multipart upload without third-party libs ----
def multipart_upload(file_path):
    boundary = "----e2e" + uuid.uuid4().hex
    content = Path(file_path).read_bytes()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{Path(file_path).name}"\r\n'.encode(),
        b"Content-Type: application/x-hdf5\r\n\r\n",
        content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return call("POST", "/api/files", raw=body,
                ctype=f"multipart/form-data; boundary={boundary}")


status, up = multipart_upload("data/sample.h5")
check("upload returns 200", status == 200, str(status))
fid = up["file_id"]
paths = []
def walk(n):
    if n["type"] == "dataset":
        paths.append(n["path"])
    for c in n.get("children", []):
        walk(c)
walk(up["tree"])
check("tree contains temp dataset", "/sensors/temp" in paths)
check("time axis excluded from suggestions",
      "/time" not in up["time_suggestions"])
check("pressure time unit defaults to s (not hPa)",
      up["time_suggestions"]["/sensors/pressure"]["unit"] == "s")

# ---- validation: paused datasets ----
status, r = call("POST", "/api/datasets/validate", {
    "file_id": fid, "dataset_path": "/problems/time_mismatch",
    "time_config": {"mode": "array", "array_path": "/problems/short_time",
                    "unit": "s"}})
check("mismatch dataset paused", r["status"] == "paused" and r["problems"])

status, r = call("POST", "/api/datasets/validate", {
    "file_id": fid, "dataset_path": "/problems/dup_channels",
    "time_config": {"mode": "array", "array_path": "/time", "unit": "s"}})
check("dup-channel dataset paused", r["status"] == "paused")

status, r = call("POST", "/api/datasets/validate", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": {"mode": "array", "array_path": "/time", "unit": "s",
                    "timezone": "Asia/Shanghai"}})
check("valid dataset stays active", r["status"] == "active" and not r["problems"])

# ---- preview: nulls + decimation ----
status, pv = call("POST", "/api/preview", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": {"mode": "array", "array_path": "/time", "unit": "s",
                    "timezone": "Asia/Shanghai"},
    "channels": [0, 1, 2], "start": 0, "stop": 2000, "max_points": 400})
check("preview 200", status == 200, str(pv)[:120])
check("preview decimated", pv["returned"] <= 400 and pv["decimated"])
check("nulls present for missing samples",
      any(v is None for v in pv["series"]["temp_B"]))
check("fill row excluded from channel C values",
      all(v is None or isinstance(v, (int, float))
          for v in pv["series"]["temp_C"]))

# preview a paused dataset must be refused
status, pv_bad = call("POST", "/api/preview", {
    "file_id": fid, "dataset_path": "/problems/dup_channels",
    "time_config": {"mode": "array", "array_path": "/time", "unit": "s"},
    "channels": [0], "start": 0, "stop": 100})
check("paused dataset preview refused 409", status == 409)

# preview a 1D dataset whose time axis is derived (start + interval)
status, pv1d = call("POST", "/api/preview", {
    "file_id": fid, "dataset_path": "/sensors/pressure",
    "time_config": {"mode": "sampled", "start": 1700000000.0,
                    "interval": 0.5, "unit": "s", "timezone": "+08:00"},
    "channels": [0], "start": 0, "stop": 400, "max_points": 200})
check("1D sampled-mode preview 200", status == 200)
check("1D series decimated and named",
      pv1d["returned"] <= 200 and "pressure" in pv1d["series"])
check("integer fill rows arrive as null",
      any(v is None for v in pv1d["series"]["pressure"]))

# ---- import + dedup ----
cfg = {"mode": "array", "array_path": "/time", "unit": "s",
       "timezone": "Asia/Shanghai", "use_scale": True}
status, i1 = call("POST", "/api/import", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": cfg, "channels": [0, 1], "start": 0, "stop": 300})
check("first import", status == 200 and i1["counts"]["inserted"] == 600,
      json.dumps(i1.get("counts", {})))

status, i2 = call("POST", "/api/import", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": cfg, "channels": [0, 1], "start": 0, "stop": 300})
check("re-import same slice = duplicate, zero inserts",
      i2["status"] == "duplicate" and i2["counts"]["inserted"] == 0)

# ---- changed config -> version + diff ----
cfg2 = dict(cfg, use_scale=False)
status, i3 = call("POST", "/api/import", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": cfg2, "channels": [0, 1], "start": 0, "stop": 300})
check("changed config creates v2 with diff",
      i3["config_version"] == 2 and i3["diff"] is not None
      and any(c["field"] == "use_scale" for c in i3["diff"]["changes"]))

# ---- readings + provenance ----
status, ds_list = call("GET", f"/api/datasets?file_id={fid}")
ds_by_path = {d["dataset_path"]: d["id"] for d in ds_list}
temp_id = ds_by_path["/sensors/temp"]
pressure_id = ds_by_path["/sensors/pressure"]
check("measurable datasets registered",
      "/sensors/temp" in ds_by_path and "/sensors/pressure" in ds_by_path)
check("label/string arrays not registered",
      "/sensors/labels" not in ds_by_path and "/meta/serial" not in ds_by_path)

status, rd = call("GET", "/api/readings?limit=10")
check("readings reachable", status == 200 and rd["total"] == 600)
item = rd["items"][0]
check("provenance fields present",
      item["source_dataset_path"] == "/sensors/temp"
      and item["array_index"] is not None
      and item["source_file_id"] == fid)

# the modal resolves source_file_id back to the original uploaded file
status, files_now = call("GET", "/api/files")
file_ids = {f["id"] for f in files_now}
check("reading source_file_id resolves to an uploaded file",
      all(r["source_file_id"] in file_ids for r in rd["items"]))

# ---- sampled-mode import (pressure) ----
scfg = {"mode": "sampled", "start": 1700000000.0, "interval": 0.5,
        "unit": "s", "timezone": "+08:00", "use_scale": True}
status, sp = call("POST", "/api/import", {
    "file_id": fid, "dataset_path": "/sensors/pressure",
    "time_config": scfg, "channels": [0], "start": 0, "stop": 400})
check("sampled-mode import", status == 200 and sp["counts"]["inserted"] == 400,
      json.dumps(sp)[:200])
status, rd2 = call("GET",
                   f"/api/readings?limit=1&dataset_id={pressure_id}")
check("sampled timestamps generated",
      rd2["items"] and rd2["items"][0]["timestamp_utc"] is not None)

# ---- missing values stored as missing ----
status, mv = call("POST", "/api/import", {
    "file_id": fid, "dataset_path": "/sensors/temp",
    "time_config": cfg, "channels": [2], "start": 495, "stop": 505})
status, rd3 = call(
    "GET", f"/api/readings?limit=5000&dataset_id={temp_id}")
row500 = next(x for x in rd3["items"]
              if x["channel_index"] == 2 and x["array_index"] == 500)
check("fill value stored as missing, not number",
      row500["is_missing"] == 1 and row500["value"] is None)

# ---- config versions endpoint ----
status, cfgs = call("GET", f"/api/datasets/{temp_id}/configs")
check("config versions retained",
      [c["version"] for c in cfgs] == [1, 2])

print()
print("FAILURES:", check.failed)
sys.exit(1 if check.failed else 0)
