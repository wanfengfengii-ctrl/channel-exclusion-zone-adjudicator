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
    r = post(SQUARE, [{"x": 1.5, "y": 0}])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_empty_points_allowed():
    r = post(SQUARE, [])
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert data["polygon"]["vertex_count"] == 4


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
