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
# CW 变体取顶点序列的反转（与 scripts/acceptance.py 一致）；顶点等距平局按
# 最小边序号归因，锚点不同的编号方案会得到不同的边序号，因此这里不循环平移锚点。
SQUARE_CW = list(reversed(SQUARE))


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
    # 反转后的顶点序列为 [(0,10),(10,10),(10,0),(0,0)]，底边序号为 2。
    assert b[0]["evidence"]["edge_index"] == 2


def test_margin_vertex_tie_follows_smallest_index_legacy_rule():
    # 旧版本行为锁定：顶点等距区（(-2,-1) 到顶点 (0,0) 两侧邻边同为 sqrt(5)）
    # 一律取最小边序号。锚在 (0,0) 的 CW 区域左边为边 0，归到左边而非底边——
    # 未提交许可口袋时响应必须与原版本完全一致。
    cw_anchored = [(0, 0), (0, 10), (10, 10), (10, 0)]
    r = post_margin(cw_anchored, [{"x": -2, "y": -1}], 5)
    item = r.json()["results"][0]
    assert item["classification"] == "NEAR_BOUNDARY"
    assert item["evidence"]["edge_index"] == 0
    assert item["evidence"]["edge"] == [[0, 0], [0, 10]]
    assert (item["evidence"]["distance2_num"], item["evidence"]["distance2_den"]) == (5, 1)


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


# ---------------------------------------------------------------------------
# 未声明字段：任何层级都必须整单 422，禁止静默丢弃后按默认值裁决
# ---------------------------------------------------------------------------

def _issues(body):
    return body["error"]["details"]["issues"]


def _locs(body):
    return [tuple(issue["loc"]) for issue in _issues(body)]


def test_misspelled_margin_field_rejected_instead_of_zero_margin():
    # 近边外部点在 3cm 安全距离下本应改判 FORBIDDEN；误拼字段名时
    # 不得静默按缺省 0 裁决并放行，必须整单校验失败。
    r = client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in SQUARE]},
            "points": [{"x": 5, "y": -3}],
            "exclushun_margin_cm": 3,
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "results" not in body
    assert ("body", "exclushun_margin_cm") in _locs(body)
    assert all(
        issue.get("type") == "extra_forbidden" and issue["loc"][-1] == "exclushun_margin_cm"
        for issue in _issues(body)
    )


def test_undeclared_coordinate_field_on_point_rejects_whole_order():
    r = client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in SQUARE]},
            "points": [{"x": 1, "y": 1}, {"x": 5, "y": -3, "z": 1}],
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "results" not in body
    # 错误定位到具体待判点的未声明字段。
    assert ("body", "points", 1, "z") in _locs(body)


