"""纯整数平面几何裁决算法。

坐标单位为整数厘米；Python 整数为任意精度，在文档允许的坐标范围内
（绝对值 <= 100_000_000）不存在溢出或舍入问题。本模块不调用任何空间
数据库或第三方几何库，所有判定（方向、跨立、点在线段上、线段相交、
水平射线奇偶、点到线段最短距离）均由整数叉积、点积与平方距离分数
比较直接给出，全程无浮点。

分类约定：
* 点严格在多边形内部 -> ``INSIDE``
* 点严格在多边形外部 -> ``OUTSIDE``
* 点落在任意一条边（含顶点）上 -> ``BOUNDARY``

边界点一律 FORBIDDEN；点同时命中多条边（即顶点）时，取序号最小的边。
边序号从 0 开始，闭合边为最后一个不同顶点到第 0 个顶点。

安全距离（exclusion_margin_cm）支持由 ``nearest_edge`` 与
``within_exclusion_margin`` 提供：外部点距最近边的距离不超过安全
距离时，HTTP 层将其改判为 NEAR_BOUNDARY；证据中的平方距离以约分
分数（分子/分母）给出，最近距离相同取最小边序号。

许可口袋（permitted_pockets）由 ``prepare_pockets`` 规整与校验：
数量上限、单口袋拓扑（沿用区域全部顶点规则）、外环与全部口袋的
总顶点数、每个口袋严格位于禁抛区内部、口袋两两不接触不重叠；
``containing_pocket`` 按输入顺序返回严格包含待判点的第一个口袋
序号，供 HTTP 层把口袋内部点改判为 ALLOWED。

面积汇总由 ``doubled_area`` 提供：返回多边形绝对二倍面积（整数，
与顶点方向及闭合写法无关），供 /region-area-summary 核对申报面积。
"""

from dataclasses import dataclass
from math import gcd

COORD_LIMIT = 100_000_000
MIN_VERTEX_COUNT = 3
MAX_VERTEX_COUNT = 200
MAX_POINT_COUNT = 500
MAX_POCKET_COUNT = 10
MAX_TOTAL_VERTEX_COUNT = 500


class PolygonError(ValueError):
    """区域不构成合法简单多边形；HTTP 层统一映射为 422。"""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


# ---------------------------------------------------------------------------
# 整数本原运算
# ---------------------------------------------------------------------------

def cross(ax: int, ay: int, bx: int, by: int, cx: int, cy: int) -> int:
    """(B - A) x (C - A)。

    >0：C 在有向直线 A->B 左侧（逆时针转）；
    <0：C 在右侧；=0：三点共线。
    """

    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def point_on_segment(
    px: int, py: int, ax: int, ay: int, bx: int, by: int
) -> bool:
    """整数判定 P 是否在线段 AB 上（含端点）。"""

    if cross(ax, ay, bx, by, px, py) != 0:
        return False
    return (
        min(ax, bx) <= px <= max(ax, bx)
        and min(ay, by) <= py <= max(ay, by)
    )


def segments_intersect(
    a: tuple[int, int],
    b: tuple[int, int],
    c: tuple[int, int],
    d: tuple[int, int],
) -> bool:
    """整数判定线段 AB 与 CD 是否存在任何公共点。

    覆盖正常相交、端点相接（T 形接触）与共线重叠，全部视为相交。
    调用方负责跳过本来就共享端点的相邻边。
    """

    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d

    o1 = cross(ax, ay, bx, by, cx, cy)
    o2 = cross(ax, ay, bx, by, dx, dy)
    o3 = cross(cx, cy, dx, dy, ax, ay)
    o4 = cross(cx, cy, dx, dy, bx, by)

    # 任一端点落在对方线段上（含共线重叠）。
    if o1 == 0 and min(ax, bx) <= cx <= max(ax, bx) and min(ay, by) <= cy <= max(ay, by):
        return True
    if o2 == 0 and min(ax, bx) <= dx <= max(ax, bx) and min(ay, by) <= dy <= max(ay, by):
        return True
    if o3 == 0 and min(cx, dx) <= ax <= max(cx, dx) and min(cy, dy) <= ay <= max(cy, dy):
        return True
    if o4 == 0 and min(cx, dx) <= bx <= max(cx, dx) and min(cy, dy) <= by <= max(cy, dy):
        return True

    # 严格跨立：两侧叉积异号。
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))


