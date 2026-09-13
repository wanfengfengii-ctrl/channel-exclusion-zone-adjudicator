# 疏浚抛泥裁决服务（Spoil Dumping Adjudication）

纯后端裁决微服务：给定一个**禁抛区简单多边形**与**最多 500 个待判点**，逐点裁决
`FORBIDDEN`（禁抛）或 `ALLOWED`（放行），并给出可复核的整数几何证据。

- 语言/框架：Python 3.12 + FastAPI（容器镜像 `python:3.12-slim`）
- 几何实现：**自行实现的整数计算几何**（`app/geometry.py`），不调用任何空间数据库或第三方几何库
- 计算精度：全程 Python 任意精度整数，无浮点、无容差，坐标边界上的点不存在“浮点抖动”

## 坐标与单位

- 所有坐标均为**十进制整数，单位厘米**。
- 合法范围：`-100_000_000 <= x, y <= 100_000_000`（约 ±1000 公里）。
- 必须是 JSON 整数，类型严格：`true`/`false`、数字字符串（`"5"`）、浮点（`5.0`）
  即使数值上等于整数也一律整单拒绝（422），不会被隐式转换后裁决。
- 坐标系采用通常的笛卡尔约定（y 轴向上）；由于顺/逆时针区域给出完全相同的裁决，
  上游系统坐标轴朝向不影响放行结论。

## 裁决规则

对每个点按优先级分类：

| 分类 | 含义 | 裁决 |
| --- | --- | --- |
| `BOUNDARY` | 严格落在任意一条边或顶点上（**含边线与顶点本身**） | `FORBIDDEN` |
| `INSIDE` | 严格在多边形内部（含落在许可口袋边界上的点） | `FORBIDDEN` |
| `NEAR_BOUNDARY` | 严格在外部、但距任一边不超过 `exclusion_margin_cm`；或严格在许可口袋内部、但距口袋边界不超过该值（仅设置了正安全距离时出现） | `FORBIDDEN` |
| `PERMITTED_POCKET` | 严格位于某个许可口袋内部，且未被安全距离带覆盖（仅提交 `permitted_pockets` 时出现） | `ALLOWED` |
| `OUTSIDE` | 严格在多边形外部，且未被安全距离覆盖 | `ALLOWED` |

- 点同时命中多条边（即顶点）时，证据中的 `edge_index` 取**输入边序号最小者**。
- 边序号从 **0** 开始，第 `i` 条边为 `vertices[i] → vertices[(i+1) mod n]`，
  其中**闭合边**为最后一个不同顶点到第 0 个顶点（序号 `n-1`）。
- 区域顶点可省略末尾首点，也可再写一次首点表示闭合；两种写法完全等价，
  服务规整后丢弃重复的闭合点，响应中给出规整后的顶点/边数。
- 非边界点的证据来自**一次 +x 水平射线**：与边的穿越数为奇数 ⇒ 内部，偶数 ⇒ 外部。
  顶点采用半开区间跨立规则（只计严格高于射线的端点），不会被重复计数。

### 安全距离 `exclusion_margin_cm`（可选）

测量坐标存在定位误差时，可在请求顶层附带可选字段 `exclusion_margin_cm`
（整数厘米，`0 <= 值 <= 100_000_000`，缺省为 `0`）：

- **缺省或传 `0`**：响应结构、分类结果、证据与点顺序和旧接口完全一致；
- **正值**：内部点与边界点仍按原规则禁抛；严格在外部、但到某条边的最短距离
  **不超过**该值的点改判 `FORBIDDEN`，分类标记为 `NEAR_BOUNDARY`；刚越过阈值的
  外部点正常放行；
- 距离计算全程整数：以叉积、点积和平方距离分数比较点到各线段（含端点）的最短
  距离，不使用浮点；最近距离相同（如顶点两侧邻边）时取**最小边序号**；
- `NEAR_BOUNDARY` 的证据给出该边及**约分后**的距离平方分子/分母，例如：

  ```json
  {
    "type": "near_boundary",
    "edge_index": 0,
    "edge": [[0, 0], [1000, 0]],
    "distance2_num": 9,
    "distance2_den": 1,
    "exclusion_margin_cm": 3,
    "rule": "外部点到最近边的距离不超过 exclusion_margin_cm，改判 FORBIDDEN；距离平方以约分后的分数给出，最近距离相同取最小边序号。"
  }
  ```

- 负数、非整数（布尔、数字字符串、`5.0` 这类浮点）或超过坐标上限的安全距离，
  与坐标校验一样**整单返回 422**（`VALIDATION_ERROR`），绝不返回部分结果。

### 许可口袋 `permitted_pockets`（可选）

港调获批在禁抛区内部划定临时许可口袋后，可在请求顶层附带可选字段
`permitted_pockets`：最多 **10** 个简单多边形，每个口袋的顶点规则与禁抛区
完全一致（3～200 个不同顶点、允许末尾闭合写法、严格整数坐标、未声明字段拒绝）。