def test_undeclared_field_on_region_rejects_and_locates_region_field():
    r = client.post(
        "/adjudicate",
        json={
            "region": {
                "vertices": [{"x": x, "y": y} for x, y in SQUARE],
                "zone_code": "D-07",
            },
            "points": [{"x": 1, "y": 1}],
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "results" not in body
    assert ("body", "region", "zone_code") in _locs(body)


def test_undeclared_field_on_region_vertex_rejected():
    r = client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [
                {"x": 0, "y": 0, "elevation": 0},
                {"x": 10, "y": 0}, {"x": 10, "y": 10}, {"x": 0, "y": 10},
            ]},
            "points": [],
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert ("body", "region", "vertices", 0, "elevation") in _locs(body)


# ---------------------------------------------------------------------------
# permitted_pockets 许可口袋
# ---------------------------------------------------------------------------

BIG = [(0, 0), (100, 0), (100, 100), (0, 100)]
POCKET_A = [(10, 10), (20, 10), (20, 20), (10, 20)]
POCKET_B = [(40, 40), (50, 40), (50, 50), (40, 50)]


def post_pockets(region, points, pockets, margin=None):
    payload = {
        "region": {"vertices": [{"x": x, "y": y} for x, y in region]},
        "points": points,
        "permitted_pockets": [
            {"vertices": [{"x": x, "y": y} for x, y in pocket]} for pocket in pockets
        ],
    }
    if margin is not None:
        payload["exclusion_margin_cm"] = margin
    return client.post("/adjudicate", json=payload)


def ngon(n, cx, cy, r):
    from math import cos, pi, sin
    return [(cx + round(r * cos(2 * pi * k / n)),
             cy + round(r * sin(2 * pi * k / n))) for k in range(n)]


def test_pocket_interior_points_allowed_with_input_order_index():
    r = post_pockets(
        BIG,
        [{"x": 15, "y": 15}, {"x": 45, "y": 45}, {"x": 30, "y": 30}, {"x": 150, "y": 150}],
        [POCKET_A, POCKET_B],
    )
    assert r.status_code == 200
    a, b, c, d = r.json()["results"]
    # 口袋内部点放行，证据按输入顺序携带口袋序号。
    assert a["decision"] == "ALLOWED" and a["classification"] == "PERMITTED_POCKET"
    assert a["evidence"]["type"] == "permitted_pocket" and a["evidence"]["pocket_index"] == 0
    assert b["decision"] == "ALLOWED" and b["classification"] == "PERMITTED_POCKET"
    assert b["evidence"]["pocket_index"] == 1
    # 区域内部、口袋之外的点维持原裁决。
    assert c["decision"] == "FORBIDDEN" and c["classification"] == "INSIDE"
    assert c["evidence"]["type"] == "horizontal_ray"
    # 区域外部点不受口袋影响。
    assert d["decision"] == "ALLOWED" and d["classification"] == "OUTSIDE"
    assert d["evidence"]["type"] == "horizontal_ray"


def test_pocket_boundary_points_still_forbidden_via_original_chain():
    # 口袋的边与顶点上的点：仍按禁抛区内部处理（FORBIDDEN/INSIDE + 射线证据）。
    pts = [{"x": 10, "y": 15}, {"x": 20, "y": 20}, {"x": 15, "y": 10}]
    r = post_pockets(BIG, pts, [POCKET_A])
    assert r.status_code == 200
    for item in r.json()["results"]:
        assert item["decision"] == "FORBIDDEN"
        assert item["classification"] == "INSIDE"
        assert item["evidence"]["type"] == "horizontal_ray"


def test_pocket_margin_shrinks_permission_toward_interior():
    # 安全距离 3：许可范围向口袋内部收缩。
    # (12,15) 距左边 2 <= 3 -> 禁抛；(13,15) 距左边 3 == 3 -> 禁抛；(15,15) 距最近边 5 > 3 -> 放行。
    r = post_pockets(BIG, [{"x": 12, "y": 15}, {"x": 13, "y": 15}, {"x": 15, "y": 15}],
                     [POCKET_A], margin=3)
    assert r.status_code == 200
    near, edge_case, free = r.json()["results"]
    assert near["decision"] == "FORBIDDEN" and near["classification"] == "NEAR_BOUNDARY"
    ev = near["evidence"]
    assert ev["type"] == "near_boundary" and ev["pocket_index"] == 0
    assert ev["edge_index"] == 3 and ev["edge"] == [[10, 20], [10, 10]]
    assert (ev["distance2_num"], ev["distance2_den"]) == (4, 1)
    assert ev["exclusion_margin_cm"] == 3
    assert edge_case["decision"] == "FORBIDDEN" and edge_case["classification"] == "NEAR_BOUNDARY"
    assert (edge_case["evidence"]["distance2_num"], edge_case["evidence"]["distance2_den"]) == (9, 1)
    assert free["decision"] == "ALLOWED" and free["classification"] == "PERMITTED_POCKET"


def test_pocket_margin_does_not_leak_to_region_boundary_evidence():
    # 区域外部点的安全距离证据仍相对禁抛区边，不夹带口袋序号。
    r = post_pockets(BIG, [{"x": 5, "y": -3}], [POCKET_A], margin=3)
    item = r.json()["results"][0]
    assert item["classification"] == "NEAR_BOUNDARY" and item["decision"] == "FORBIDDEN"
    assert "pocket_index" not in item["evidence"]
    assert item["evidence"]["edge_index"] == 0


@pytest.mark.parametrize(
    "second",
    [
        [(15, 15), (25, 15), (25, 25), (15, 25)],   # 边交叉重叠
        [(20, 10), (30, 10), (30, 20), (20, 20)],   # 共边接触
        [(12, 12), (18, 12), (18, 18), (12, 18)],   # 嵌套
    ],
)
def test_pockets_intersect_rejects_whole_order(second):
    r = post_pockets(BIG, [{"x": 15, "y": 15}], [POCKET_A, second])
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "POCKETS_INTERSECT"
    assert body["error"]["details"]["pocket_indices"] == [0, 1]
    assert "results" not in body


def test_pocket_outside_region_rejects_whole_order():
    r = post_pockets(BIG, [{"x": 15, "y": 15}], [[(90, 90), (110, 90), (110, 110), (90, 110)]])
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "POCKET_NOT_INSIDE_REGION"
    assert body["error"]["details"]["pocket_index"] == 0
    assert "results" not in body


def test_pocket_topology_error_carries_index_and_rejects_whole_order():
    bowtie = [(10, 10), (20, 20), (20, 10), (10, 18)]
    r = post_pockets(BIG, [{"x": 15, "y": 15}], [POCKET_A, bowtie])
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "SELF_INTERSECTING_POLYGON"
    assert body["error"]["details"]["pocket_index"] == 1
    assert "results" not in body


def test_eleven_pockets_rejected_with_count():
    pockets = [[(10 + 8 * k, 10), (16 + 8 * k, 10), (16 + 8 * k, 16), (10 + 8 * k, 16)]
               for k in range(11)]
    r = post_pockets(BIG, [{"x": 15, "y": 15}], pockets)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "TOO_MANY_POCKETS"
    assert body["error"]["details"]["pocket_count"] == 11
    assert "results" not in body


def test_total_vertex_count_over_500_rejected_with_count():
    region = [(0, 0), (40000, 0), (40000, 5000), (0, 5000)]
    pockets = [ngon(50, 2000 + 3000 * k, 2500, 1000) for k in range(10)]
    # 4 + 10 * 50 = 504 > 500：整单 422 并给出总顶点计数。
    r = post_pockets(region, [], pockets)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "TOO_MANY_TOTAL_VERTICES"
    assert body["error"]["details"]["total_vertex_count"] == 504
    assert "results" not in body


def test_total_vertex_count_exactly_500_accepted():
    region = [(0, 0), (40000, 0), (40000, 5000), (0, 5000)]
    pockets = [ngon(50, 2000 + 3000 * k, 2500, 1000) for k in range(9)]
    pockets.append(ngon(46, 29000, 2500, 1000))
    # 4 + 9 * 50 + 46 = 500：恰好达到上限，正常裁决。
    r = post_pockets(region, [{"x": 2000, "y": 2500}, {"x": 29000, "y": 2500}], pockets)
    assert r.status_code == 200
    first, last = r.json()["results"]
    assert first["classification"] == "PERMITTED_POCKET" and first["evidence"]["pocket_index"] == 0
    assert last["classification"] == "PERMITTED_POCKET" and last["evidence"]["pocket_index"] == 9


def test_missing_null_and_empty_pockets_are_legacy_compatible():
    pts = [{"x": 15, "y": 15}, {"x": 5, "y": -1}, {"x": 0, "y": 0}, {"x": 150, "y": 150}]
    baseline = post(BIG, pts)
    assert baseline.status_code == 200
    # 显式空列表与 null 均等同于未提交：响应逐字节一致，且不出现口袋分类。
    assert post_pockets(BIG, pts, []).json() == baseline.json()
    r_null = client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in BIG]},
            "points": pts,
            "permitted_pockets": None,
        },
    )
    assert r_null.json() == baseline.json()
    assert all(item["classification"] != "PERMITTED_POCKET" for item in baseline.json()["results"])


