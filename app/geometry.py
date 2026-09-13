"""纯整数平面几何裁决算法。

坐标单位为整数厘米；Python 整数为任意精度，在文档允许的坐标范围内
（绝对值 <= 100_000_000）不存在溢出或舍入问题。本模块不调用任何空间
数据库或第三方几何库，所有判定（方向、跨立、点在线段上、线段相交、
水平射线奇偶）均由整数叉积与区间比较直接给出。

分类约定：
* 点严格在多边形内部 -> ``INSIDE``
* 点严格在多边形外部 -> ``OUTSIDE``
* 点落在任意一条边（含顶点）上 -> ``BOUNDARY``

边界点一律 FORBIDDEN；点同时命中多条边（即顶点）时，取序号最小的边。
边序号从 0 开始，闭合边为最后一个不同顶点到第 0 个顶点。
"""

from dataclasses import dataclass

COORD_LIMIT = 100_000_000
MIN_VERTEX_COUNT = 3
MAX_VERTEX_COUNT = 200
MAX_POINT_COUNT = 500


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
