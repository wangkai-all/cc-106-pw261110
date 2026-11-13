# HDF5 测量数据导入工具

上传 HDF5 测量文件后，在页面浏览组/数据集/维度/属性，选择时间方式与通道预览曲线，
再把选中的切片导入统一测量表。每条读数都保留原文件、数据集路径和数组下标，可逐条溯源。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. 生成本地样本文件（含压缩数组、非连续时间、缺失通道、暂停场景）
.venv/bin/python -I backend/scripts/make_sample.py data/sample.h5

# 2. 启动服务
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8077
# 浏览器打开 http://127.0.0.1:8077/
```

数据默认落在 `data/`（上传文件 + `measurements.sqlite3`），可用环境变量
`APP_DATA_DIR` 改位置。

## 测试

```bash
# 单元测试（23 项，全部用 -I 隔离模式运行）
.venv/bin/python -I -m pytest backend/tests -q

# 端到端：自动清库、起服务、跑完 27 项 HTTP 检查后停服务
bash backend/scripts/run_e2e.sh
```

## 目录结构

```
backend/
  app/
    hdf5.py      扫描树、时间方式推断、校验暂停、分块/跨步读取、缺失值与比例处理
    db.py        SQLite 统一测量表、配置版本、自然键去重、导入任务
    importer.py  导入编排：校验 → 配置版本 → 分块流式写入 → 差异统计
    main.py      FastAPI 路由 + 静态前端
  scripts/
    make_sample.py  样本文件生成器
    e2e_check.py    HTTP 端到端检查
    run_e2e.sh      一键重置并运行 E2E
  tests/            pytest
frontend/           原生 HTML/CSS/JS，无构建步骤
```

## 需求落点

### 浏览与选择
- 上传后展示完整组/数据集树，数据集带形状、dtype、分块、压缩、填充值、通道名和全部属性。
- 时间轴支持两种方式，界面显式切换并要求确认单位：
  - **独立时间数组**：选择数组路径（自动从 `time_ref` 等属性或同级 `time/timestamp` 数组推断）。
  - **起始时间 + 采样间隔推算**：填起始值、间隔与单位（ns/us/ms/s/min/h/d）。
- 时区支持 IANA 名（`Asia/Shanghai`）或固定偏移（`+08:00`），用于展示；读数统一存 UTC。

### 缺失值、比例系数、时区
- 缺失标记来源：HDF5 `_FillValue`、`missing_value` 属性、数据集 fillvalue，以及界面确认后填写的值。
- 填充值与 NaN 一律转为掩码（曲线断沟、接口返回 `null`、库存 `is_missing=1`），**绝不计入曲线或数值**。
- `scale_factor` / `add_offset` 按属性应用，可在界面关闭；关闭后作为新配置版本保留。
- 时区优先取属性，可在界面覆盖确认。

### 暂停规则（只暂停问题数据集，其他照常使用）
- 时间轴长度与数据长度不匹配 → 该数据集暂停。
- 通道名重复 → 该数据集暂停。
- 标量/字符串等非数值 1D/2D 数组只可浏览，不能预览/导入。
- 暂停状态持久化，预览和导入接口对暂停数据集返回 409。

### 分块读取，不整文件入内存
- 预览：HDF5 **跨步超切片（strided hyperslab）** 按块只读要画的点，先按 `max_points` 抽稀。
- 导入：时间块与数据块用生成器**交错消费**，同一时刻内存里只有一个块（默认 8192 行）。
- 有专门测试用 `h5py.Dataset.__getitem__` 守卫断言不存在整轴连续读取，并统计实际读取元素数。

### 统一测量表、去重与配置版本
- 读数自然键 `(dataset_id, channel_index, array_index)` 建唯一约束。
- 同一切片 + 同一配置重复导入：识别为 `duplicate`，**零新增**。
- 换配置导入：生成新版本号，任务卡展示与上一版本的字段差异（旧值→新值），已有行按新值更新而非新增。
- 所有配置版本永久保留，可经 `/api/datasets/{id}/configs` 查询。

### 读数溯源
统一表每行带 `source_file_id`、`source_dataset_path`、`array_index`、`channel_index`、
`timestamp_utc`、`config_version`。界面点击任意读数行弹出溯源卡，显示原文件名、数据集路径与数组下标。

## 样本文件覆盖的场景

`make_sample.py` 生成的 `data/sample.h5` 包含：

| 路径 | 场景 |
|---|---|
| `/sensors/temp` | gzip 2D、非连续时间数组（含 NaT 缺口）、NaN 与填充值、scale/offset、通道标签 |
| `/sensors/pressure` | gzip 1D、起始时间+采样间隔、整数填充标记、固定偏移时区 |
| `/sensors/vibration` | 含一个全缺失（全填充）通道 |
| `/problems/time_mismatch` | 时间数组长度不匹配 → 暂停 |
| `/problems/dup_channels` | 通道名重复 → 暂停 |
| `/meta/*` | 标量/字符串数组 → 仅浏览 |

## 主要 API

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/files` | 上传 HDF5（按 sha256 去重），返回树与时间方式建议 |
| GET | `/api/files/{id}` | 重新扫描，返回组/数据集树与时间建议 |
| POST | `/api/datasets/validate` | 校验并设置 active/paused |
| POST | `/api/preview` | 分块抽稀预览，缺失值以 null 返回 |
| POST | `/api/import` | 导入切片（去重/版本/差异） |
| GET | `/api/datasets/{id}/configs` | 配置版本列表 |
| GET | `/api/datasets/{id}/jobs` | 导入任务与差异 |
| GET | `/api/readings` | 统一测量表（支持 dataset_id 过滤、分页） |
