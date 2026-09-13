"""app.geometry 的行为锁定测试。

覆盖：方向反转等价、闭合写法等价、大坐标（叉积可达 ~1e16）、
凹多边形、自交/退化拒绝、边界归属最小边序号、水平射线奇偶，
以及用 Fraction 精确参照实现对随机简单多边形做批量对拍。
"""

import random
from fractions import Fraction
from math import cos, pi, sin

import pytest

from app.geometry import (
    MAX_POINT_COUNT,
    MAX_POCKET_COUNT,
    MAX_TOTAL_VERTEX_COUNT,
    PolygonError,
    classify,
    containing_pocket,
    doubled_area,
    nearest_edge,
    prepare_pockets,
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


def test_too_many_vertices_with_closing_point_rejected_after_normalization():
    # 201 个不同顶点 + 末尾重复首点：闭合点先被规整丢弃，再按 201 个不同
    # 顶点裁决超限——不得按 202 条原始条目计数，也不得漏报。
    pts = [(i, i * i % 7 - 3) for i in range(201)]
    with pytest.raises(PolygonError) as ei:
        prepare_polygon(pts + [pts[0]])
    assert ei.value.code == "TOO_MANY_VERTICES"
    assert ei.value.details["vertex_count"] == 201


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


def test_nearest_edge_vertex_tie_follows_smallest_index_when_renumbered():
    # 旧版本行为锁定：顶点等距平局一律取最小边序号。SQUARE_CW 锚在 (0,0)，
    # 左边成为边 0，归因跟随顶点编号（底边在 CW 下序号为 3，不参与平局裁决）。
    near = nearest_edge(prepare_polygon(SQUARE_CW), -2, -1)
    assert (near.edge_index, near.dist2_num, near.dist2_den) == (0, 5, 1)
    assert SQUARE_CW[0] == (0, 0) and SQUARE_CW[1] == (0, 10)  # 边 0 是左边


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


# ---------------------------------------------------------------------------
# 许可口袋：规整、包含校验、互斥校验与按输入顺序归因
# ---------------------------------------------------------------------------

REGION_100 = [(0, 0), (100, 0), (100, 100), (0, 100)]
POCKET_A = [(10, 10), (20, 10), (20, 20), (10, 20)]
POCKET_B = [(40, 40), (50, 40), (50, 50), (40, 50)]


def ngon(n, cx, cy, r):
    """近似正 n 边形的整数顶点（半径足够大，取整后不会并点或自交）。"""
    return [(cx + round(r * cos(2 * pi * k / n)),
             cy + round(r * sin(2 * pi * k / n))) for k in range(n)]


def test_pocket_constants():
    assert MAX_POCKET_COUNT == 10
    assert MAX_TOTAL_VERTEX_COUNT == 500


def test_prepare_pockets_accepts_and_preserves_input_order():
    region = prepare_polygon(REGION_100)
    pockets = prepare_pockets(region, [POCKET_B, POCKET_A])
    assert [p.vertices for p in pockets] == [tuple(POCKET_B), tuple(POCKET_A)]


def test_pocket_closing_point_is_equivalent():
    region = prepare_polygon(REGION_100)
    a = prepare_pockets(region, [POCKET_A])
    b = prepare_pockets(region, [POCKET_A + [POCKET_A[0]]])
    assert a[0].vertices == b[0].vertices == tuple(POCKET_A)


def test_containing_pocket_attributes_in_input_order():
    region = prepare_polygon(REGION_100)
    pockets = prepare_pockets(region, [POCKET_A, POCKET_B])
    assert containing_pocket(pockets, 15, 15) == 0
    assert containing_pocket(pockets, 45, 45) == 1
    assert containing_pocket(pockets, 30, 30) is None    # 区域内部、任何口袋之外
    assert containing_pocket(pockets, 10, 15) is None    # 口袋边界不算包含
    assert containing_pocket(pockets, 20, 20) is None    # 口袋顶点不算包含
    assert containing_pocket(pockets, 200, 200) is None  # 区域之外
    assert containing_pocket([], 15, 15) is None


def test_too_many_pockets_rejected_with_count():
    region = prepare_polygon(REGION_100)
    many = [[(10 + 8 * k, 10), (16 + 8 * k, 10), (16 + 8 * k, 16), (10 + 8 * k, 16)]
            for k in range(11)]
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, many)
    assert ei.value.code == "TOO_MANY_POCKETS"
    assert ei.value.details["pocket_count"] == 11


def test_too_many_pockets_count_checked_before_pocket_topology():
    # 11 个顶点不足的口袋：整单数量限制必须先于逐口袋顶点校验裁决。
    region = prepare_polygon(REGION_100)
    bad = [[(10 + k, 10), (20 + k, 10)] for k in range(11)]
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, bad)
    assert ei.value.code == "TOO_MANY_POCKETS"
    assert ei.value.details["pocket_count"] == 11


def test_pocket_too_many_vertices_with_closing_point_carries_index():
    # 超限口袋（201 个不同顶点 + 末尾闭合）：沿用区域错误码并保留口袋序号。
    region = prepare_polygon(REGION_100)
    big = [(i, i * i % 7 - 3) for i in range(201)]
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, [POCKET_A, big + [big[0]]])
    assert ei.value.code == "TOO_MANY_VERTICES"
    assert ei.value.details["pocket_index"] == 1
    assert ei.value.details["vertex_count"] == 201


