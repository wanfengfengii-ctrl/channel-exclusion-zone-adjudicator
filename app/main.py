"""FastAPI 入口：抛泥点裁决服务。

POST /adjudicate
    入参::

        {"region": {"vertices": [{"x": .., "y": ..}, ...]},
         "points": [{"x": .., "y": ..}, ...],
         "exclusion_margin_cm": 0,      # 可选，缺省为 0
         "permitted_pockets": null}     # 可选，缺省为无口袋

    区域非法时整个请求返回 422，绝不返回部分结果；区域合法时按输入
    顺序给出每个点的裁决与证据。exclusion_margin_cm 为正时，距任一
    边不超过该距离的外部点改判 FORBIDDEN 并标记为 NEAR_BOUNDARY；
    缺省或传 0 时响应结构、分类与证据和旧接口完全一致。

    permitted_pockets 在禁抛区内部划定临时许可口袋：最多 10 个简单
    多边形，每个沿用区域顶点规则，外环与全部口袋规整后的总顶点数
    不超过 500；每个口袋必须严格位于禁抛区内部且彼此不接触、不
    重叠，任一非法整单 422（details 携带口袋序号或计数）。点严格
    落入某口袋内部时改判 ALLOWED 并标记 PERMITTED_POCKET（证据
    携带按输入顺序归因的口袋序号）；落在口袋边界上仍为 FORBIDDEN。
    安全距离为正时许可范围向口袋内部收缩：口袋内部距口袋边界不超
    过该距离的点继续禁抛，沿用既有精确距离证据并标注口袋序号。
    未提交 permitted_pockets 时请求响应与当前版本完全一致。
POST /region-area-summary
    入参::

        {"region": {"vertices": [{"x": .., "y": ..}, ...]},
         "permitted_pockets": null}     # 可选，缺省为无口袋

    面积核对接口：复用与裁决完全一致的多边形规整与口袋拓扑校验
    （非法时同一 422 错误信封并保留口袋定位），返回外环、各口袋
    （按输入顺序携带 pocket_index）与扣除口袋后的面积。面积一律
    以二倍整数（平方厘米的二倍）给出：汇总值 = 外环绝对二倍面积
    - 全部口袋绝对二倍面积，与顶点顺/逆时针方向及末尾重复闭合点
    写法无关。本接口不接收待判点与安全距离。
GET /healthz
    存活探针，供 Docker Compose 的 verify 服务等待 API 就绪。
"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .geometry import (
    Classification,
    NearestEdge,
    Polygon,
    PolygonError,
    classify,
    containing_pocket,
    doubled_area,
    nearest_edge,
    prepare_pockets,
    prepare_polygon,
    within_exclusion_margin,
)
from .models import (
    AdjudicateRequest,
    AdjudicateResponse,
    PocketAreaSummary,
    PointModel,
    PointResult,
    PolygonSummary,
    RegionAreaSummaryRequest,
    RegionAreaSummaryResponse,
)

app = FastAPI(
    title="Dredging Spoil Dumping Adjudication Service",
    version="1.3.0",
    description="纯整数计算几何：判定点位于禁抛区内部、外部还是边界，支持边界安全距离、区内许可口袋与区域面积核对。",
)


def error_envelope(code: str, message: str, details: dict | None = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=422, content=body)


@app.exception_handler(PolygonError)
async def polygon_error_handler(_request: Request, exc: PolygonError) -> JSONResponse:
    return error_envelope(exc.code, exc.message, dict(exc.details))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # 与几何错误保持同一信封结构，字段级问题放入 details.issues；
    # 剥去 pydantic 的 ctx（内含异常对象，不保证可 JSON 序列化）。
    issues = [{k: v for k, v in issue.items() if k != "ctx"} for issue in exc.errors()]
    return error_envelope(
        "VALIDATION_ERROR",
        "请求结构或取值不合法。",
        {"issues": jsonable_encoder(issues)},
    )


def edge_evidence(poly, edge_index: int) -> dict:
    (ax, ay), (bx, by) = poly.edge(edge_index)
    return {
        "type": "boundary",
        "edge_index": edge_index,
        "edge": [[ax, ay], [bx, by]],
        "rule": "点落在边（含端点）上，一律 FORBIDDEN；命中多条边时取最小边序号。",
    }


def ray_evidence(cls: Classification) -> dict:
    return {
        "type": "horizontal_ray",
        "ray_direction": "+x",
        "crossing_edges": list(cls.crossing_edges),
        "crossing_count": len(cls.crossing_edges),
        "parity": "odd" if len(cls.crossing_edges) % 2 == 1 else "even",
        "rule": "一次水平射线穿越边数为奇数 => 内部(FORBIDDEN)，偶数 => 外部(ALLOWED)。",
    }


def near_boundary_evidence(
    poly, near: NearestEdge, margin_cm: int, pocket_index: int | None = None
) -> dict:
    (ax, ay), (bx, by) = poly.edge(near.edge_index)
    evidence = {
        "type": "near_boundary",
        "edge_index": near.edge_index,
        "edge": [[ax, ay], [bx, by]],
        "distance2_num": near.dist2_num,
        "distance2_den": near.dist2_den,
        "exclusion_margin_cm": margin_cm,
        "rule": "外部点到最近边的距离不超过 exclusion_margin_cm，改判 FORBIDDEN；"
        "距离平方以约分后的分数给出，最近距离相同取最小边序号。",
    }
    if pocket_index is not None:
        # 口袋内部的近边界点：同一精确距离证据，边序号相对该口袋。
        evidence["pocket_index"] = pocket_index
        evidence["rule"] = (
            "许可口袋内部的点距口袋边界不超过 exclusion_margin_cm，许可范围向口袋内部收缩，"
            "继续 FORBIDDEN；距离平方以约分后的分数给出，最近距离相同取最小边序号。"
        )
    return evidence


def pocket_evidence(pocket_index: int) -> dict:
    return {
        "type": "permitted_pocket",
        "pocket_index": pocket_index,
        "rule": "点严格位于许可口袋内部，改判 ALLOWED；口袋按输入顺序归因，"
        "落在口袋边界上的点仍按禁抛区内部处理（FORBIDDEN）。",
    }


def prepare_region_and_pockets(region_model, pocket_models) -> tuple[Polygon, list[Polygon]]:
    """规整禁抛区并校验许可口袋，两个接口共用同一链路。

    区域非法或口袋非法时直接抛 PolygonError -> 422，无部分结果；
    缺省、null 或空列表的 permitted_pockets 均视为无口袋。
    """

    raw = [[v.x, v.y] for v in region_model.vertices]
    poly = prepare_polygon(raw)
    pockets: list[Polygon] = []
    if pocket_models:
        raw_pockets = [[[v.x, v.y] for v in pocket.vertices] for pocket in pocket_models]
        pockets = prepare_pockets(poly, raw_pockets)
    return poly, pockets


@app.post("/adjudicate", response_model=AdjudicateResponse)
def adjudicate(req: AdjudicateRequest) -> AdjudicateResponse:
    poly, pockets = prepare_region_and_pockets(req.region, req.permitted_pockets)
    margin = req.exclusion_margin_cm

    results: list[PointResult] = []
    for i, p in enumerate(req.points):
        cls = classify(poly, p.x, p.y)
        if cls.kind == "BOUNDARY":
            decision = "FORBIDDEN"
            classification = "BOUNDARY"
            evidence = edge_evidence(poly, cls.boundary_edge)
        elif cls.kind == "INSIDE":
            pocket_idx = containing_pocket(pockets, p.x, p.y) if pockets else None
            if pocket_idx is None:
                # 不在任何口袋严格内部（含落在口袋边界上）：维持原裁决。
                decision = "FORBIDDEN"
                classification = "INSIDE"
                evidence = ray_evidence(cls)
            else:
                pocket = pockets[pocket_idx]
                # 安全距离为正时许可范围向口袋内部收缩：距口袋边界不超过
                # 该距离的点继续禁抛，沿用既有精确距离证据并标注口袋序号。
                near = nearest_edge(pocket, p.x, p.y) if margin > 0 else None
                if near is not None and within_exclusion_margin(near, margin):
                    decision = "FORBIDDEN"
                    classification = "NEAR_BOUNDARY"
                    evidence = near_boundary_evidence(pocket, near, margin, pocket_index=pocket_idx)
                else:
                    decision = "ALLOWED"
                    classification = "PERMITTED_POCKET"
                    evidence = pocket_evidence(pocket_idx)
        else:
            # 外部点：安全距离为正且距最近边不超过该距离时改判禁抛。
            near = nearest_edge(poly, p.x, p.y) if margin > 0 else None
            if near is not None and within_exclusion_margin(near, margin):
                decision = "FORBIDDEN"
                classification = "NEAR_BOUNDARY"
                evidence = near_boundary_evidence(poly, near, margin)
            else:
                decision = "ALLOWED"
                classification = "OUTSIDE"
                evidence = ray_evidence(cls)
        results.append(
            PointResult(
                index=i,
                point=PointModel(x=p.x, y=p.y),
                decision=decision,
                classification=classification,
                evidence=evidence,
            )
        )

    return AdjudicateResponse(
        polygon=PolygonSummary(
            vertex_count=len(poly.vertices),
            edge_count=poly.edge_count,
            orientation=poly.orientation,
            signed_area2=poly.signed_area2,
        ),
        results=results,
    )


@app.post("/region-area-summary", response_model=RegionAreaSummaryResponse)
def region_area_summary(req: RegionAreaSummaryRequest) -> RegionAreaSummaryResponse:
    # 与裁决完全一致的多边形规整与口袋拓扑校验；非法时同一 422 信封。
    poly, pockets = prepare_region_and_pockets(req.region, req.permitted_pockets)
    region_area2 = doubled_area(poly)
    pocket_summaries = [
        PocketAreaSummary(pocket_index=i, area2=doubled_area(pocket))
        for i, pocket in enumerate(pockets)
    ]
    net_area2 = region_area2 - sum(p.area2 for p in pocket_summaries)
    return RegionAreaSummaryResponse(
        region_area2=region_area2,
        pockets=pocket_summaries,
        net_area2=net_area2,
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
