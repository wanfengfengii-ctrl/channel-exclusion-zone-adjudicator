"""请求/响应模型与校验边界。

数值范围的硬性约束在 Pydantic 层完成；多边形合法性（顶点计数、重复点、
零面积、自交等）在 ``app.geometry.prepare_polygon`` 中以整数运算裁决，
两类错误统一以 422 + 显式错误信封返回。

顶点计数不在本层设上限：末尾重复首点的闭合写法必须先规整丢弃，再按
不同顶点数裁决 ``TOO_MANY_VERTICES``——若由本层限制原始条目数，201 个
不同顶点加闭合点的提交只会得到通用字段错误。口袋的顶点计数（含下限）
同样全部交由几何层：口袋数量上限（``TOO_MANY_POCKETS``）必须先于逐口袋
顶点校验裁决，且单口袋顶点错误必须携带口袋序号。

所有请求侧模型继承 ``StrictRequestModel``：``extra="forbid"`` 使顶层、
区域、顶点与待判点上的任何未声明字段都整单 422，绝不静默丢弃——
例如误拼的 ``exclusion_margin_cm`` 不得被当成缺省 0 裁决。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .geometry import COORD_LIMIT, MAX_POINT_COUNT

Decision = Literal["FORBIDDEN", "ALLOWED"]
ClassificationKind = Literal["INSIDE", "OUTSIDE", "BOUNDARY", "NEAR_BOUNDARY", "PERMITTED_POCKET"]


class StrictRequestModel(BaseModel):
    # 未声明字段（误拼参数名、夹带业务字段、多写的坐标分量等）一律拒绝，
    # 避免服务静默忽略后按默认值裁决而掩盖调用方错误。
    model_config = ConfigDict(extra="forbid")


class PointModel(StrictRequestModel):
    # strict：只接受真正的整数。布尔值（JSON true/false 会被 Python 解析为 bool，
    # 而 bool 是 int 的子类）、数字字符串、5.0 这类浮点值一律拒绝，整单 422。
    x: int = Field(strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT, description="整数厘米 X 坐标")
    y: int = Field(strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT, description="整数厘米 Y 坐标")


class RegionModel(StrictRequestModel):
    # 允许末尾再写一次首点表示闭合。原始条目只约束下限；上限不在此约束——
    # 末尾闭合点需先在几何层规整丢弃，再按不同顶点数裁决 TOO_MANY_VERTICES，
    # 否则 201 个不同顶点加闭合点的提交会被误报为通用字段错误。
    vertices: list[PointModel] = Field(min_length=3)


class PocketModel(RegionModel):
    """许可口袋：顶点规则与禁抛区完全一致（3～200 个不同顶点，允许末尾闭合写法）。

    顶点计数（含下限）不在本层约束，全部交由几何层裁决：口袋数量上限
    （TOO_MANY_POCKETS）必须先于逐口袋顶点校验，且单口袋顶点错误必须携带
    口袋序号——若由本层逐口袋校验顶点数，11 个顶点不足的口袋会返回 11 条
    局部字段错误，掩盖整单超限。
    """

    vertices: list[PointModel] = Field()


class AdjudicateRequest(StrictRequestModel):
    region: RegionModel
    points: list[PointModel] = Field(max_length=MAX_POINT_COUNT)
    # 可选安全距离（整数厘米）。strict：与坐标同样的严格整数规则——布尔、
    # 数字字符串、5.0 这类浮点一律拒绝；负数或超过坐标上限同样整单 422。
    # 缺省或传 0 时行为与旧接口完全一致。
    exclusion_margin_cm: int = Field(
        default=0,
        strict=True,
        ge=0,
        le=COORD_LIMIT,
        description="可选安全距离：外部点距任一边不超过该值时改判 FORBIDDEN/NEAR_BOUNDARY",
    )
    # 可选许可口袋列表：每个口袋都是严格位于禁抛区内部、彼此不接触不重叠的
    # 简单多边形。数量上限（10 个）、单口袋顶点计数与外环加全部口袋规整后的
    # 总顶点数上限（500）都在几何层校验，分别给出携带计数/序号的显式错误码。
    # 缺省、null 或空列表时行为与旧接口完全一致。
    permitted_pockets: list[PocketModel] | None = Field(
        default=None,
        description="可选许可口袋：严格位于禁抛区内部、互不接触重叠的简单多边形，最多 10 个",
    )


class PointResult(BaseModel):
    index: int
    point: PointModel
    decision: Decision
    classification: ClassificationKind
    evidence: dict


class PolygonSummary(BaseModel):
    vertex_count: int
    edge_count: int
    orientation: Literal["CCW", "CW"]
    signed_area2: int


class AdjudicateResponse(BaseModel):
    polygon: PolygonSummary
    results: list[PointResult]


class RegionAreaSummaryRequest(StrictRequestModel):
    """面积汇总请求：与裁决一致的 region 与可选 permitted_pockets。

    不接收待判点（points）与安全距离（exclusion_margin_cm）——本接口只做
    面积核对；未声明字段由 extra="forbid" 整单 422 拒绝。
    """

    region: RegionModel
    permitted_pockets: list[PocketModel] | None = Field(
        default=None,
        description="可选许可口袋：与裁决接口相同的拓扑校验，最多 10 个",
    )


class PocketAreaSummary(BaseModel):
    pocket_index: int
    area2: int = Field(description="该口袋的绝对二倍面积（平方厘米的二倍）")


class RegionAreaSummaryResponse(BaseModel):
    region_area2: int = Field(description="外环（禁抛区）的绝对二倍面积")
    pockets: list[PocketAreaSummary]
    net_area2: int = Field(description="扣除全部口袋后的二倍面积：region_area2 - sum(pockets.area2)")