- **几何校验**（任一不满足即整单 422，绝不返回部分结果，错误 `details`
  携带对应口袋序号或计数）：
  - 口袋数量超过 10 个时**先于**单口袋顶点校验整单拒绝
    （`TOO_MANY_POCKETS`），11 个顶点不足的口袋不会返回 11 条局部错误；
  - 单口袋顶点计数沿用区域规则并按规整后的不同顶点裁决，错误携带
    `pocket_index`（如 201 个不同顶点加闭合点的口袋返回
    `TOO_MANY_VERTICES` 而非通用"列表过长"）；
  - 每个口袋**严格位于禁抛区内部**：全部顶点严格在区域内，且任意口袋边与
    区域边无公共点（接触也不行）；
  - 口袋两两之间**不接触、不重叠**（含相互嵌套）；
  - 外环与全部口袋**规整后**的总顶点数不超过 **500**；
- **裁决仍走原链路**：先按禁抛区分类，再对严格落入某口袋内部的点改判
  `ALLOWED`，分类 `PERMITTED_POCKET`，证据携带按输入顺序归因的
  `pocket_index`；点落在口袋边界（含顶点）上仍 `FORBIDDEN`（按禁抛区内部
  处理，分类与证据维持原样）；
- **与安全距离组合**：`exclusion_margin_cm` 为正时许可范围向口袋内部收缩——
  口袋内部距口袋边界不超过该距离的点继续 `FORBIDDEN`，分类 `NEAR_BOUNDARY`，
  证据沿用既有精确距离形式（约分后的平方距离分数）并标注 `pocket_index`；
- 未提交（或提交 `null`/空列表）时，请求与响应和当前版本完全一致。

`PERMITTED_POCKET` 的证据示例：

```json
{
  "type": "permitted_pocket",
  "pocket_index": 0,
  "rule": "点严格位于许可口袋内部，改判 ALLOWED；口袋按输入顺序归因，落在口袋边界上的点仍按禁抛区内部处理（FORBIDDEN）。"
}
```

### 未声明字段一律拒绝

请求顶层、禁抛区域、区域顶点与待判点上的**任何未声明字段**（如误拼的
`exclushun_margin_cm`、点上的额外坐标分量、区域夹带的业务字段）都不会被
静默忽略或丢弃，而是**整单返回 422**（`VALIDATION_ERROR`），错误明细中的
`loc` 精确定位到该字段（如 `["body", "points", 1, "z"]`、
`["body", "region", "zone_code"]`），避免服务按默认值（零距离）裁决而掩盖
调用方参数错误。

## 区域合法性

区域由 3～200 个**不同**顶点按边界顺序给出。以下任一情况都会让**整次请求返回 422**，
绝不返回部分结果：

- 连续重复顶点（含闭合位置的意外重复）；
- 规整后不同顶点少于 3 个、顶点数超过 200；
- 零面积（共线或退化）；
- 非相邻边相交/接触/共线重叠，或相邻边共线回退（区域必须是简单多边形）。

顶点数上限按**规整后的不同顶点**裁决：201 个不同顶点加末尾闭合点的提交明确返回
`TOO_MANY_VERTICES`（`details.vertex_count` 给出规整后的顶点数），而非通用字段错误；
只有原始条目不足 3 个时才按结构错误 `VALIDATION_ERROR` 处理。

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
   内外分类与射线奇偶、非法区域只出现明确错误、500 点上限与顺序保持、1e8 大坐标精确性，
   以及安全距离（边中段阈值翻转、顶点端点距离、约分分数证据、顺/逆时针边序归因稳定、
   零安全距离与缺省完全等价、非法安全距离整单 422），
   以及许可口袋（口袋内部放行与输入顺序归因、口袋边界禁抛、安全距离带向口袋内部
   收缩、口袋相交/接触/嵌套与越出区域整单 422、11 个口袋与总顶点超限拒绝、
   未提交口袋时响应与当前版本完全一致），
   以及面积汇总（矩形外环加两个口袋的面积守恒、顺/逆时针与闭合写法结果一致、
   非法口袋拒绝且可定位、不接收待判点与安全距离字段）。

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

### `POST /region-area-summary`

港调在启用带许可口袋的禁抛区前核对申报面积：接收与裁决一致的 `region` 与可选
`permitted_pockets`（**不接收** `points` 与 `exclusion_margin_cm`，未声明字段同样
整单 422），复用同一套多边形规整与口袋拓扑校验后，返回外环、各口袋与扣除口袋
后的面积，避免调用点位裁决后再由外部系统重复计算。

请求：

```json
{
  "region": {
    "vertices": [
      {"x": 0, "y": 0},
      {"x": 100, "y": 0},
      {"x": 100, "y": 100},
      {"x": 0, "y": 100}
    ]
  },
  "permitted_pockets": [
    {"vertices": [{"x": 10, "y": 10}, {"x": 20, "y": 10}, {"x": 20, "y": 20}, {"x": 10, "y": 20}]},
    {"vertices": [{"x": 40, "y": 40}, {"x": 50, "y": 40}, {"x": 50, "y": 50}, {"x": 40, "y": 50}]}
  ]
}
```

