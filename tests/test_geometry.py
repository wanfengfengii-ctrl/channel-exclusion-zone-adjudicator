"""app.geometry 的行为锁定测试。

覆盖：方向反转等价、闭合写法等价、大坐标（叉积可达 ~1e16）、
凹多边形、自交/退化拒绝、边界归属最小边序号、水平射线奇偶，
以及用 Fraction 精确参照实现对随机简单多边形做批量对拍。
"""

import random
from fractions import Fraction

import pytest

from app.geometry import (
    MAX_POINT_COUNT,
    PolygonError,
    classify,
    nearest_edge,
    prepare_polygon,
    segment_distance2,
    within_exclusion_margin,
)


# ---------------------------------------------------------------------------
# 固定用例
# ---------------------------------------------------------------------------

SQUARE_CCW = [(0, 0), (10, 0), (10, 10), (0, 10)]
SQUARE_CW = [(0, 0), (0, 10), (10, 10), (10, 0)]
L_SHAPE_CCW = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
L_SHAPE_CW = list(reversed(L_SHAPE_CCW))


def kinds(poly_pts, points):
    poly = prepare_polygon(poly_pts)
    return [classify(poly, x, y).kind for x, y in points]


def test_orientation_reversal_square_gives_same_conclusions():
    samples = [(5, 5), (-1, 5), (11, 5), (5, -1), (0, 0), (10, 5), (5, 10)]
    assert kinds(SQUARE_CCW, samples) == kinds(SQUARE_CW, samples)
    assert kinds(SQUARE_CCW, samples) == [
        "INSIDE", "OUTSIDE", "OUTSIDE", "OUTSIDE", "BOUNDARY", "BOUNDARY", "BOUNDARY",
    ]


def test_orientation_reversal_concave_gives_same_conclusions():
    # (8,8) 位于凹口内，必须判外；方向反转不能改变任何结论。
    samples = [(2, 2), (8, 2), (2, 8), (8, 8), (6, 6), (4, 4), (3, 7)]
    assert kinds(L_SHAPE_CCW, samples) == kinds(L_SHAPE_CW, samples)
    assert kinds(L_SHAPE_CCW, samples) == [
        "INSIDE", "INSIDE", "INSIDE", "OUTSIDE", "OUTSIDE", "BOUNDARY", "INSIDE",
    ]


def test_closing_first_point_is_equivalent():
    a = prepare_polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
    b = prepare_polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
    assert a.vertices == b.vertices
    assert a.signed_area2 == b.signed_area2 == 32
    assert a.orientation == "CCW"


def test_closing_point_duplicate_is_not_treated_as_consecutive_duplicate():
    # 末尾首点是合法闭合写法，不得报连续重复顶点。
    poly = prepare_polygon([(0, 0), (1, 0), (0, 1), (0, 0)])
    assert len(poly.vertices) == 3


@pytest.mark.parametrize(
    "vertices",
    [
        [(0, 0), (0, 0), (1, 0)],                       # 开头连续重复
        [(0, 0), (5, 0), (5, 5), (5, 5), (0, 5)],       # 中部连续重复
    ],
)
def test_consecutive_duplicate_vertices_rejected(vertices):
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(vertices)
    assert ei.value.code == "CONSECUTIVE_DUPLICATE_VERTEX"


def test_too_few_distinct_vertices_rejected():
    with pytest.raises(PolygonError) as ei:
        prepare_polygon([(0, 0), (5, 5)])  # pydantic 层之外，直接调算法给 2 点
    assert ei.value.code == "NOT_ENOUGH_DISTINCT_VERTICES"


def test_collinear_zero_area_rejected():
    with pytest.raises(PolygonError) as ei:
        prepare_polygon([(0, 0), (5, 0), (10, 0)])
    assert ei.value.code == "ZERO_AREA_POLYGON"


def test_too_many_vertices_rejected():
    pts = [(i, i * i % 7 - 3) for i in range(201)]
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(pts)
    assert ei.value.code == "TOO_MANY_VERTICES"


