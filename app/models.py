"""请求/响应模型与校验边界。

数值范围的硬性约束在 Pydantic 层完成；多边形合法性（重复点、零面积、
自交等）在 ``app.geometry.prepare_polygon`` 中以整数运算裁决，两类错误
统一以 422 + 显式错误信封返回。
"""

from typing import Literal

from pydantic import BaseModel, Field

from .geometry import COORD_LIMIT, MAX_POINT_COUNT, MAX_VERTEX_COUNT

Decision = Literal["FORBIDDEN", "ALLOWED"]
ClassificationKind = Literal["INSIDE", "OUTSIDE", "BOUNDARY"]


class PointModel(BaseModel):
    # strict：只接受真正的整数。布尔值（JSON true/false 会被 Python 解析为 bool，
    # 而 bool 是 int 的子类）、数字字符串、5.0 这类浮点值一律拒绝，整单 422。
    x: int = Field(strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT, description="整数厘米 X 坐标")
    y: int = Field(strict=True, ge=-COORD_LIMIT, le=COORD_LIMIT, description="整数厘米 Y 坐标")


class RegionModel(BaseModel):
    # 允许末尾再写一次首点表示闭合，因此原始条目数上限为 201。
    vertices: list[PointModel] = Field(min_length=3, max_length=MAX_VERTEX_COUNT + 1)


class AdjudicateRequest(BaseModel):
    region: RegionModel
    points: list[PointModel] = Field(max_length=MAX_POINT_COUNT)


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
