# 疏浚抛泥裁决服务（Spoil Dumping Adjudication）

纯后端裁决微服务：给定一个**禁抛区简单多边形**与**最多 500 个待判点**，逐点裁决
`FORBIDDEN`（禁抛）或 `ALLOWED`（放行），并给出可复核的整数几何证据。

- 语言/框架：Python 3.12 + FastAPI（容器镜像 `python:3.12-slim`）
- 几何实现：**自行实现的整数计算几何**（`app/geometry.py`），不调用任何空间数据库或第三方几何库
- 计算精度：全程 Python 任意精度整数，无浮点、无容差，坐标边界上的点不存在“浮点抖动”

## 坐标与单位

- 所有坐标均为**十进制整数，单位厘米**。
- 合法范围：`-100_000_000 <= x, y <= 100_000_000`（约 ±1000 公里）。
- 非整数坐标（如 `1.5`）直接拒绝。
- 坐标系采用通常的笛卡尔约定（y 轴向上）；由于顺/逆时针区域给出完全相同的裁决，
  上游系统坐标轴朝向不影响放行结论。

## 裁决规则

对每个点按优先级分类：

| 分类 | 含义 | 裁决 |
| --- | --- | --- |
| `BOUNDARY` | 严格落在任意一条边或顶点上（**含边线与顶点本身**） | `FORBIDDEN` |
| `INSIDE` | 严格在多边形内部 | `FORBIDDEN` |
| `OUTSIDE` | 严格在多边形外部 | `ALLOWED` |

- 点同时命中多条边（即顶点）时，证据中的 `edge_index` 取**输入边序号最小者**。
- 边序号从 **0** 开始，第 `i` 条边为 `vertices[i] → vertices[(i+1) mod n]`，
  其中**闭合边**为最后一个不同顶点到第 0 个顶点（序号 `n-1`）。
- 区域顶点可省略末尾首点，也可再写一次首点表示闭合；两种写法完全等价，
  服务规整后丢弃重复的闭合点，响应中给出规整后的顶点/边数。
- 非边界点的证据来自**一次 +x 水平射线**：与边的穿越数为奇数 ⇒ 内部，偶数 ⇒ 外部。
  顶点采用半开区间跨立规则（只计严格高于射线的端点），不会被重复计数。

## 区域合法性

区域由 3～200 个**不同**顶点按边界顺序给出。以下任一情况都会让**整次请求返回 422**，
绝不返回部分结果：

- 连续重复顶点（含闭合位置的意外重复）；
- 规整后不同顶点少于 3 个、顶点数超过 200；
- 零面积（共线或退化）；
- 非相邻边相交/接触/共线重叠，或相邻边共线回退（区域必须是简单多边形）。

## 运行

```bash
# 构建并启动 API（宿主端口可用 API_PORT 覆盖，默认 8000）
docker compose up --build api
API_PORT=9090 docker compose up --build api

# 一次性验收服务：先跑 pytest，再对在线 API 执行验收清单，结束即退出
docker compose build --pull verify
docker compose run --rm verify
```

`docker-compose.yml` 中的 `verify` 是一次性服务（`restart: "no"`），它：

1. 在容器内运行 `pytest`（算法旁单元/契约测试）；
2. 等待 `api` 健康检查通过后，运行 `scripts/acceptance.py`，
   通过真实 HTTP 调用核对：顺/逆时针与闭合写法等价、边界点稳定拒绝且边序号最小、
   内外分类与射线奇偶、非法区域只出现明确错误、500 点上限与顺序保持、1e8 大坐标精确性。