def test_bowtie_self_intersection_rejected():
    # 非对称蝴蝶结：有向面积非零，确保由自交规则（而非零面积规则）拒绝。
    bowtie = [(0, 0), (10, 10), (10, 0), (0, 8)]
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(bowtie)
    assert ei.value.code == "SELF_INTERSECTING_POLYGON"
    # 两条对角边 0 与 2 在内部交叉。
    assert ei.value.details["edge_indices"] == [0, 2]


def test_nonadjacent_vertex_touch_rejected():
    # 两个三角形在顶点 (5,5) 处相触：(5,5) 在序列中被两次非相邻访问。
    pinched = [(5, 5), (10, 0), (10, 10), (5, 5), (0, 10), (0, 0)]
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(pinched)
    assert ei.value.code == "SELF_INTERSECTING_POLYGON"


def test_spike_overlap_rejected():
    # 共线回退：(0,0)->(10,0)->(5,0) 后回到上方，边压到了另一条边上。
    spiked = [(0, 0), (10, 0), (5, 0), (5, 5), (0, 5)]
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(spiked)
    assert ei.value.code == "SELF_INTERSECTING_POLYGON"


def test_boundary_edge_interior_attributes_correct_index():
    poly = prepare_polygon(SQUARE_CCW)
    cases = {
        (5, 0): 0,
        (10, 5): 1,
        (5, 10): 2,
        (0, 5): 3,
    }
    for (x, y), edge in cases.items():
        c = classify(poly, x, y)
        assert c.kind == "BOUNDARY"
        assert c.boundary_edge == edge


def test_boundary_vertex_returns_smallest_incident_edge():
    poly = prepare_polygon(SQUARE_CCW)
    # 顶点 k 同时命中边 k-1 与边 k（顶点 0 命中边 3 与边 0），取最小序号。
    expected = {0: 0, 1: 0, 2: 1, 3: 2}
    for k, edge in expected.items():
        c = classify(poly, *SQUARE_CCW[k])
        assert c.kind == "BOUNDARY"
        assert c.boundary_edge == edge


def test_boundary_point_on_non_axis_aligned_edge():
    # 三角形斜边 x+y=10 上的整数点。
    tri = prepare_polygon([(0, 0), (10, 0), (0, 10)])
    c = classify(tri, 3, 7)
    assert c.kind == "BOUNDARY"
    assert c.boundary_edge == 1
    assert classify(tri, 2, 7).kind == "INSIDE"   # x+y=9
    assert classify(tri, 3, 8).kind == "OUTSIDE"  # x+y=11


def test_ray_evidence_parity():
    poly = prepare_polygon(SQUARE_CCW)
    inside = classify(poly, 5, 5)
    assert len(inside.crossing_edges) == 1 and inside.kind == "INSIDE"
    outside = classify(poly, -5, 5)
    assert len(outside.crossing_edges) % 2 == 0 and outside.kind == "OUTSIDE"
    # 射线在凹口点 (8,8) 处向右不穿任何边（右侧边界只到 y=4），0 为偶 => 外部。
    concave_out = classify(prepare_polygon(L_SHAPE_CCW), 8, 8)
    assert concave_out.kind == "OUTSIDE"
    assert len(concave_out.crossing_edges) == 0


def test_points_aligned_with_horizontal_edge_but_outside_segment():
    poly = prepare_polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert classify(poly, -5, 0).kind == "OUTSIDE"
    assert classify(poly, 15, 0).kind == "OUTSIDE"
    assert classify(poly, -5, 10).kind == "OUTSIDE"
    # 与某条水平边共线但位于多边形内部投影之外，不得误判为边界。
    poly2 = prepare_polygon([(0, 0), (6, 0), (6, 6), (4, 6), (4, 4), (0, 4)])
    assert classify(poly2, 5, 4).kind == "INSIDE"
    assert classify(poly2, -1, 4).kind == "OUTSIDE"


