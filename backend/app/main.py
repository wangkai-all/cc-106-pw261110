"""FastAPI application: HDF5 upload, inspection, preview, import, readings."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, hdf5, importer
from .db import Database

app = FastAPI(title="HDF5 测量数据导入", version="1.0.0")
db = Database(config.DB_PATH)


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------

class TimeConfig(BaseModel):
    mode: str = Field(pattern="^(array|sampled)$")
    array_path: str | None = None
    unit: str | None = "s"
    timezone: str | None = None
    start: float | None = None
    interval: float | None = None
    fill_value: float | None = None
    use_scale: bool = True
    scale: float | None = None
    offset: float | None = None


class ValidateBody(BaseModel):
    file_id: int
    dataset_path: str
    time_config: TimeConfig


class PreviewBody(ValidateBody):
    channels: list[int]
    start: int = Field(ge=0)
    stop: int = Field(ge=1)
    max_points: int = 1500


class ImportBody(PreviewBody):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require_file(file_id: int):
    row = db.get_file(file_id)
    if row is None:
        raise HTTPException(404, f"文件不存在: {file_id}")
    return row


def _require_dataset(file_id: int, dataset_path: str):
    row = db.find_dataset(file_id, dataset_path)
    if row is None:
        raise HTTPException(404, f"数据集未登记: {dataset_path}")
    return row


def _row_to_dict(row) -> dict[str, Any]:
    return dict(row)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".h5", ".hdf5", ".hdf")):
        raise HTTPException(400, "仅接受 .h5/.hdf5/.hdf 文件")
    digest = hashlib.sha256()
    size = 0
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    tmp_path = config.UPLOAD_DIR / f".tmp-{safe_name}"
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "文件超过上传大小限制")
                digest.update(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        stored = config.UPLOAD_DIR / f"{sha[:12]}-{safe_name}"
        tmp_path.rename(stored)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    file_id = db.insert_file(file.filename, str(stored), sha, size)
    db.commit()
    return _register_datasets(file_id, str(stored))


@app.get("/api/files")
def list_files():
    return [_row_to_dict(r) for r in db.list_files()]


@app.get("/api/files/{file_id}")
def inspect_file(file_id: int):
    row = _require_file(file_id)
    try:
        result = hdf5.scan_file(row["stored_path"])
    except hdf5.HdfError as exc:
        raise HTTPException(422, str(exc))
    return {"file": _row_to_dict(row), **result}


# ---------------------------------------------------------------------------
# datasets (registration + validation)
# ---------------------------------------------------------------------------

def _register_datasets(file_id: int, stored_path: str) -> dict[str, Any]:
    """Scan a freshly uploaded file and register every dataset as active."""
    result = hdf5.scan_file(stored_path)
    registered = []

    def walk(node: dict[str, Any]) -> None:
        if node["type"] == "dataset" and not node.get("issue"):
            ds_id = db.upsert_dataset(
                file_id, node["path"], node["shape"], node["dtype"],
                node["channel_names"], "active", [],
            )
            registered.append({"id": ds_id, **node})
        for child in node.get("children", []):
            walk(child)

    walk(result["root"])
    db.commit()
    return {"file_id": file_id, "tree": result["root"],
            "time_suggestions": result["time_suggestions"],
            "datasets": registered}


@app.get("/api/datasets")
def list_datasets(file_id: int | None = None):
    out = []
    for row in db.list_datasets(file_id):
        d = _row_to_dict(row)
        d["shape"] = json.loads(d["shape"])
        d["channel_names"] = json.loads(d["channel_names"])
        d["issues"] = json.loads(d["issues"]) if d["issues"] else []
        out.append(d)
    return out


@app.post("/api/datasets/validate")
def validate(body: ValidateBody):
    row = _require_file(body.file_id)
    cfg = body.time_config.model_dump(exclude_none=True)
    try:
        problems = hdf5.validate_dataset(
            row["stored_path"], body.dataset_path, cfg
        )
    except hdf5.HdfError as exc:
        problems = [str(exc)]
    ds = _require_dataset(body.file_id, body.dataset_path)
    status = "paused" if problems else "active"
    db.upsert_dataset(
        body.file_id, body.dataset_path,
        json.loads(ds["shape"]), ds["dtype"],
        json.loads(ds["channel_names"]), status, problems,
    )
    db.commit()
    return {"dataset_path": body.dataset_path, "status": status,
            "problems": problems}


# ---------------------------------------------------------------------------
# preview (chunked, decimated)
# ---------------------------------------------------------------------------

@app.post("/api/preview")
def preview(body: PreviewBody):
    row = _require_file(body.file_id)
    ds = _require_dataset(body.file_id, body.dataset_path)
    cfg = body.time_config.model_dump(exclude_none=True)
    problems = hdf5.validate_dataset(row["stored_path"], body.dataset_path, cfg)
    if problems:
        raise HTTPException(409, {
            "message": "数据集已暂停，无法预览", "problems": problems})
    all_names = json.loads(ds["channel_names"])
    if any(c < 0 or c >= len(all_names) for c in body.channels):
        raise HTTPException(400, "通道下标越界")
    if body.stop > ds_row_length(ds) or body.start >= body.stop:
        raise HTTPException(400, "时间范围非法或超出数据集长度")
    names = [all_names[c] for c in body.channels]
    try:
        result = hdf5.preview(
            row["stored_path"], body.dataset_path, body.channels, names,
            cfg, body.start, body.stop, body.max_points,
        )
    except hdf5.HdfError as exc:
        raise HTTPException(422, str(exc))
    return {
        "times": result.times,
        "series": result.series,
        "raw_count": result.raw_count,
        "returned": result.returned,
        "decimated": result.decimated,
        "fill_markers_note": "填充值/缺失值以 null 返回，不参与曲线绘制",
    }


def ds_row_length(ds_row) -> int:
    return int(json.loads(ds_row["shape"])[0])


# ---------------------------------------------------------------------------
# import + jobs + configs
# ---------------------------------------------------------------------------

@app.post("/api/import")
def do_import(body: ImportBody):
    row = _require_file(body.file_id)
    ds = _require_dataset(body.file_id, body.dataset_path)
    cfg = body.time_config.model_dump(exclude_none=True)
    try:
        return importer.import_slice(
            db, file_row=row, dataset_row=ds, config=cfg,
            start=body.start, stop=body.stop, channels=body.channels,
        )
    except hdf5.HdfError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/datasets/{dataset_id}/configs")
def dataset_configs(dataset_id: int):
    rows = db.list_configs(dataset_id)
    return [
        {"id": r["id"], "version": r["version"],
         "config": json.loads(r["config_json"]),
         "created_at": r["created_at"]}
        for r in rows
    ]


@app.get("/api/datasets/{dataset_id}/jobs")
def dataset_jobs(dataset_id: int):
    rows = db.list_jobs(dataset_id)
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["channels"] = json.loads(d.pop("channels_json"))
        d["diff"] = json.loads(d["diff_json"]) if d["diff_json"] else None
        d.pop("diff_json", None)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# unified measurement table
# ---------------------------------------------------------------------------

@app.get("/api/readings")
def get_readings(dataset_id: int | None = None, limit: int = 500,
                 offset: int = 0):
    limit = min(limit, 5000)
    rows = db.query_readings(dataset_id, limit, offset)
    return {
        "total": db.count_readings(dataset_id),
        "limit": limit,
        "offset": offset,
        "items": [_row_to_dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")