本地不用 Docker 时：

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest
```

## API

### `POST /adjudicate`

请求：

```json
{
  "region": {
    "vertices": [
      {"x": 0, "y": 0},
      {"x": 1000, "y": 0},
      {"x": 1000, "y": 1000},
      {"x": 0, "y": 1000}
    ]
  },
  "points": [
    {"x": 500, "y": 500},
    {"x": 0, "y": 0},
    {"x": -500, "y": 0}
  ]
}
```

成功响应（`200`，结果严格保持待判点输入顺序）：

```json
{
  "polygon": {
    "vertex_count": 4,
    "edge_count": 4,
    "orientation": "CCW",
    "signed_area2": 2000000
  },
  "results": [
    {
      "index": 0,
      "point": {"x": 500, "y": 500},
      "decision": "FORBIDDEN",
      "classification": "INSIDE",
      "evidence": {
        "type": "horizontal_ray",
        "ray_direction": "+x",
        "crossing_edges": [1],
        "crossing_count": 1,
        "parity": "odd",
        "rule": "一次水平射线穿越边数为奇数 => 内部(FORBIDDEN)，偶数 => 外部(ALLOWED)。"
      }
    },
    {
      "index": 1,
      "point": {"x": 0, "y": 0},
      "decision": "FORBIDDEN",
      "classification": "BOUNDARY",
      "evidence": {
        "type": "boundary",
        "edge_index": 0,
        "edge": [[0, 0], [1000, 0]],
        "rule": "点落在边（含端点）上，一律 FORBIDDEN；命中多条边时取最小边序号。"
      }
    },
    {
      "index": 2,
      "point": {"x": -500, "y": 0},
      "decision": "ALLOWED",
      "classification": "OUTSIDE",
      "evidence": {
        "type": "horizontal_ray",
        "ray_direction": "+x",
        "crossing_edges": [1, 3],
        "crossing_count": 2,
        "parity": "even",
        "rule": "一次水平射线穿越边数为奇数 => 内部(FORBIDDEN)，偶数 => 外部(ALLOWED)。"
      }
    }
  ]
}
```

`GET /healthz` → `200 {"status": "ok"}`，供容器健康检查与验收等待使用。

### 错误结构（统一信封，HTTP 422）

所有请求级错误（结构/取值校验、几何合法性）都返回相同信封，且**不含任何部分结果**：

```json
{
  "error": {
    "code": "SELF_INTERSECTING_POLYGON",
    "message": "非相邻边 0 与 2 存在公共点，区域必须是简单多边形。",
    "details": {"edge_indices": [0, 2]}
  }
}
```

错误码：

| `code` | 触发条件 | 典型 `details` |
| --- | --- | --- |
| `VALIDATION_ERROR` | JSON 结构错误、坐标非整数/越界、顶点数不在 3～201、点数超过 500 等 | `issues`: 字段级问题列表 |
| `CONSECUTIVE_DUPLICATE_VERTEX` | 连续重复顶点（含闭合处） | `vertex_index` |
| `NOT_ENOUGH_DISTINCT_VERTICES` | 不同顶点少于 3 个 | `distinct_vertex_count` |
| `TOO_MANY_VERTICES` | 规整后顶点超过 200 个 | `vertex_count` |
| `ZERO_AREA_POLYGON` | 有向面积为 0（共线/退化） | — |
| `SELF_INTERSECTING_POLYGON` | 非相邻边相交/接触/重叠，或相邻边共线回退 | `edge_indices` |

## 测试策略（不固定结果）

`tests/` 锁定以下行为而非固定响应表：

- **方向反转**：顺/逆时针区域、省略/重写首点两种闭合写法对同一批点逐点结论一致；
- **大坐标**：±1e8 下斜边 `x+y=B` 上的点与相邻整数点严格区分为边界/内/外；
- **凹多边形**：凹口点判外，并核对射线穿越边数奇偶；
- **自交拒绝**：蝴蝶结（含非零面积变体）、8 字顶点相触、共线回退一律 422；
- **边界归属**：边内点与顶点均为 `BOUNDARY`，顶点命中两条边时返回最小序号；
- **随机性质测试**：`test_random_polygons_match_exact_reference` 生成大量随机星形简单
  多边形（顺/逆时针各一遍），用标准库 `fractions.Fraction` 写的独立精确参照实现对拍，
  包含各顶点周围 3×3 邻域与随机探针——期望由数学参照实时计算，不固化任何结果。