def test_total_vertex_count_limit_rejected_with_count():
    region = prepare_polygon(ngon(200, 100_000, 100_000, 50_000))
    pockets = [ngon(50, 100_000 + round(20_000 * cos(2 * pi * k / 8)),
                    100_000 + round(20_000 * sin(2 * pi * k / 8)), 1000)
               for k in range(8)]
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, pockets)
    assert ei.value.code == "TOO_MANY_TOTAL_VERTICES"
    assert ei.value.details["total_vertex_count"] == 200 + 8 * 50


def test_total_vertex_count_exactly_at_limit_accepted():
    region = prepare_polygon(ngon(200, 100_000, 100_000, 50_000))
    pockets = [ngon(50, 100_000 + round(20_000 * cos(2 * pi * k / 6)),
                    100_000 + round(20_000 * sin(2 * pi * k / 6)), 1000)
               for k in range(6)]
    # 200 + 6 * 50 = 500，恰好达到上限，必须正常受理。
    prepared = prepare_pockets(region, pockets)
    assert len(prepared) == 6
    assert len(region.vertices) + sum(len(p.vertices) for p in prepared) == 500


def test_pocket_topology_error_carries_pocket_index():
    region = prepare_polygon(REGION_100)
    bowtie = [(10, 10), (20, 20), (20, 10), (10, 18)]  # 非对称蝴蝶结（区域内坐标）
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, [POCKET_A, bowtie])
    assert ei.value.code == "SELF_INTERSECTING_POLYGON"
    assert ei.value.details["pocket_index"] == 1
    assert ei.value.details["edge_indices"] == [0, 2]


@pytest.mark.parametrize(
    "pocket,vertex_index",
    [
        ([(90, 90), (110, 90), (110, 110), (90, 110)], 1),  # 顶点在区域外
        ([(0, 10), (10, 10), (10, 20), (0, 20)], 0),        # 顶点压在区域边界上
        ([(0, 0), (10, 0), (10, 10)], 0),                   # 顶点与区域顶点重合
        ([(95, 10), (105, 10), (105, 20), (95, 20)], 1),    # 整体在区域外
    ],
)
def test_pocket_not_strictly_inside_region_vertices(pocket, vertex_index):
    region = prepare_polygon(REGION_100)
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, [pocket])
    assert ei.value.code == "POCKET_NOT_INSIDE_REGION"
    assert ei.value.details["pocket_index"] == 0
    assert ei.value.details["vertex_index"] == vertex_index


def test_pocket_edge_crossing_concave_region_rejected():
    # 顶点全部严格在凹区域内部，但边穿越区域边界（横跨凹口）。
    concave = prepare_polygon([(0, 0), (100, 0), (100, 20), (20, 20),
                               (20, 80), (100, 80), (100, 100), (0, 100)])
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(concave, [[(40, 10), (60, 10), (50, 90)]])
    assert ei.value.code == "POCKET_NOT_INSIDE_REGION"
    assert ei.value.details["pocket_index"] == 0
    assert ei.value.details["edge_indices"] == [1, 2]


@pytest.mark.parametrize(
    "second",
    [
        [(15, 15), (25, 15), (25, 25), (15, 25)],   # 边交叉重叠
        [(20, 10), (30, 10), (30, 20), (20, 20)],   # 共边接触
        [(20, 20), (30, 20), (30, 30), (20, 30)],   # 顶点相触（A 的顶点 (20,20)）
        [(12, 12), (18, 12), (18, 18), (12, 18)],   # 嵌套在 A 内
        [(5, 5), (25, 5), (25, 25), (5, 25)],       # 整体包含 A
    ],
)
def test_pockets_intersect_variants_rejected(second):
    region = prepare_polygon(REGION_100)
    with pytest.raises(PolygonError) as ei:
        prepare_pockets(region, [POCKET_A, second])
    assert ei.value.code == "POCKETS_INTERSECT"
    assert ei.value.details["pocket_indices"] == [0, 1]


# ---------------------------------------------------------------------------
# 面积汇总：绝对二倍面积
# ---------------------------------------------------------------------------

def test_doubled_area_orientation_and_closing_point_invariant():
    a = prepare_polygon(SQUARE_CCW)
    b = prepare_polygon(list(reversed(SQUARE_CCW)))
    c = prepare_polygon(SQUARE_CCW + [SQUARE_CCW[0]])
    assert doubled_area(a) == doubled_area(b) == doubled_area(c) == 200
    # 有向面积符号随方向翻转，绝对二倍面积不变。
    assert a.signed_area2 == -b.signed_area2


def test_doubled_area_large_coordinates_exact():
    B = 100_000_000
    big = prepare_polygon([(-B, -B), (B, -B), (B, B), (-B, B)])
    assert doubled_area(big) == 2 * (2 * B) * (2 * B)  # 8e16，整数精确


def test_doubled_area_net_conservation_with_pockets():
    region = prepare_polygon(REGION_100)
    pockets = prepare_pockets(region, [POCKET_A, POCKET_B])
    net = doubled_area(region) - sum(doubled_area(p) for p in pockets)
    assert doubled_area(region) == 20000
    assert [doubled_area(p) for p in pockets] == [200, 200]
    assert net == 19600