def test_undeclared_field_on_pocket_rejected():
    r = client.post(
        "/adjudicate",
        json={
            "region": {"vertices": [{"x": x, "y": y} for x, y in BIG]},
            "points": [],
            "permitted_pockets": [
                {"vertices": [{"x": x, "y": y} for x, y in POCKET_A], "label": "P-1"}
            ],
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert ("body", "permitted_pockets", 0, "label") in _locs(body)


def test_pocket_vertex_rules_match_region_rules():
    # 非整数坐标拒绝、末尾闭合写法等价，均与区域规则一致。
    r = post_pockets(BIG, [], [[(10, 10), (20, 10), (20, 20), (10, 20), (10, 10)]])
    assert r.status_code == 200
    for bad in (True, "10", 10.0):
        r_bad = post_pockets(BIG, [], [[(10, 10), (20, 10), (bad, 20), (10, 20)]])
        assert r_bad.status_code == 422, bad
        assert r_bad.json()["error"]["code"] == "VALIDATION_ERROR", bad


# ---------------------------------------------------------------------------
# POST /region-area-summary 面积汇总
# ---------------------------------------------------------------------------

def post_summary(region, pockets=None, **extra):
    payload = {"region": {"vertices": [{"x": x, "y": y} for x, y in region]}}
    if pockets is not None:
        payload["permitted_pockets"] = [
            {"vertices": [{"x": x, "y": y} for x, y in pocket]} for pocket in pockets
        ]
    payload.update(extra)
    return client.post("/region-area-summary", json=payload)


def test_area_summary_rectangle_with_two_pockets_conserves_area():
    r = post_summary(BIG, pockets=[POCKET_A, POCKET_B])
    assert r.status_code == 200
    data = r.json()
    assert data["region_area2"] == 20000  # 100cm x 100cm 的二倍
    # 各口袋按输入顺序携带 pocket_index，面积为各自绝对二倍面积。
    assert data["pockets"] == [
        {"pocket_index": 0, "area2": 200},
        {"pocket_index": 1, "area2": 200},
    ]
    # 面积守恒：汇总值 = 外环绝对二倍面积 - 全部口袋绝对二倍面积。
    assert data["net_area2"] == 20000 - 200 - 200 == 19600
    assert data["net_area2"] == data["region_area2"] - sum(p["area2"] for p in data["pockets"])


def test_area_summary_orientation_and_closing_point_invariant():
    # 顺/逆时针与末尾重复闭合点写法（区域与口袋同时变体）给出完全一致的响应。
    base = post_summary(BIG, pockets=[POCKET_A, POCKET_B]).json()
    cw = post_summary(list(reversed(BIG)),
                      pockets=[list(reversed(POCKET_A)), list(reversed(POCKET_B))]).json()
    closed = post_summary(BIG + [BIG[0]],
                          pockets=[POCKET_A + [POCKET_A[0]], POCKET_B + [POCKET_B[0]]]).json()
    assert base == cw == closed


def test_area_summary_without_pockets_defaults():
    # 缺省、显式 null、空列表均视为无口袋：汇总值等于外环面积。
    r_null = post_summary(BIG, permitted_pockets=None)
    for r in (post_summary(BIG), post_summary(BIG, pockets=[]), r_null):
        assert r.status_code == 200
        data = r.json()
        assert data["region_area2"] == 20000
        assert data["pockets"] == []
        assert data["net_area2"] == data["region_area2"]


def test_area_summary_large_coordinates_exact():
    B = 100_000_000
    r = post_summary([(-B, -B), (B, -B), (B, B), (-B, B)])
    assert r.status_code == 200
    assert r.json()["region_area2"] == 2 * (2 * B) * (2 * B)  # 8e16，整数精确


def test_area_summary_invalid_region_422_envelope():
    r = post_summary([(0, 0), (10, 10), (10, 0), (0, 8)])
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "SELF_INTERSECTING_POLYGON"


def test_area_summary_invalid_pocket_rejected_and_located():
    # 第二个口袋为蝴蝶结：整单 422，details 精确定位到口袋序号。
    bowtie = [(40, 40), (50, 50), (50, 40), (40, 48)]
    r = post_summary(BIG, pockets=[POCKET_A, bowtie])
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "SELF_INTERSECTING_POLYGON"
    assert body["error"]["details"]["pocket_index"] == 1

    # 口袋越出禁抛区：POCKET_NOT_INSIDE_REGION 并给出口袋序号。
    r = post_summary(BIG, pockets=[[(90, 90), (110, 90), (110, 110), (90, 110)]])
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "POCKET_NOT_INSIDE_REGION"
    assert body["error"]["details"]["pocket_index"] == 0

    # 口袋两两接触：POCKETS_INTERSECT 并给出两个口袋序号。
    touching = [(20, 10), (30, 10), (30, 20), (20, 20)]
    r = post_summary(BIG, pockets=[POCKET_A, touching])
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "POCKETS_INTERSECT"
    assert body["error"]["details"]["pocket_indices"] == [0, 1]


def test_area_summary_count_limits_rejected():
    # 11 个口袋：TOO_MANY_POCKETS 并给出计数。
    eleven = [[(10 + 8 * k, 10), (16 + 8 * k, 10), (16 + 8 * k, 16), (10 + 8 * k, 16)]
              for k in range(11)]
    r = post_summary(BIG, pockets=eleven)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "TOO_MANY_POCKETS"
    assert body["error"]["details"]["pocket_count"] == 11

    # 外环与口袋总顶点 504 > 500：TOO_MANY_TOTAL_VERTICES 并给出总计数。
    region = [(0, 0), (40000, 0), (40000, 5000), (0, 5000)]
    pockets = [ngon(50, 2000 + 3000 * k, 2500, 1000) for k in range(10)]
    r = post_summary(region, pockets=pockets)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "TOO_MANY_TOTAL_VERTICES"
    assert body["error"]["details"]["total_vertex_count"] == 504


def test_area_summary_rejects_points_and_margin_fields():
    # 本接口不接收待判点与安全距离：未声明字段整单 422 并精确定位。
    r = post_summary(BIG, points=[{"x": 1, "y": 1}])
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert ("body", "points") in _locs(body)

    r = post_summary(BIG, exclusion_margin_cm=3)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert ("body", "exclusion_margin_cm") in _locs(body)


# ---------------------------------------------------------------------------
# 顶点/数量上限：计数校验在几何层按规整后的顶点裁决，不被通用字段错误掩盖
# ---------------------------------------------------------------------------

def test_region_201_distinct_plus_closing_point_reports_vertex_limit():
    # 201 个不同顶点 + 末尾重复首点闭合 = 202 条原始条目：不得被通用字段
    # 错误（too_long）掩盖，必须明确返回外环顶点数量超限。
    ring = [(i, i * i % 7 - 3) for i in range(201)]
    closed = ring + [ring[0]]
    for r in (post(closed, []), post_summary(closed)):
        assert r.status_code == 422
        body = r.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == "TOO_MANY_VERTICES"
        assert body["error"]["details"]["vertex_count"] == 201
        assert "results" not in body


def test_region_200_distinct_plus_closing_point_accepted():
    # 200 个不同顶点 + 末尾闭合点 = 201 条原始条目：合法上限，两个接口都受理。
    region = ngon(200, 100_000, 100_000, 50_000)
    closed = region + [region[0]]
    r = post(closed, [{"x": 100_000, "y": 100_000}])
    assert r.status_code == 200
    assert r.json()["polygon"]["vertex_count"] == 200
    assert post_summary(closed).status_code == 200


def test_over_limit_pocket_with_closing_point_reports_vertex_limit_and_index():
    # 超限口袋（201 个不同顶点 + 末尾闭合）：不得报通用"列表过长"，
    # 必须返回口袋顶点超限并保留口袋序号。
    big_pocket = [(i, 500 + i * i % 7) for i in range(201)]
    pockets = [POCKET_A, big_pocket + [big_pocket[0]]]
    for r in (post_pockets(BIG, [], pockets), post_summary(BIG, pockets=pockets)):
        assert r.status_code == 422
        body = r.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == "TOO_MANY_VERTICES"
        assert body["error"]["details"]["pocket_index"] == 1
        assert body["error"]["details"]["vertex_count"] == 201
        assert "results" not in body


def test_pocket_200_distinct_plus_closing_point_accepted():
    # 口袋侧合法上限：200 个不同顶点 + 末尾闭合点 = 201 条原始条目，正常受理。
    region = [(0, 0), (40000, 0), (40000, 5000), (0, 5000)]
    pocket = ngon(200, 20000, 2500, 2000)
    r = post_summary(region, pockets=[pocket + [pocket[0]]])
    assert r.status_code == 200
    assert r.json()["pockets"][0]["pocket_index"] == 0


def test_eleven_pockets_with_insufficient_vertices_reports_count_first():
    # 11 个顶点不足的口袋：整单数量限制必须先于逐口袋顶点校验，
    # 不得返回 11 条局部顶点错误。
    pockets = [[(10 + k, 10), (20 + k, 10)] for k in range(11)]
    for r in (post_pockets(BIG, [], pockets), post_summary(BIG, pockets=pockets)):
        assert r.status_code == 422
        body = r.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == "TOO_MANY_POCKETS"
        assert body["error"]["details"]["pocket_count"] == 11
        assert "issues" not in body["error"].get("details", {})
        assert "results" not in body


def test_pocket_with_too_few_vertices_reports_indexed_geometry_error():
    # 单个顶点不足的口袋：由几何层给出显式错误并携带口袋序号。
    for r in (post_pockets(BIG, [], [POCKET_A, [(50, 50), (60, 60)]]),
              post_summary(BIG, pockets=[POCKET_A, [(50, 50), (60, 60)]])):
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "NOT_ENOUGH_DISTINCT_VERTICES"
        assert body["error"]["details"]["pocket_index"] == 1