def test_large_coordinates_exact_integer_arithmetic():
    B = 100_000_000
    big_square = prepare_polygon([(-B, -B), (B, -B), (B, B), (-B, B)])
    assert classify(big_square, 0, 0).kind == "INSIDE"
    assert classify(big_square, B, 0).kind == "BOUNDARY"
    assert classify(big_square, -B, -B).kind == "BOUNDARY"
    assert classify(big_square, B, B + 1).kind == "OUTSIDE"

    # 大坐标三角形斜边 x+y=B：最近的整数点在两侧与线上，必须严格区分。
    tri = prepare_polygon([(0, 0), (B, 0), (0, B)])
    assert classify(tri, B // 2 - 1, B // 2).kind == "INSIDE"
    assert classify(tri, B // 2, B // 2).kind == "BOUNDARY"
    assert classify(tri, B // 2 + 1, B // 2).kind == "OUTSIDE"


# ---------------------------------------------------------------------------
# Fraction 精确参照 + 随机简单多边形对拍
# ---------------------------------------------------------------------------

def reference_classify(vertices, px, py):
    """与方向无关的精确参照：边界优先 + Fraction 水平射线奇偶。"""
    p = Fraction(px), Fraction(py)
    m = len(vertices)
    for i in range(m):
        ax, ay = vertices[i]
        bx, by = vertices[(i + 1) % m]
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        if cross == 0 and min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by):
            return "BOUNDARY"
    crossings = 0
    for i in range(m):
        ax, ay = vertices[i]
        bx, by = vertices[(i + 1) % m]
        if (ay > py) != (by > py):
            xint = ax + Fraction(bx - ax) * Fraction(py - ay, by - ay)
            if xint > px:
                crossings += 1
    return "INSIDE" if crossings % 2 else "OUTSIDE"


def random_star_polygon(rng):
    """绕中心按极角排列、半径轻微抖动的星形多边形；若仍自交则返回 None。"""
    n = rng.randint(3, 12)
    cx, cy = rng.randint(-50, 50), rng.randint(-50, 50)
    base_r = rng.randint(20, 80)
    angles = sorted(rng.uniform(0, 6.28318) for _ in range(n))
    pts = []
    for a in angles:
        r = base_r * rng.uniform(0.85, 1.0)
        pts.append((int(cx + r * __import__("math").cos(a)),
                    int(cy + r * __import__("math").sin(a))))
    if len(set(pts)) < 3:
        return None
    try:
        prepare_polygon(pts)
    except PolygonError:
        return None
    return pts


def test_random_polygons_match_exact_reference():
    rng = random.Random(20260913)
    checked = 0
    for _ in range(300):
        pts = random_star_polygon(rng)
        if pts is None:
            continue
        for rev in (pts, list(reversed(pts))):
            poly = prepare_polygon(rev)
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            probes = [(x + dx, y + dy) for x, y in pts for dx in (-2, 0, 2) for dy in (-2, 0, 2)]
            probes += [(rng.randint(min(xs) - 5, max(xs) + 5),
                        rng.randint(min(ys) - 5, max(ys) + 5)) for _ in range(40)]
            for px, py in probes:
                got = classify(poly, px, py).kind
                want = reference_classify(rev, px, py)
                assert got == want, f"{rev} point={(px,py)} got={got} want={want}"
                checked += 1
    assert checked > 1000


def test_max_point_constant_is_500():
    assert MAX_POINT_COUNT == 500


# ---------------------------------------------------------------------------
# 安全距离：点到线段的整数平方距离
# ---------------------------------------------------------------------------

def test_segment_distance2_perpendicular_interior_foot():
    # 垂足落在线段内：平方距离 = cross² / |AB|²，按未约分形式返回。
    assert segment_distance2(5, 3, 0, 0, 10, 0) == (900, 100)  # 即 9
    # 斜线段上的真分数：dist² = 9/10。
    assert segment_distance2(0, 1, 0, 0, 3, 1) == (9, 10)


def test_segment_distance2_clamps_to_endpoints():
    assert segment_distance2(-4, 3, 0, 0, 10, 0) == (25, 1)   # 垂足在 A 外 -> 端点 A
    assert segment_distance2(14, 3, 0, 0, 10, 0) == (25, 1)   # 垂足在 B 外 -> 端点 B
    assert segment_distance2(0, 0, 0, 0, 10, 0) == (0, 1)     # 端点上距离为 0


def test_nearest_edge_picks_segment_and_reduces_fraction():
    poly = prepare_polygon(SQUARE_CCW)
    near = nearest_edge(poly, 5, -3)
    assert (near.edge_index, near.dist2_num, near.dist2_den) == (0, 9, 1)

    # 斜边 x+y=10 外侧：dist² = 900/200，证据必须约分为 9/2。
    tri = prepare_polygon([(0, 0), (10, 0), (0, 10)])
    near = nearest_edge(tri, 8, 5)
    assert (near.edge_index, near.dist2_num, near.dist2_den) == (1, 9, 2)


def test_nearest_edge_vertex_tie_returns_smallest_index():
    poly = prepare_polygon(SQUARE_CCW)
    # (-2,-1) 最近的是顶点 (0,0)：边 0 与边 3 的端点距离同为 sqrt(5)，取最小序号。
    near = nearest_edge(poly, -2, -1)
    assert (near.edge_index, near.dist2_num, near.dist2_den) == (0, 5, 1)


def test_nearest_edge_orientation_reversal_attributes_same_segment():
    a = nearest_edge(prepare_polygon(SQUARE_CCW), 5, -3)
    b = nearest_edge(prepare_polygon(SQUARE_CW), 5, -3)
    assert (a.dist2_num, a.dist2_den) == (b.dist2_num, b.dist2_den) == (9, 1)
    # 边序号随顶点顺序变化，但归属的是同一条几何边（底边）。
    assert (a.edge_index, b.edge_index) == (0, 3)
    assert SQUARE_CW[3] == (10, 0) and SQUARE_CW[0] == (0, 0)


def test_within_exclusion_margin_exact_threshold():
    near = nearest_edge(prepare_polygon(SQUARE_CCW), 5, -3)  # dist² = 9
    assert within_exclusion_margin(near, 3)        # 距离恰等于阈值 -> 命中
    assert not within_exclusion_margin(near, 2)    # 刚越过阈值 -> 放行
    # 分数距离 dist² = 9/2：margin=2 -> 4 < 4.5 不命中；margin=3 -> 9 >= 4.5 命中。
    tri_near = nearest_edge(prepare_polygon([(0, 0), (10, 0), (0, 10)]), 8, 5)
    assert not within_exclusion_margin(tri_near, 2)
    assert within_exclusion_margin(tri_near, 3)


def reference_segment_dist2(px, py, ax, ay, bx, by):
    """Fraction 精确参照：点到线段的最短平方距离。"""
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    t = Fraction((px - ax) * dx + (py - ay) * dy, len2)
    if t <= 0:
        return Fraction((px - ax) ** 2 + (py - ay) ** 2)
    if t >= 1:
        return Fraction((px - bx) ** 2 + (py - by) ** 2)
    qx = ax + t * dx  # 垂足
    qy = ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def test_random_nearest_edge_matches_fraction_reference():
    rng = random.Random(20260914)
    checked = 0
    for _ in range(300):
        pts = random_star_polygon(rng)
        if pts is None:
            continue
        poly = prepare_polygon(pts)
        m = len(pts)
        probes = [(rng.randint(-160, 160), rng.randint(-160, 160)) for _ in range(30)]
        for px, py in probes:
            dists = [
                reference_segment_dist2(px, py, *pts[i], *pts[(i + 1) % m])
                for i in range(m)
            ]
            best = min(dists)
            want_idx = dists.index(best)  # 首个最小值 <=> 同距取最小边序号
            near = nearest_edge(poly, px, py)
            assert near.edge_index == want_idx, f"{pts} point={(px,py)}"
            assert Fraction(near.dist2_num, near.dist2_den) == best
            checked += 1
    assert checked > 1000