成功响应（`200`）：

```json
{
  "region_area2": 20000,
  "pockets": [
    {"pocket_index": 0, "area2": 200},
    {"pocket_index": 1, "area2": 200}
  ],
  "net_area2": 19600
}
```

- 面积一律以**二倍整数**（平方厘米的二倍）表达，全程整数无浮点；
- `region_area2` 为外环绝对二倍面积；各口袋按输入顺序携带 `pocket_index` 与自身
  绝对二倍面积；`net_area2` = `region_area2` − 全部口袋 `area2` 之和；
- 结果与顶点顺/逆时针方向及末尾重复闭合点写法无关（规整后取绝对值）；
- 区域或口袋非法、口袋数量或总顶点超限时，返回与裁决接口完全相同的 422 错误
  信封（`details` 保留口袋序号/计数定位），绝不返回部分结果；
- 未提交（或提交 `null`/空列表）`permitted_pockets` 时 `pockets` 为空、
  `net_area2` 等于 `region_area2`。

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
| `VALIDATION_ERROR` | JSON 结构错误、坐标非整数/越界、区域顶点原始条目少于 3、点数超过 500、安全距离非法（负数/非整数/超过坐标上限）等 | `issues`: 字段级问题列表 |
| `CONSECUTIVE_DUPLICATE_VERTEX` | 连续重复顶点（含闭合处） | `vertex_index`（口袋另附 `pocket_index`） |
| `NOT_ENOUGH_DISTINCT_VERTICES` | 不同顶点少于 3 个 | `distinct_vertex_count`（口袋另附 `pocket_index`） |
| `TOO_MANY_VERTICES` | 规整后顶点超过 200 个 | `vertex_count`（口袋另附 `pocket_index`） |
| `ZERO_AREA_POLYGON` | 有向面积为 0（共线/退化） | —（口袋另附 `pocket_index`） |
| `SELF_INTERSECTING_POLYGON` | 非相邻边相交/接触/重叠，或相邻边共线回退 | `edge_indices`（口袋另附 `pocket_index`） |
| `TOO_MANY_POCKETS` | 许可口袋超过 10 个 | `pocket_count` |
| `TOO_MANY_TOTAL_VERTICES` | 外环与全部口袋规整后的总顶点数超过 500 | `total_vertex_count` |
| `POCKET_NOT_INSIDE_REGION` | 口袋顶点不在禁抛区内部（含压在边界上），或口袋边与区域边存在公共点 | `pocket_index`，`vertex_index` 或 `edge_indices` |
| `POCKETS_INTERSECT` | 两个口袋的边相交/接触/重叠，或相互嵌套 | `pocket_indices`（可含 `edge_indices`） |

## 测试策略（不固定结果）

`tests/` 锁定以下行为而非固定响应表：

- **方向反转**：顺/逆时针区域、省略/重写首点两种闭合写法对同一批点逐点结论一致；
- **大坐标**：±1e8 下斜边 `x+y=B` 上的点与相邻整数点严格区分为边界/内/外；
- **凹多边形**：凹口点判外，并核对射线穿越边数奇偶；
- **自交拒绝**：蝴蝶结（含非零面积变体）、8 字顶点相触、共线回退一律 422；
- **边界归属**：边内点与顶点均为 `BOUNDARY`，顶点命中两条边时返回最小序号；
- **安全距离**：边中段外侧点按阈值翻转为 `NEAR_BOUNDARY`、顶点附近按线段端点距离
  命中、刚越过阈值放行、平方距离分数约分、顺/逆时针边序归因稳定、零安全距离与
  缺省响应完全一致、非法安全距离整单 422；
- **许可口袋**：口袋内部点放行并携带输入顺序的 `pocket_index`、口袋边界点维持
  禁抛、安全距离带向口袋内部收缩（恰等于阈值仍禁抛）、口袋相交/接触/嵌套与越出
  禁抛区整单 422、11 个口袋与总顶点数超过 500 拒绝（恰好 500 受理）、
  缺省/`null`/空列表与未提交字段响应完全一致；
- **面积汇总**：矩形外环加两个口袋核对 `net_area2` 面积守恒、顺/逆时针与闭合
  写法结果逐项一致、非法口袋（自交/越界/接触/数量与总顶点超限）整单 422 且
  `details` 保留口袋定位、接口拒绝 `points` 与 `exclusion_margin_cm` 字段、
  大坐标二倍面积整数精确；
- **随机性质测试**：`test_random_polygons_match_exact_reference` 生成大量随机星形简单
  多边形（顺/逆时针各一遍），用标准库 `fractions.Fraction` 写的独立精确参照实现对拍，
  包含各顶点周围 3×3 邻域与随机探针——期望由数学参照实时计算，不固化任何结果；
  `test_random_nearest_edge_matches_fraction_reference` 以同样方式对拍最近边序号
  （同距取最小序号）与约分后的平方距离分数。
