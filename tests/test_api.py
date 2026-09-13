"""HTTP 层契约测试：裁决结果、顺序保持、证据、422 错误信封。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(region, points):
    return client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in region]},
            "points": points,
        },
    )


def post_margin(region, points, margin):
    return client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in region]},
            "points": points,
            "exclusion_margin_cm": margin,
        },
    )


SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
SQUARE_CW = [(0, 0), (0, 10), (10, 10), (10, 0)]


def test_basic_decisions_preserve_input_order():
    pts = [(5, 5), (-1, 0), (10, 5), (0, 0), (5, 10), (20, 20)]
    r = post(SQUARE, [{"x": x, "y": y} for x, y in pts])
    assert r.status_code == 200
    data = r.json()
    assert [item["index"] for item in data["results"]] == list(range(len(pts)))
    assert [item["decision"] for item in data["results"]] == [
        "FORBIDDEN", "ALLOWED", "FORBIDDEN", "FORBIDDEN", "FORBIDDEN", "ALLOWED",
    ]
    assert [item["classification"] for item in data["results"]] == [
        "INSIDE", "OUTSIDE", "BOUNDARY", "BOUNDARY", "BOUNDARY", "OUTSIDE",
    ]


def test_boundary_evidence_smallest_edge_and_ray_evidence():
    r = post(SQUARE, [{"x": 0, "y": 0}, {"x": 10, "y": 5}, {"x": 5, "y": 5}, {"x": -5, "y": 5}])
    data = r.json()["results"]

    assert data[0]["evidence"]["type"] == "boundary"
    assert data[0]["evidence"]["edge_index"] == 0  # 顶点 0 命中边 0/3，取最小

    assert data[1]["evidence"]["edge_index"] == 1

    ray = data[2]["evidence"]
    assert ray["type"] == "horizontal_ray"
    assert ray["crossing_count"] == 1 and ray["parity"] == "odd"

    out = data[3]["evidence"]
    # 左外侧点的 +x 射线穿过整个矩形（边 3 入、边 1 出），2 为偶 => 外部。
    assert out["crossing_count"] == 2 and out["crossing_edges"] == [1, 3]
    assert out["parity"] == "even"


def test_orientation_reversal_identical_over_http():
    pts = [(i % 11 - 1, i % 13 - 2) for i in range(60)]
    a = post(SQUARE, [{"x": x, "y": y} for x, y in pts]).json()
    b = post(SQUARE_CW, [{"x": x, "y": y} for x, y in pts]).json()
    for ra, rb in zip(a["results"], b["results"]):
        assert ra["decision"] == rb["decision"]
        assert ra["classification"] == rb["classification"]
    assert a["polygon"]["orientation"] == "CCW"
    assert b["polygon"]["orientation"] == "CW"


def test_closing_point_equivalence():
    a = post(SQUARE, [{"x": 1, "y": 1}]).json()
    b = post(SQUARE + [SQUARE[0]], [{"x": 1, "y": 1}]).json()
    assert a["polygon"] == b["polygon"]
    assert a["results"][0]["decision"] == b["results"][0]["decision"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "vertices,code",
    [
        ([(0, 0), (1, 1), (1, 1), (2, 0)], "CONSECUTIVE_DUPLICATE_VERTEX"),
        ([(0, 0), (5, 0), (10, 0)], "ZERO_AREA_POLYGON"),
        ([(0, 0), (10, 10), (10, 0), (0, 8)], "SELF_INTERSECTING_POLYGON"),
    ],
)
def test_invalid_region_returns_422_envelope_no_partial_results(vertices, code):
    r = post(vertices, [{"x": 0, "y": 0}, {"x": 1, "y": 1}])
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert "results" not in body


def test_validation_error_envelope_for_bad_shape_and_range():
    r = client.post(
        "/adjudicate",
        json={"region": {"vertices": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}, "points": []},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    r2 = post(SQUARE, [{"x": 10**9, "y": 0}])
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "VALIDATION_ERROR"


def test_too_many_points_rejected():
    r = post(SQUARE, [{"x": 0, "y": 0}] * 501)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_non_integer_coordinates_rejected():
    # 布尔值、数字字符串、浮点（即便数值为整数 5.0）都不得被当成整数裁决。
    for bad in (True, False, "5", "5.0", 5.0, 1.5):
        r = post(SQUARE, [{"x": bad, "y": 0}])
        assert r.status_code == 422, bad
        body = r.json()
        assert body["error"]["code"] == "VALIDATION_ERROR", bad
        assert "results" not in body, bad

    # 区域顶点同样适用严格整数。
    for bad in (True, "10", 10.0):
        r = client.post(
            "/adjudicate",
            json={
                "region": {"vertices": [
                    {"x": 0, "y": 0}, {"x": bad, "y": 0},
                    {"x": 10, "y": 10}, {"x": 0, "y": 10},
                ]},
                "points": [{"x": 1, "y": 1}],
            },
        )
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "VALIDATION_ERROR", bad

    # 真正的整数必须正常受理（边界值另行由范围测试覆盖）。
    assert post(SQUARE, [{"x": -1, "y": 0}, {"x": 0, "y": 0}]).status_code == 200


def test_empty_points_allowed():
    r = post(SQUARE, [])
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert data["polygon"]["vertex_count"] == 4


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# exclusion_margin_cm 安全距离
# ---------------------------------------------------------------------------

def test_margin_midpoint_outside_edge_flips_to_forbidden():
    r = post_margin(SQUARE, [{"x": 5, "y": -3}, {"x": 5, "y": -4}], 3)
    assert r.status_code == 200
    near, far = r.json()["results"]
    # 边 0 中段外侧 3cm：距离恰等于阈值 -> 改判禁抛。
    assert near["index"] == 0
    assert near["decision"] == "FORBIDDEN"
    assert near["classification"] == "NEAR_BOUNDARY"
    ev = near["evidence"]
    assert ev["type"] == "near_boundary"
    assert ev["edge_index"] == 0
    assert ev["edge"] == [[0, 0], [10, 0]]
    assert (ev["distance2_num"], ev["distance2_den"]) == (9, 1)
    assert ev["exclusion_margin_cm"] == 3
    # 刚越过阈值（4cm > 3cm）-> 放行，证据保持原有射线形式。
    assert far["index"] == 1
    assert far["decision"] == "ALLOWED"
    assert far["classification"] == "OUTSIDE"
    assert far["evidence"]["type"] == "horizontal_ray"


def test_margin_vertex_proximity_uses_endpoint_distance():
    # (-2,-1) 最近的是顶点 (0,0)：到边 0 与边 3 的端点距离同为 sqrt(5)，取最小边序号。
    r = post_margin(SQUARE, [{"x": -2, "y": -1}], 3)
    item = r.json()["results"][0]
    assert item["classification"] == "NEAR_BOUNDARY"
    assert item["decision"] == "FORBIDDEN"
    assert item["evidence"]["edge_index"] == 0
    assert (item["evidence"]["distance2_num"], item["evidence"]["distance2_den"]) == (5, 1)
    # sqrt(5) > 2：阈值为 2 时正常放行。
    r2 = post_margin(SQUARE, [{"x": -2, "y": -1}], 2)
    assert r2.json()["results"][0]["decision"] == "ALLOWED"


def test_margin_slanted_edge_reduced_fraction_evidence():
    tri = [(0, 0), (10, 0), (0, 10)]
    r = post_margin(tri, [{"x": 8, "y": 5}], 3)
    item = r.json()["results"][0]
    assert item["classification"] == "NEAR_BOUNDARY"
    ev = item["evidence"]
    assert ev["edge_index"] == 1
    assert ev["edge"] == [[10, 0], [0, 10]]
    assert (ev["distance2_num"], ev["distance2_den"]) == (9, 2)  # 900/200 约分


def test_margin_inside_and_boundary_points_unaffected():
    pts = [{"x": 5, "y": 5}, {"x": 0, "y": 0}, {"x": 10, "y": 5}]
    r = post_margin(SQUARE, pts, 1000)
    data = r.json()["results"]
    assert [d["classification"] for d in data] == ["INSIDE", "BOUNDARY", "BOUNDARY"]
    assert [d["decision"] for d in data] == ["FORBIDDEN"] * 3
    assert data[0]["evidence"]["type"] == "horizontal_ray"
    assert data[1]["evidence"]["type"] == "boundary"
    assert data[2]["evidence"]["type"] == "boundary"


def test_margin_zero_and_missing_are_legacy_compatible():
    pts = [{"x": 5, "y": -1}, {"x": 5, "y": 5}, {"x": 0, "y": 0}, {"x": -2, "y": -1}]
    a = post(SQUARE, pts)
    b = post_margin(SQUARE, pts, 0)
    assert a.status_code == b.status_code == 200
    # 响应结构、分类结果、证据与点顺序完全一致（无 NEAR_BOUNDARY 出现）。
    assert a.json() == b.json()
    assert all(item["classification"] != "NEAR_BOUNDARY" for item in a.json()["results"])


def test_margin_orientation_reversal_edge_attribution_stable():
    pts = [{"x": 5, "y": -3}, {"x": -2, "y": -1}]
    a = post_margin(SQUARE, pts, 5).json()["results"]
    b = post_margin(SQUARE_CW, pts, 5).json()["results"]
    for ra, rb in zip(a, b):
        assert ra["classification"] == rb["classification"] == "NEAR_BOUNDARY"
        assert ra["decision"] == rb["decision"] == "FORBIDDEN"
        # 边序号随顶点顺序不同，但归属同一条几何边（端点集合一致）、平方距离一致。
        assert {tuple(e) for e in ra["evidence"]["edge"]} == {tuple(e) for e in rb["evidence"]["edge"]}
        assert (ra["evidence"]["distance2_num"], ra["evidence"]["distance2_den"]) == (
            rb["evidence"]["distance2_num"], rb["evidence"]["distance2_den"]
        )
    assert a[0]["evidence"]["edge_index"] == 0
    assert b[0]["evidence"]["edge_index"] == 3


@pytest.mark.parametrize("bad", [-1, -100, 1.5, 5.0, "3", True, False, 100_000_001])
def test_invalid_margin_values_rejected_422(bad):
    r = post_margin(SQUARE, [{"x": 5, "y": 5}], bad)
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "results" not in body


def test_margin_at_coordinate_limit_accepted():
    # 坐标上限本身合法；巨大安全距离下远处外部点也被改判。
    r = post_margin(SQUARE, [{"x": 20, "y": 20}], 100_000_000)
    assert r.status_code == 200
    assert r.json()["results"][0]["classification"] == "NEAR_BOUNDARY"
