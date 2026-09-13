"""FastAPI 入口：抛泥点裁决服务。

POST /adjudicate
    入参::

        {"region": {"vertices": [{"x": .., "y": ..}, ...]},
         "points": [{"x": .., "y": ..}, ...]}

    区域非法时整个请求返回 422，绝不返回部分结果；区域合法时按输入
    顺序给出每个点的裁决与证据。
GET /healthz
    存活探针，供 Docker Compose 的 verify 服务等待 API 就绪。
"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .geometry import Classification, PolygonError, classify, prepare_polygon
from .models import AdjudicateRequest, AdjudicateResponse, PointModel, PointResult, PolygonSummary

app = FastAPI(
    title="Dredging Spoil Dumping Adjudication Service",
    version="1.0.0",
    description="纯整数计算几何：判定点位于禁抛区内部、外部还是边界。",
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


@app.post("/adjudicate", response_model=AdjudicateResponse)
def adjudicate(req: AdjudicateRequest) -> AdjudicateResponse:
    raw = [[v.x, v.y] for v in req.region.vertices]
    poly = prepare_polygon(raw)  # 非法直接抛 PolygonError -> 422，无部分结果

    results: list[PointResult] = []
    for i, p in enumerate(req.points):
        cls = classify(poly, p.x, p.y)
        if cls.kind == "BOUNDARY":
            decision = "FORBIDDEN"
            evidence = edge_evidence(poly, cls.boundary_edge)
        else:
            decision = "FORBIDDEN" if cls.kind == "INSIDE" else "ALLOWED"
            evidence = ray_evidence(cls)
        results.append(
            PointResult(
                index=i,
                point=PointModel(x=p.x, y=p.y),
                decision=decision,
                classification=cls.kind,
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


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