def signed_area2(vertices: list[tuple[int, int]]) -> int:
    """鞋带公式返回有向面积的 2 倍；>0 为逆时针（y 轴向上约定）。"""

    total = 0
    m = len(vertices)
    for i in range(m):
        ax, ay = vertices[i]
        bx, by = vertices[(i + 1) % m]
        total += ax * by - ay * bx
    return total


def doubled_area(poly: "Polygon") -> int:
    """多边形面积的 2 倍（整数，单位为平方厘米的二倍）。

    取有向二倍面积的绝对值，因此与顶点顺/逆时针方向无关；末尾重复
    闭合点在规整阶段已被丢弃，同样不影响结果。
    """

    return abs(poly.signed_area2)


# ---------------------------------------------------------------------------
# 区域规整与合法性校验
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Polygon:
    """通过全部合法性校验的简单多边形。"""

    vertices: tuple[tuple[int, int], ...]
    signed_area2: int

    @property
    def edge_count(self) -> int:
        return len(self.vertices)

    @property
    def orientation(self) -> str:
        return "CCW" if self.signed_area2 > 0 else "CW"

    def edge(self, i: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """第 i 条边：vertices[i] -> vertices[(i+1) % m]，闭合边包含在内。"""

        return self.vertices[i], self.vertices[(i + 1) % self.edge_count]


def prepare_polygon(raw_vertices: list[list[int]]) -> Polygon:
    """把输入顶点规整为简单多边形，或抛 ``PolygonError``（请求级 422）。

    规整步骤：
    1. 若末尾又写了一次首点（闭合点），丢弃它——两种写法完全等价；
    2. 拒绝连续重复顶点（含首尾相接处）；
    3. 拒绝不同顶点少于 3 个；
    4. 拒绝零面积（含全部共线）；
    5. 拒绝非相邻边存在任何公共点（自交/自触/重叠）。
    """

    pts = [(int(x), int(y)) for x, y in raw_vertices]

    # (1) 可选的末尾首点闭合写法。
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts.pop()

    m = len(pts)

    # 规整后仍多于 200 个顶点（末尾首点闭合写法除外，它已在上一步被丢弃）。
    if m > MAX_VERTEX_COUNT:
        raise PolygonError(
            "TOO_MANY_VERTICES",
            f"区域最多包含 {MAX_VERTEX_COUNT} 个不同顶点，收到 {m} 个。",
            vertex_count=m,
        )

    # (2) 连续重复顶点（含闭合位置）。先于去重：重复顶点必须明确报错而不是静默吞掉。
    for i in range(m):
        j = (i + 1) % m
        if pts[i] == pts[j]:
            raise PolygonError(
                "CONSECUTIVE_DUPLICATE_VERTEX",
                f"顶点 {i} 与后继顶点完全相同；连续重复顶点不允许出现。",
                vertex_index=i,
            )

    # (3) 不同顶点少于 3 个。
    if len(set(pts)) < MIN_VERTEX_COUNT:
        raise PolygonError(
            "NOT_ENOUGH_DISTINCT_VERTICES",
            "区域至少需要 3 个互不相同的顶点。",
            distinct_vertex_count=len(set(pts)),
        )

    # (4) 零面积。
    area2 = signed_area2(pts)
    if area2 == 0:
        raise PolygonError(
            "ZERO_AREA_POLYGON",
            "区域有向面积为 0（共线或退化），不能构成多边形。",
        )

    # (5) 边界不得自交，区域必须是简单多边形。O(m^2)，m <= 200，足够。
    #     - 非相邻边：存在任何公共点（正常交叉、端点接触、共线重叠）即拒绝；
    #     - 相邻边：共享端点本属正常，但共线回退/重叠（spike）仍要拒绝。
    for i in range(m):
        a, b = pts[i], pts[(i + 1) % m]
        for j in range(i + 1, m):
            c, d = pts[j], pts[(j + 1) % m]
            adjacent = j == i + 1 or (i == 0 and j == m - 1)
            if adjacent:
                # 两条相邻边共享顶点 s，各自的另一端点为 p、q；
                # (p-s) 与 (q-s) 共线且同向（点积 > 0）意味着一条边
                # 回退压到另一条上（spike），必须拒绝。
                shared = {i, (i + 1) % m} & {j, (j + 1) % m}
                s_idx = shared.pop()
                p_idx = ({i, (i + 1) % m} - {s_idx}).pop()
                q_idx = ({j, (j + 1) % m} - {s_idx}).pop()
                sx, sy = pts[s_idx]
                vpx, vpy = pts[p_idx][0] - sx, pts[p_idx][1] - sy
                vqx, vqy = pts[q_idx][0] - sx, pts[q_idx][1] - sy
                if (
                    vpx * vqy - vpy * vqx == 0
                    and vpx * vqx + vpy * vqy > 0
                ):
                    raise PolygonError(
                        "SELF_INTERSECTING_POLYGON",
                        f"相邻边 {i} 与 {j} 共线重叠，边界发生回退，区域必须是简单多边形。",
                        edge_indices=[i, j],
                    )
                continue
            if segments_intersect(a, b, c, d):
                raise PolygonError(
                    "SELF_INTERSECTING_POLYGON",
                    f"非相邻边 {i} 与 {j} 存在公共点，区域必须是简单多边形。",
                    edge_indices=[i, j],
                )

    return Polygon(vertices=tuple(pts), signed_area2=area2)


# ---------------------------------------------------------------------------
# 单点裁决
# ---------------------------------------------------------------------------

def boundary_edge(poly: Polygon, px: int, py: int) -> int | None:
    """返回点命中的序号最小的边；不在任何边上则返回 None。

    按边序号升序扫描，首个命中即最小序号。简单多边形的顶点只可能同时
    命中与其相邻的两条边，因此最小序号规则在此一次扫描内即满足。
    """

    for i in range(poly.edge_count):
        (ax, ay), (bx, by) = poly.edge(i)
        if point_on_segment(px, py, ax, ay, bx, by):
            return i
    return None


def horizontal_ray_edges(
    poly: Polygon, px: int, py: int
) -> list[int]:
    """从 P 向 +x 方向发射水平射线，返回被穿过的边序号（升序）。

    采用半开区间跨立规则：端点仅当严格位于射线上方时计入，
    ``(ay > py) != (by > py)``，这样顶点不会被重复计数，水平边
    天然不计入。交点在 P 右侧的比较完全用整数完成：

        交点 x* = ax + (bx-ax)*(py-ay)/(by-ay)
        x* > px  <=>  cross(A,B,P) 与 (by-ay) 同号

    调用本函数前已排除边界点；在跨立前提下 cross 不可能为 0
    （否则 P 位于该边线段上，属于边界）。
    """

    crossed: list[int] = []
    for i in range(poly.edge_count):
        (ax, ay), (bx, by) = poly.edge(i)
        if (ay > py) != (by > py):
            cr = cross(ax, ay, bx, by, px, py)
            if (cr > 0) == (by > ay):  # cross 与 dy 同号 <=> 交点严格在右侧
                crossed.append(i)
    return crossed


@dataclass(frozen=True)
class Classification:
    kind: str  # "INSIDE" | "OUTSIDE" | "BOUNDARY"
    boundary_edge: int | None
    crossing_edges: tuple[int, ...]


def classify(poly: Polygon, px: int, py: int) -> Classification:
    """边界优先；非边界点用一次水平射线的奇偶性给出内/外结论与证据。"""

    hit = boundary_edge(poly, px, py)
    if hit is not None:
        return Classification(kind="BOUNDARY", boundary_edge=hit, crossing_edges=())

    crossed = tuple(horizontal_ray_edges(poly, px, py))
    kind = "INSIDE" if len(crossed) % 2 == 1 else "OUTSIDE"
    return Classification(kind=kind, boundary_edge=None, crossing_edges=crossed)


# ---------------------------------------------------------------------------
# 安全距离：点到最近边的整数平方距离
# ---------------------------------------------------------------------------

def segment_distance2(
    px: int, py: int, ax: int, ay: int, bx: int, by: int
) -> tuple[int, int]:
    """点 P 到线段 AB 的最短平方距离，返回未约分的 ``(分子, 分母)``。

    垂足参数 t = ((P-A)·(B-A)) / |B-A|²（点积与平方长度均为整数）：
    * t <= 0：最近点为端点 A，返回 (|P-A|², 1)；
    * t >= 1：最近点为端点 B，返回 (|P-B|², 1)；
    * 0 < t < 1：最近点为垂足，平方距离 = cross(A,B,P)² / |B-A|²。

    分子、分母均为非负整数；两个这样的分数比较大小用交叉相乘完成，
    不引入浮点。
    """

    dx = bx - ax
    dy = by - ay
    len2 = dx * dx + dy * dy
    t_num = (px - ax) * dx + (py - ay) * dy
    if t_num <= 0:
        return (px - ax) * (px - ax) + (py - ay) * (py - ay), 1
    if t_num >= len2:
        return (px - bx) * (px - bx) + (py - by) * (py - by), 1
    cr = cross(ax, ay, bx, by, px, py)
    return cr * cr, len2


@dataclass(frozen=True)
class NearestEdge:
    """点到多边形边界的最短距离证据；平方距离以约分后的分数给出。"""

    edge_index: int
    dist2_num: int  # 最短平方距离的分子（已约分）
    dist2_den: int  # 最短平方距离的分母（已约分）


def nearest_edge(poly: Polygon, px: int, py: int) -> NearestEdge:
    """返回距 P 最近的边及最短平方距离（约分后的分子/分母）。

    按边序号升序扫描，仅在严格更小时更新最佳值（交叉相乘比较分数），
    因此最近距离相同——例如顶点两侧的两条邻边端点距离相等——时
    自然保留最小边序号。
    """

    best_i = -1
    best_num = best_den = 0
    for i in range(poly.edge_count):
        (ax, ay), (bx, by) = poly.edge(i)
        num, den = segment_distance2(px, py, ax, ay, bx, by)
        if best_i < 0 or num * best_den < best_num * den:
            best_i, best_num, best_den = i, num, den
    g = gcd(best_num, best_den)
    return NearestEdge(
        edge_index=best_i, dist2_num=best_num // g, dist2_den=best_den // g
    )


def within_exclusion_margin(near: NearestEdge, margin_cm: int) -> bool:
    """最短距离不超过安全距离的精确整数判定：num/den <= margin²。"""

    return near.dist2_num <= margin_cm * margin_cm * near.dist2_den


# ---------------------------------------------------------------------------
# 许可口袋：严格位于禁抛区内部、彼此不接触不重叠的简单多边形
# ---------------------------------------------------------------------------

def prepare_pockets(
    region: Polygon, raw_pockets: list[list[list[int]]]
) -> list[Polygon]:
    """规整并校验许可口袋列表，返回与输入同序的口袋多边形。

    校验顺序：数量 -> 逐口袋沿用区域顶点规则规整 -> 外环与全部口袋
    规整后的总顶点数 -> 每个口袋严格位于禁抛区内部 -> 口袋两两不
    接触不重叠。任何一步失败都抛 ``PolygonError``（请求级 422，
    无部分结果），details 携带对应的口袋序号或计数。
    """

    if len(raw_pockets) > MAX_POCKET_COUNT:
        raise PolygonError(
            "TOO_MANY_POCKETS",
            f"许可口袋最多 {MAX_POCKET_COUNT} 个，收到 {len(raw_pockets)} 个。",
            pocket_count=len(raw_pockets),
        )

    pockets: list[Polygon] = []
    for idx, raw in enumerate(raw_pockets):
        try:
            pockets.append(prepare_polygon(raw))
        except PolygonError as exc:
            # 单口袋拓扑非法：沿用区域错误码，补充口袋序号后整单 422。
            raise PolygonError(
                exc.code,
                f"许可口袋 {idx}：{exc.message}",
                pocket_index=idx,
                **exc.details,
            ) from exc

    total = len(region.vertices) + sum(len(p.vertices) for p in pockets)
    if total > MAX_TOTAL_VERTEX_COUNT:
        raise PolygonError(
            "TOO_MANY_TOTAL_VERTICES",
            f"外环与全部口袋规整后的总顶点数最多 {MAX_TOTAL_VERTEX_COUNT} 个，实际 {total} 个。",
            total_vertex_count=total,
        )

    for idx, pocket in enumerate(pockets):
        _require_pocket_strictly_inside(region, pocket, idx)
    for i in range(len(pockets)):
        for j in range(i + 1, len(pockets)):
            _require_pockets_disjoint(pockets[i], pockets[j], i, j)
    return pockets


def _require_pocket_strictly_inside(
    region: Polygon, pocket: Polygon, pocket_index: int
) -> None:
    """口袋必须严格位于禁抛区内部：全部顶点严格在内，且任意口袋边
    与区域边无公共点。两者兼备时整条口袋边界都在区域内部；口袋边界
    是有界闭曲线，其内部随之完全落在区域内部（否则内部必含区域外点，
    与边界不相交矛盾）。"""

    for vi, (x, y) in enumerate(pocket.vertices):
        cls = classify(region, x, y)
        if cls.kind != "INSIDE":
            where = "边界上" if cls.kind == "BOUNDARY" else "外部"
            raise PolygonError(
                "POCKET_NOT_INSIDE_REGION",
                f"许可口袋 {pocket_index} 的顶点 {vi} 位于禁抛区{where}，"
                "口袋必须严格位于禁抛区内部。",
                pocket_index=pocket_index,
                vertex_index=vi,
            )
    for ei in range(pocket.edge_count):
        a, b = pocket.edge(ei)
        for ri in range(region.edge_count):
            c, d = region.edge(ri)
            if segments_intersect(a, b, c, d):
                raise PolygonError(
                    "POCKET_NOT_INSIDE_REGION",
                    f"许可口袋 {pocket_index} 的边 {ei} 与禁抛区边 {ri} 存在公共点，"
                    "口袋必须严格位于禁抛区内部。",
                    pocket_index=pocket_index,
                    edge_indices=[ei, ri],
                )


def _require_pockets_disjoint(
    first: Polygon, second: Polygon, i: int, j: int
) -> None:
    """两口袋不得接触或重叠：任意边无公共点，且互不嵌套。"""

    for ei in range(first.edge_count):
        a, b = first.edge(ei)
        for ej in range(second.edge_count):
            c, d = second.edge(ej)
            if segments_intersect(a, b, c, d):
                raise PolygonError(
                    "POCKETS_INTERSECT",
                    f"许可口袋 {i} 的边 {ei} 与许可口袋 {j} 的边 {ej} 存在公共点，"
                    "口袋之间不得接触或重叠。",
                    pocket_indices=[i, j],
                    edge_indices=[ei, ej],
                )
    # 边无公共点时，两多边形要么完全分离，要么一个整体嵌套在另一个内部；
    # 嵌套当且仅当一方的任一顶点落在另一方内部（顶点不可能恰落在对方
    # 边界上——那意味着边存在公共点，已在上面拒绝）。
    x, y = first.vertices[0]
    if classify(second, x, y).kind == "INSIDE":
        raise PolygonError(
            "POCKETS_INTERSECT",
            f"许可口袋 {i} 整体嵌套在许可口袋 {j} 内部，口袋之间不得接触或重叠。",
            pocket_indices=[i, j],
        )
    x, y = second.vertices[0]
    if classify(first, x, y).kind == "INSIDE":
        raise PolygonError(
            "POCKETS_INTERSECT",
            f"许可口袋 {j} 整体嵌套在许可口袋 {i} 内部，口袋之间不得接触或重叠。",
            pocket_indices=[i, j],
        )


def containing_pocket(pockets: list[Polygon], px: int, py: int) -> int | None:
    """返回严格包含 P 的第一个口袋序号（按输入顺序归因）；无则 None。

    口袋两两不接触不重叠，P 至多严格位于一个口袋内部；落在口袋边界
    上不算包含（边界点仍按禁抛区内部处理，维持 FORBIDDEN）。
    """

    for idx, pocket in enumerate(pockets):
        if classify(pocket, px, py).kind == "INSIDE":
            return idx
    return None
