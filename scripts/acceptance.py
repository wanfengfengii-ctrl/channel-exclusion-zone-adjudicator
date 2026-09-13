#!/usr/bin/env python3
"""一次性验收脚本：对运行中的 API 执行验收清单。

只使用标准库；期望不是照抄服务实现，而是来自：
* 方向反转 / 闭合写法之间的响应互相对比（同一点必须同结论）；
* 独立的整数叉积与最小相邻边序号推导；
* 对非法区域只要求“显式错误信封 + 无部分结果”；
* 面积汇总接口的矩形外环 + 双口袋面积守恒核对（鞋带公式手算值）。

任一检查不过即以非零码退出。
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
URL = f"{API}/adjudicate"
SUMMARY_URL = f"{API}/region-area-summary"

failures: list[str] = []
checks = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(f"{name}: {detail}")


def call(vertices, points, *, margin=None, pockets=None, expect_status=200):
    payload = {
        "region": {"vertices": [{"x": x, "y": y} for x, y in vertices]},
        "points": [{"x": x, "y": y} for x, y in points],
    }
    if margin is not None:
        payload["exclusion_margin_cm"] = margin
    if pockets is not None:
        payload["permitted_pockets"] = [
            {"vertices": [{"x": x, "y": y} for x, y in pocket]} for pocket in pockets
        ]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
        if expect_status != exc.code:
            raise AssertionError(f"expected {expect_status}, got {exc.code}: {payload}")
        return exc.code, payload


def call_summary(vertices, *, pockets=None, extra=None, expect_status=200):
    """调用 POST /region-area-summary；pockets=None 表示不提交该字段。"""

    payload = {"region": {"vertices": [{"x": x, "y": y} for x, y in vertices]}}
    if pockets is not None:
        payload["permitted_pockets"] = [
            {"vertices": [{"x": x, "y": y} for x, y in pocket]} for pocket in pockets
        ]
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(SUMMARY_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
        if expect_status != exc.code:
            raise AssertionError(f"expected {expect_status}, got {exc.code}: {payload}")
        return exc.code, payload


def wait_ready(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise SystemExit("API did not become ready in time")


def decisions(vertices, points, *, margin=None, pockets=None):
    _, data = call(vertices, points, margin=margin, pockets=pockets)
    return data["results"], data.get("polygon")


def grid(lo, hi, step):
    return [(x, y) for x in range(lo, hi + 1, step) for y in range(lo, hi + 1, step)]


def ngon(n, cx, cy, r):
    """近似正 n 边形的整数顶点（半径足够大，取整后不会并点或自交）。"""
    return [(cx + round(r * math.cos(2 * math.pi * k / n)),
             cy + round(r * math.sin(2 * math.pi * k / n))) for k in range(n)]


def main() -> int:
    print(f"== Acceptance against {URL} ==")
    wait_ready()

    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    square_cw = list(reversed(square))
    square_closed = square + [square[0]]
    concave = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]

    # --- 1. 顺逆时针 + 闭合写法：同一点必须获得相同结论 --------------------
    print("[1] 方向反转与闭合写法等价")
    probes = grid(-2, 12, 1) + square
    variants = [square, square_cw, square_closed]
    ref, poly_ccw = decisions(square, probes)
    rev, poly_cw = decisions(square_cw, probes)
    closed, _ = decisions(square_closed, probes)
    same_rev = all(
        (a["decision"], a["classification"]) == (b["decision"], b["classification"])
        for a, b in zip(ref, rev)
    )
    same_closed = all(
        (a["decision"], a["classification"]) == (c["decision"], c["classification"])
        for a, c in zip(ref, closed)
    )
    check("顺/逆时针逐点结论一致", same_rev)
    check("省略/重写首点逐点结论一致", same_closed)
    check("规整后顶点数为 4（闭合点被丢弃）", poly_ccw["vertex_count"] == 4, str(poly_ccw))
    check("朝向字段 CCW/CW 相反", poly_ccw["orientation"] == "CCW" and poly_cw["orientation"] == "CW",
          f"{poly_ccw['orientation']} / {poly_cw['orientation']}")

    rc, _ = decisions(concave, probes)
    rv, _ = decisions(list(reversed(concave)), probes)
    check("凹多边形顺/逆时针逐点结论一致",
          all((a["decision"], a["classification"]) == (b["decision"], b["classification"]) for a, b in zip(rc, rv)))

    # --- 2. 边界点稳定拒绝 + 最小边序号 -----------------------------------
    print("[2] 边界归属：边与顶点一律 FORBIDDEN，命中多边取最小序号")
    m = len(square)
    boundary_points = []
    expected_edge = {}
    for k, (x, y) in enumerate(square):  # 顶点 k 同时落在边 k-1 与边 k
        boundary_points.append((x, y))
        expected_edge[(x, y)] = 0 if k == 0 else k - 1
    for i in range(m):  # 每条边的整数中点
        ax, ay = square[i]
        bx, by = square[(i + 1) % m]
        mid = ((ax + bx) // 2, (ay + by) // 2)
        boundary_points.append(mid)
        expected_edge[mid] = i
    first, _ = decisions(square, boundary_points)
    second, _ = decisions(square, boundary_points)  # 重复请求验证稳定性
    ok = True
    for item, p in zip(first, boundary_points):
        if item["decision"] != "FORBIDDEN" or item["classification"] != "BOUNDARY":
            ok = False
        ev = item["evidence"]
        if ev.get("type") != "boundary" or ev.get("edge_index") != expected_edge[p]:
            ok = False
    check("所有顶点/边内点均为 BOUNDARY+FORBIDDEN 且边序号最小", ok,
          json.dumps([(p, r["evidence"]) for p, r in zip(boundary_points, first)], ensure_ascii=False))
    check("重复请求边界证据完全一致",
          [r["evidence"] for r in first] == [r["evidence"] for r in second])

    # --- 3. 内外分类与射线奇偶证据 ----------------------------------------
    print("[3] 内部/外部与水平射线奇偶证据")
    pts = [(5, 5, "INSIDE"), (-5, 5, "OUTSIDE"), (20, 20, "OUTSIDE"), (1, 1, "INSIDE")]
    results, _ = decisions(square, [(x, y) for x, y, _ in pts])
    ok = True
    for item, (_, _, want) in zip(results, pts):
        if item["classification"] != want:
            ok = False
        ev = item["evidence"]
        if ev["type"] != "horizontal_ray":
            ok = False
        parity = "odd" if ev["crossing_count"] % 2 else "even"
        if ev["parity"] != parity or ev["crossing_edges"] != sorted(ev["crossing_edges"]):
            ok = False
        if (want == "INSIDE") != (item["decision"] == "FORBIDDEN"):
            ok = False
    check("内部 FORBIDDEN/外部 ALLOWED，穿越数奇偶与证据一致", ok,
          json.dumps([r["evidence"] for r in results], ensure_ascii=False))

    notch, _ = decisions(concave, [(8, 8), (8, 2), (2, 8), (3, 3)])
    check("凹口外点 ALLOWED，两臂与中心 FORBIDDEN",
          [r["decision"] for r in notch] == ["ALLOWED", "FORBIDDEN", "FORBIDDEN", "FORBIDDEN"],
          str([(r["classification"], r["decision"]) for r in notch]))

    # --- 4. 非法区域：只有显式错误，绝不给部分结果 --------------------------
    print("[4] 非法区域 -> 422 显式错误信封且无部分结果")
    invalid = [
        ("连续重复顶点", [(0, 0), (0, 0), (1, 0), (1, 1)], "CONSECUTIVE_DUPLICATE_VERTEX"),
        ("不同顶点不足3", [(0, 0), (5, 5), (0, 0)], "NOT_ENOUGH_DISTINCT_VERTICES"),
        ("共线零面积", [(0, 0), (5, 0), (10, 0)], "ZERO_AREA_POLYGON"),
        ("非对称蝴蝶结自交", [(0, 0), (10, 10), (10, 0), (0, 8)], "SELF_INTERSECTING_POLYGON"),
        ("8字顶点相触", [(5, 5), (10, 0), (10, 10), (5, 5), (0, 10), (0, 0)], "SELF_INTERSECTING_POLYGON"),
    ]
    for name, verts, want_code in invalid:
        status, body = call(verts, [(0, 0), (1, 1)], expect_status=422)
        ok = (
            status == 422
            and set(body.keys()) == {"error"}
            and body["error"].get("code") == want_code
            and bool(body["error"].get("message"))
            and "results" not in body
        )
        check(f"非法区域（{name}）只出现明确错误 {want_code}", ok, json.dumps(body, ensure_ascii=False))

    # 结构性约束错误也走同一信封。
    status, body = call([(0, 0), (1, 1)], [(0, 0)], expect_status=422)
    check("顶点数不足 3 的请求结构错误使用同一信封",
          status == 422 and body["error"]["code"] == "VALIDATION_ERROR", str(body))
    status, body = call(square, [(10**9, 0)], expect_status=422)
    check("坐标越界 422", status == 422 and body["error"]["code"] == "VALIDATION_ERROR", str(body)[:200])

    # 布尔值、数字字符串、整数形式的浮点都不得被当作整数受理：直接发原始 JSON。
    for label, bad_json in (("布尔", "true"), ("数字字符串", '"5"'), ("整数浮点", "5.0")):
        raw = (
            '{"region":{"vertices":[{"x":0,"y":0},{"x":10,"y":0},{"x":10,"y":10},{"x":0,"y":10}]},'
            f'"points":[{{"x":{bad_json},"y":0}}]}}'
        ).encode()
        req = urllib.request.Request(URL, data=raw, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read())
        check(f"非整数坐标（{label}）整单 422 且无结果",
              status == 422 and body["error"]["code"] == "VALIDATION_ERROR" and "results" not in body,
              f"{status} {str(body)[:200]}")

    # --- 5. 顺序保持与 500 点上限 ------------------------------------------
    print("[5] 顺序保持与 500 点上限")
    many = [((i * 7919) % 13 - 2, (i * 6151) % 11 - 2) for i in range(500)]
    results, _ = decisions(square, many)
    check("返回 500 条且顺序/坐标与输入一致",
          len(results) == 500
          and [r["index"] for r in results] == list(range(500))
          and [(r["point"]["x"], r["point"]["y"]) for r in results] == many)
    status, body = call(square, [(0, 0)] * 501, expect_status=422)
    check("501 个点整请求 422", status == 422 and body["error"]["code"] == "VALIDATION_ERROR")

    # --- 6. 大坐标整数精确性 -----------------------------------------------
    print("[6] 大坐标（1e8 厘米，叉积量级 1e16）")
    B = 100_000_000
    big = [(0, 0), (B, 0), (B, B), (0, B)]
    results, _ = decisions(big, [(1, 1), (B, 0), (0, 0), (0, -1), (B // 2, B // 2)])
    got = [(r["classification"], r["decision"]) for r in results]
    check("大坐标内/边/顶点/外严格区分",
          got == [("INSIDE", "FORBIDDEN"), ("BOUNDARY", "FORBIDDEN"),
                  ("BOUNDARY", "FORBIDDEN"), ("OUTSIDE", "ALLOWED"),
                  ("INSIDE", "FORBIDDEN")], str(got))

    # 大坐标斜边 x+y=B：最近的整数点在线两侧与线上，任何浮点容差都会误判。
    tri = [(0, 0), (B, 0), (0, B)]
    results, _ = decisions(tri, [(B // 2 - 1, B // 2), (B // 2, B // 2), (B // 2 + 1, B // 2)])
    got = [(r["classification"], r["decision"]) for r in results]
    check("斜边相邻整数点内/线/外严格区分",
          got == [("INSIDE", "FORBIDDEN"), ("BOUNDARY", "FORBIDDEN"), ("OUTSIDE", "ALLOWED")],
          str(got))
    check("斜边命中边序号为 1", results[1]["evidence"]["edge_index"] == 1,
          str(results[1]["evidence"]))

    # --- 7. exclusion_margin_cm 安全距离 ------------------------------------
    print("[7] exclusion_margin_cm 安全距离")

    # 边中段外侧点：距离恰为阈值 -> 改判禁抛；刚越过阈值 -> 放行。
    res, _ = decisions(square, [(5, -3), (5, -4)], margin=3)
    ev_near = res[0]["evidence"]
    check("边中段外侧点距离等于阈值时改判 NEAR_BOUNDARY+FORBIDDEN",
          res[0]["classification"] == "NEAR_BOUNDARY" and res[0]["decision"] == "FORBIDDEN"
          and ev_near.get("type") == "near_boundary"
          and ev_near.get("edge_index") == 0
          and (ev_near.get("distance2_num"), ev_near.get("distance2_den")) == (9, 1)
          and ev_near.get("exclusion_margin_cm") == 3,
          json.dumps(res[0], ensure_ascii=False))
    check("刚越过阈值的点保持 ALLOWED 且证据仍为射线形式",
          res[1]["classification"] == "OUTSIDE" and res[1]["decision"] == "ALLOWED"
          and res[1]["evidence"].get("type") == "horizontal_ray",
          json.dumps(res[1], ensure_ascii=False))

    # 顶点附近：按线段端点距离命中；两侧邻边等距时取最小边序号。
    res, _ = decisions(square, [(-2, -1)], margin=3)
    ev = res[0]["evidence"]
    check("顶点附近按端点距离命中且同距取最小边序号",
          res[0]["classification"] == "NEAR_BOUNDARY"
          and ev.get("edge_index") == 0
          and (ev.get("distance2_num"), ev.get("distance2_den")) == (5, 1),
          json.dumps(res[0], ensure_ascii=False))
    res, _ = decisions(square, [(-2, -1)], margin=2)
    check("端点距离 sqrt(5) 刚越过阈值 2 时放行",
          res[0]["classification"] == "OUTSIDE" and res[0]["decision"] == "ALLOWED",
          json.dumps(res[0], ensure_ascii=False))

    # 斜边外侧点：证据给出约分后的平方距离分数（900/200 -> 9/2）。
    res, _ = decisions([(0, 0), (10, 0), (0, 10)], [(8, 5)], margin=3)
    ev = res[0]["evidence"]
    check("斜边外侧点证据为约分后的平方距离分数",
          res[0]["classification"] == "NEAR_BOUNDARY"
          and ev.get("edge_index") == 1
          and (ev.get("distance2_num"), ev.get("distance2_den")) == (9, 2),
          json.dumps(res[0], ensure_ascii=False))

    # 顺/逆时针：归属同一条几何边（端点集合一致）、平方距离一致。
    res_ccw, _ = decisions(square, [(5, -3), (-2, -1)], margin=5)
    res_cw, _ = decisions(square_cw, [(5, -3), (-2, -1)], margin=5)
    ok = True
    for a, b in zip(res_ccw, res_cw):
        if a["classification"] != "NEAR_BOUNDARY" or b["classification"] != "NEAR_BOUNDARY":
            ok = False
        ea, eb = a["evidence"], b["evidence"]
        if {tuple(p) for p in ea["edge"]} != {tuple(p) for p in eb["edge"]}:
            ok = False
        if (ea["distance2_num"], ea["distance2_den"]) != (eb["distance2_num"], eb["distance2_den"]):
            ok = False
    check("顺/逆时针安全距离边序归因稳定（同一几何边、同一平方距离）", ok,
          json.dumps([res_ccw, res_cw], ensure_ascii=False))

    # 内部与边界点不受安全距离影响：分类与证据类型保持原样。
    res, _ = decisions(square, [(5, 5), (0, 0), (10, 5)], margin=1000)
    check("内部/边界点不受安全距离影响",
          [r["classification"] for r in res] == ["INSIDE", "BOUNDARY", "BOUNDARY"]
          and [r["decision"] for r in res] == ["FORBIDDEN"] * 3
          and res[0]["evidence"]["type"] == "horizontal_ray"
          and res[1]["evidence"]["type"] == "boundary"
          and res[2]["evidence"]["type"] == "boundary",
          json.dumps(res, ensure_ascii=False))

    # 零安全距离与未传字段完全等价（旧接口行为不变）。
    probes_small = [(5, -1), (5, 5), (0, 0), (-2, -1), (20, 20)]
    _, body_default = call(square, probes_small)
    _, body_zero = call(square, probes_small, margin=0)
    check("零安全距离与未传字段响应完全一致",
          body_default == body_zero
          and all(r["classification"] != "NEAR_BOUNDARY" for r in body_zero["results"]))

    # 非法安全距离：负数、非整数、超过坐标上限，整单 422 且无部分结果。
    for label, bad in [("负数", -1), ("非整数浮点", 1.5), ("整数值浮点", 5.0),
                       ("数字字符串", "3"), ("布尔", True), ("超过坐标上限", 100_000_001)]:
        status, body = call(square, [(5, 5)], margin=bad, expect_status=422)
        check(f"非法安全距离（{label}）整单 422 且无结果",
              status == 422 and body["error"]["code"] == "VALIDATION_ERROR" and "results" not in body,
              f"{status} {str(body)[:200]}")

    # --- 8. permitted_pockets 许可口袋 --------------------------------------
    print("[8] permitted_pockets 许可口袋")
    big = [(0, 0), (100, 0), (100, 100), (0, 100)]
    pa = [(10, 10), (20, 10), (20, 20), (10, 20)]
    pb = [(40, 40), (50, 40), (50, 50), (40, 50)]

    # 口袋内部放行并按输入顺序归因；口袋外的区域内部点维持禁抛。
    res, _ = decisions(big, [(15, 15), (45, 45), (30, 30)], pockets=[pa, pb])
    check("口袋内部点 ALLOWED + PERMITTED_POCKET 且按输入顺序携带口袋序号",
          res[0]["decision"] == "ALLOWED" and res[0]["classification"] == "PERMITTED_POCKET"
          and res[0]["evidence"].get("type") == "permitted_pocket"
          and res[0]["evidence"].get("pocket_index") == 0
          and res[1]["decision"] == "ALLOWED" and res[1]["classification"] == "PERMITTED_POCKET"
          and res[1]["evidence"].get("pocket_index") == 1,
          json.dumps(res[:2], ensure_ascii=False))
    check("口袋外的区域内部点维持 INSIDE + FORBIDDEN（射线证据）",
          res[2]["classification"] == "INSIDE" and res[2]["decision"] == "FORBIDDEN"
          and res[2]["evidence"].get("type") == "horizontal_ray",
          json.dumps(res[2], ensure_ascii=False))

    # 口袋边界（边与顶点）上的点仍禁抛，证据保持原裁决链路形式。
    res, _ = decisions(big, [(10, 15), (20, 20), (15, 10)], pockets=[pa])
    check("口袋边界（边与顶点）上的点仍 FORBIDDEN 且证据为原射线形式",
          all(r["decision"] == "FORBIDDEN" and r["classification"] == "INSIDE"
              and r["evidence"].get("type") == "horizontal_ray" for r in res),
          json.dumps(res, ensure_ascii=False))

    # 安全距离：许可范围向口袋内部收缩，距口袋边界不超过该距离继续禁抛。
    res, _ = decisions(big, [(12, 15), (13, 15), (15, 15)], margin=3, pockets=[pa])
    ev = res[0]["evidence"]
    check("距口袋边界小于安全距离的点继续禁抛（既有精确距离证据 + 口袋序号）",
          res[0]["classification"] == "NEAR_BOUNDARY" and res[0]["decision"] == "FORBIDDEN"
          and ev.get("type") == "near_boundary" and ev.get("pocket_index") == 0
          and (ev.get("distance2_num"), ev.get("distance2_den")) == (4, 1)
          and ev.get("exclusion_margin_cm") == 3,
          json.dumps(res[0], ensure_ascii=False))
    check("距口袋边界恰等于安全距离的点同样禁抛",
          res[1]["classification"] == "NEAR_BOUNDARY" and res[1]["decision"] == "FORBIDDEN"
          and (res[1]["evidence"].get("distance2_num"),
               res[1]["evidence"].get("distance2_den")) == (9, 1),
          json.dumps(res[1], ensure_ascii=False))
    check("口袋内超出安全距离带的点放行",
          res[2]["classification"] == "PERMITTED_POCKET" and res[2]["decision"] == "ALLOWED",
          json.dumps(res[2], ensure_ascii=False))

    # 口袋相交/接触/嵌套：整单 422，给出两个口袋序号。
    for name, second in [("边交叉重叠", [(15, 15), (25, 15), (25, 25), (15, 25)]),
                         ("共边接触", [(20, 10), (30, 10), (30, 20), (20, 20)]),
                         ("嵌套", [(12, 12), (18, 12), (18, 18), (12, 18)])]:
        status, body = call(big, [(15, 15)], pockets=[pa, second], expect_status=422)
        check(f"口袋{name}整单 422 且无结果",
              status == 422 and body["error"]["code"] == "POCKETS_INTERSECT"
              and body["error"]["details"].get("pocket_indices") == [0, 1]
              and "results" not in body,
              f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    # 口袋未严格位于禁抛区内部：顶点在外、顶点压边界、顶点全在内部但边穿越凹区域边界。
    concave_region = [(0, 0), (100, 0), (100, 20), (20, 20), (20, 80), (100, 80), (100, 100), (0, 100)]
    not_inside = [
        ("顶点在区域外", big, [(90, 90), (110, 90), (110, 110), (90, 110)]),
        ("顶点压在区域边界上", big, [(0, 10), (10, 10), (10, 20), (0, 20)]),
        ("顶点全在内部但边穿越区域边界", concave_region, [(40, 10), (60, 10), (50, 90)]),
    ]
    for name, region, pocket in not_inside:
        status, body = call(region, [(15, 15)], pockets=[pocket], expect_status=422)
        check(f"口袋{name}整单 422 并给出口袋序号",
              status == 422 and body["error"]["code"] == "POCKET_NOT_INSIDE_REGION"
              and body["error"]["details"].get("pocket_index") == 0
              and "results" not in body,
              f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    # 数量与总顶点超限：整单 422 并给出计数。
    eleven = [[(10 + 8 * k, 10), (16 + 8 * k, 10), (16 + 8 * k, 16), (10 + 8 * k, 16)]
              for k in range(11)]
    status, body = call(big, [(15, 15)], pockets=eleven, expect_status=422)
    check("11 个口袋整单 422 并给出口袋计数",
          status == 422 and body["error"]["code"] == "TOO_MANY_POCKETS"
          and body["error"]["details"].get("pocket_count") == 11
          and "results" not in body,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    wide = [(0, 0), (40000, 0), (40000, 5000), (0, 5000)]
    over = [ngon(50, 2000 + 3000 * k, 2500, 1000) for k in range(10)]
    status, body = call(wide, [], pockets=over, expect_status=422)
    check("外环与口袋总顶点 504 超限整单 422 并给出总顶点计数",
          status == 422 and body["error"]["code"] == "TOO_MANY_TOTAL_VERTICES"
          and body["error"]["details"].get("total_vertex_count") == 504
          and "results" not in body,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")
    at_limit = [ngon(50, 2000 + 3000 * k, 2500, 1000) for k in range(9)]
    at_limit.append(ngon(46, 29000, 2500, 1000))
    res, _ = decisions(wide, [(2000, 2500), (29000, 2500)], pockets=at_limit)
    check("总顶点数恰为 500 时正常裁决并按输入顺序归因",
          res[0]["classification"] == "PERMITTED_POCKET" and res[0]["decision"] == "ALLOWED"
          and res[0]["evidence"].get("pocket_index") == 0
          and res[1]["classification"] == "PERMITTED_POCKET"
          and res[1]["evidence"].get("pocket_index") == 9,
          json.dumps(res, ensure_ascii=False)[:300])

    # 未提交（或提交空列表）时响应与当前版本完全一致。
    probes2 = [(15, 15), (5, -1), (0, 0), (150, 150)]
    _, body_missing = call(big, probes2)
    _, body_empty = call(big, probes2, pockets=[])
    check("空口袋列表与未提交字段响应完全一致",
          body_missing == body_empty
          and all(r["classification"] != "PERMITTED_POCKET" for r in body_missing["results"]))

    # --- 9. POST /region-area-summary 面积汇总 -------------------------------
    print("[9] POST /region-area-summary 面积汇总")

    # 矩形外环 + 两个口袋：核对面积守恒（汇总值 = 外环 - 全部口袋）。
    _, summary = call_summary(big, pockets=[pa, pb])
    check("矩形外环与两个口袋的二倍面积逐项正确",
          summary.get("region_area2") == 20000
          and summary.get("pockets") == [{"pocket_index": 0, "area2": 200},
                                         {"pocket_index": 1, "area2": 200}],
          json.dumps(summary, ensure_ascii=False))
    check("面积守恒：net = 外环绝对二倍面积 - 全部口袋绝对二倍面积",
          summary.get("net_area2") == 20000 - 200 - 200
          and summary.get("net_area2")
          == summary.get("region_area2") - sum(p["area2"] for p in summary.get("pockets", [])),
          json.dumps(summary, ensure_ascii=False))

    # 顺/逆时针与末尾重复闭合点写法（区域与口袋同时变体）结果完全一致。
    _, summary_cw = call_summary(list(reversed(big)),
                                 pockets=[list(reversed(pa)), list(reversed(pb))])
    _, summary_closed = call_summary(big + [big[0]],
                                     pockets=[pa + [pa[0]], pb + [pb[0]]])
    check("顺/逆时针面积汇总结果一致", summary == summary_cw,
          json.dumps([summary, summary_cw], ensure_ascii=False))
    check("省略/重写首点面积汇总结果一致", summary == summary_closed,
          json.dumps([summary, summary_closed], ensure_ascii=False))

    # 未提交口袋：汇总值等于外环面积，口袋列表为空。
    _, summary_none = call_summary(big)
    _, summary_empty = call_summary(big, pockets=[])
    check("无口袋时 net 等于外环面积且口袋列表为空",
          summary_none == summary_empty
          and summary_none.get("pockets") == []
          and summary_none.get("net_area2") == summary_none.get("region_area2") == 20000,
          json.dumps(summary_none, ensure_ascii=False))

    # 非法口袋：整单 422 并保留口袋定位。
    bowtie = [(40, 40), (50, 50), (50, 40), (40, 48)]
    status, body = call_summary(big, pockets=[pa, bowtie], expect_status=422)
    check("自交口袋整单 422 并定位到口袋序号",
          status == 422 and set(body.keys()) == {"error"}
          and body["error"]["code"] == "SELF_INTERSECTING_POLYGON"
          and body["error"]["details"].get("pocket_index") == 1,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")
    status, body = call_summary(big, pockets=[[(90, 90), (110, 90), (110, 110), (90, 110)]],
                                expect_status=422)
    check("口袋越出禁抛区整单 422 并定位到口袋序号",
          status == 422 and body["error"]["code"] == "POCKET_NOT_INSIDE_REGION"
          and body["error"]["details"].get("pocket_index") == 0,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")
    status, body = call_summary(big, pockets=eleven, expect_status=422)
    check("11 个口袋整单 422 并给出口袋计数",
          status == 422 and body["error"]["code"] == "TOO_MANY_POCKETS"
          and body["error"]["details"].get("pocket_count") == 11,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")
    status, body = call_summary(wide, pockets=over, expect_status=422)
    check("总顶点超限整单 422 并给出总顶点计数",
          status == 422 and body["error"]["code"] == "TOO_MANY_TOTAL_VERTICES"
          and body["error"]["details"].get("total_vertex_count") == 504,
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    # 面积接口不接收待判点与安全距离：未声明字段整单 422。
    for name, extra in [("points", {"points": [{"x": 1, "y": 1}]}),
                        ("exclusion_margin_cm", {"exclusion_margin_cm": 3})]:
        status, body = call_summary(big, extra=extra, expect_status=422)
        check(f"面积接口拒绝未声明字段 {name}",
              status == 422 and body["error"]["code"] == "VALIDATION_ERROR"
              and any(issue.get("loc") == ["body", name]
                      for issue in body["error"]["details"].get("issues", [])),
              f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    print(f"\n== {checks} checks, {len(failures)} failures ==")
    if failures:
        print("\n".join(failures))
        return 1
    print("ACCEPTANCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
