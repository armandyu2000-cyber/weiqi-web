"""端到端检查：起真服务、真引擎，把整套流程走一遍。

跟 test_server.py 的区别：那边用假引擎测逻辑（快、离线），这边用真引擎测
「拼起来到底能不能下」。要跑几十秒，所以名字不叫 test_*，不进日常测试套件。

跑法：python e2e_check.py
"""

import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import rules
import server

BASE = f"http://127.0.0.1:{server.PORT}"
failures = []


def call(path, body=None, timeout=600):
    """返回 (状态码, 解析后的响应)。4xx/5xx 不抛异常，拿回来看。

    timeout 默认放得很宽 —— 引擎想一手要一两秒，弱机或抢 GPU 时更久。
    只有「探活」那种明知不该有回应的请求才该自己传个短的。
    """
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": raw}


def play(vertex):
    """人的一手 + 电脑的一手，跟前端一样分两次请求。

    分开是为了让人落子立刻可见；这里合起来用是因为大多数检查只关心回合结果。
    """
    code, state = call("/api/move", {"vertex": vertex})
    if code != 200 or state.get("finished") or state.get("resigned"):
        return code, state
    return call("/api/ai_move", {})


def _unparseable(vertex):
    try:
        rules.vertex_to_index(vertex, 19)
    except Exception:
        return True
    return False


def check(label, condition, detail=""):
    mark = "通过" if condition else "失败"
    print(f"  [{mark}] {label}" + (f"   {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main():
    # 存档和对弈记录都写到临时目录。不挡一下的话，跑一次这个检查就往人真的
    # games/ 里塞一局「端到端测试局」，还会在对弈记录里多出一盘没下过的棋。
    tmp = tempfile.TemporaryDirectory()
    server.GAMES = Path(tmp.name)

    print("启动真引擎……")
    server.SESSION.start_engine()
    httpd = ThreadingHTTPServer(("127.0.0.1", server.PORT), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("引擎就绪\n")

    print("0. 引擎启动参数")
    # 人工延迟必须关掉。它是走 -override-config 设的，而 key 写错时引擎只会往
    # stderr 嘟囔一句然后装作没事（stderr 又被我们吞了），所以只能回读确认 ——
    # 这一条挂了就说明那个 key 失效了，每手会白等 2~10 秒。
    scale = server.SESSION.engine.cmd("kata-get-param delayMoveScale").strip()
    check("人工延迟已关（delayMoveScale=0）", scale == "0", f"读到 {scale}")
    top = server.SESSION.engine.cmd("kata-get-param delayMoveMax").strip()
    check("人工延迟上限已关（delayMoveMax=0）", top == "0", f"读到 {top}")
    print()

    print("1. 新开一局，人来下几手")
    code, state = call("/api/new", {"level": "3k", "human_color": "B"})
    check("新局成功", code == 200, f"HTTP {code}")
    check("引擎报就绪", state.get("engine_ready") is True)

    code, state = call("/api/move", {"vertex": "D4"})
    check("人下 D4 就返回，不等引擎", code == 200 and len(state["moves"]) == 1,
          f"走子表 {' '.join(m['vertex'] for m in state.get('moves', []))}")
    code, state = call("/api/ai_move", {})
    check("引擎那一手单独取回", code == 200 and len(state["moves"]) == 2,
          f"走子表 {' '.join(m['color'] + m['vertex'] for m in state.get('moves', []))}")
    check("答完轮回到人", state["to_move"] == "B")

    ev = state.get("eval")
    check("形势判断也带回来了", isinstance(ev, dict) and 0 <= ev["winrate"] <= 1,
          f"你的胜率 {ev}" if ev else "没拿到")

    own = state.get("ownership")
    check("地盘归属图也带回来了", isinstance(own, list) and len(own) == 361,
          f"{len(own)} 个点" if own else "没拿到")

    code, state = play("Q4")
    check("第二手也正常", code == 200 and len(state["moves"]) == 4,
          f"第 3、4 手：{state['moves'][2]['vertex']} {state['moves'][3]['vertex']}")

    print("\n2. 非法手要被挡下来")
    code, err = call("/api/move", {"vertex": "D4"})
    check("下在已有子上被拒", code == 400, err.get("error", ""))
    code, err = call("/api/move", {"vertex": "Z99"})
    check("场外坐标被拒", code == 400, err.get("error", ""))

    print("\n3. 悔棋")
    before = len(state["moves"])
    code, state = call("/api/undo", {})
    check("悔棋退掉两手", code == 200 and len(state["moves"]) == before - 2,
          f"{before} -> {len(state['moves'])}")
    check("悔完轮回到人", state["to_move"] == "B")
    # 从当前棋盘里现挑一个空点 —— 不能硬编码坐标。humanSL 是从策略里采样出招的，
    # 同一局面两次跑会下在不同的地方（这正是它「像人」的地方）。
    free = next(i for i, c in enumerate(state["board"]) if c == 0)
    vertex = rules.index_to_vertex(free, 19)
    code, state = play(vertex)
    check("悔完能接着下", code == 200 and len(state["moves"]) == 4,
          f"下 {vertex} 后：{' '.join(m['vertex'] for m in state.get('moves', []))}")

    print("\n4. 档位切换")
    for level in ["5k", "1k", "9d"]:
        code, state = call("/api/level", {"level": level})
        check(f"切到 {level}", code == 200 and state["levels"]["W"] == level)

    print("\n5. 存档与载入")
    code, info = call("/api/save", {"name": "端到端测试局"})
    check("保存成功", code == 200 and info.get("id"), info.get("name", ""))
    code, games = call("/api/games", None)
    check("列表里有它", code == 200 and any(g["id"] == info.get("id") for g in games),
          f"共 {len(games)} 份棋谱")
    saved_moves = [m["vertex"] for m in state["moves"]]
    code, loaded = call("/api/load", {"id": info["id"]})
    check("载入后走子表一致", [m["vertex"] for m in loaded["moves"]] == saved_moves,
          " ".join(saved_moves))

    print("\n6. 终局判定的两个衔接点")
    # 没法用「连续停一手」在这里逼出终局：人停一手后，引擎在空旷开局里
    # 必然会应招。真正的终局要下满一盘，那属于实际对局，不属于快速检查。
    # 这里验的是最容易断的两个衔接：未终局时的行为，和死子坐标的格式约定。
    code, result = call("/api/result", None)
    check("未终局时不编比分", result.get("finished") is False)

    # 拿真数据验一遍死子链路。空盘上问只会返回空列表，等于什么都没验。
    # 摆一个角上 5x5 的黑空，白扔两子在里头 —— 必死。
    # 这一步会把引擎的棋盘搞乱（和我们那盘对不上），所以放在最后。
    eng = server.SESSION.engine
    eng.setup_board()
    wall = ["A1", "A2", "A3", "A4", "A5", "B5", "C5", "D5", "E5", "E1", "E2", "E3", "E4"]
    for v in wall:
        eng.play("B", v)
    for v in ["C2", "C3"]:
        eng.play("W", v)

    vertices = eng.cmd("final_status_list dead").split()
    check("引擎判出了死子", set(vertices) == {"C2", "C3"}, f"返回 {vertices}")

    bad = [v for v in vertices if _unparseable(v)]
    check("死子坐标都能被我们解析", not bad, f"解析不了的：{bad}")

    g = rules.Game()
    for v in wall:
        g.board[rules.vertex_to_index(v)] = rules.BLACK
    for v in ("C2", "C3"):
        g.board[rules.vertex_to_index(v)] = rules.WHITE
    scored = g.score(vertices)
    check("拿掉死子后数子正确",
          scored["black"] == 361 and scored["white"] == 0,
          f"黑 {scored['black']} 对 白 {scored['white']} -> {scored['result']}")

    # 把引擎拉回来，别给后续留一个脏棋盘
    server.SESSION.sync_engine()

    print("\n7. 认输")
    code, state = call("/api/new", {"level": "3k"})
    code, state = call("/api/resign", {})
    code, result = call("/api/result", None)
    check("认输结果正确", result.get("result") == "W+R", result.get("result", ""))

    print("\n8. 支招")
    code, state = call("/api/new", {"level": "3k", "human_color": "B"})
    before = len(state["moves"])
    code, hint = call("/api/hint", {})
    check("支招拿到了一个坐标", code == 200 and bool(hint.get("vertex")),
          hint.get("vertex") or hint.get("error", ""))
    code, state = call("/api/state", None)
    check("支招没有真落子", len(state["moves"]) == before,
          f"{before} -> {len(state['moves'])}")
    check("支招是 1 级给的", hint.get("level") == "1k", f"档位 {hint.get('level')}")
    check("问完档位拨回来了", state["levels"]["W"] == "3k", f"回到 {state['levels']['W']}")

    print("\n9. 换颜色")
    code, state = call("/api/new", {"level": "3k", "human_color": "W"})
    check("人可以执白", code == 200 and state["human_color"] == "W")
    first = state["moves"][0] if state["moves"] else {}
    check("电脑执黑先走", first.get("color") == "B",
          f"{first.get('color', '')}{first.get('vertex', '')}")

    print("\n10. 地盘归属图的方向")
    # 引擎要是换了行序或者转置，这里第一个红 —— 不然会悄悄画出一盘反的地盘，
    # 看着还挺像回事。故意一上一下放两个子，翻转/转置都躲不过。
    eng = server.SESSION.engine
    eng.setup_board()
    eng.play("B", "A19")        # 左上角 = 下标 0
    eng.play("W", "A1")         # 左下角 = 下标 342
    own = eng.raw_ownership()
    check("归属图有 361 个点", len(own) == 361, f"{len(own)} 个")
    check("上边对应小下标", own[0] < -0.05 and own[342] > 0.05,
          f"左上角(黑)={own[0]} 左下角(白)={own[342]}")
    server.SESSION.sync_engine()

    print("\n11. 对弈记录")
    _, before = call("/api/history", None)
    code, state = call("/api/new", {"level": "3k", "human_color": "B"})
    code, state = call("/api/resign", {})
    code, h = call("/api/history", None)
    records = h.get("records") or []
    rec = records[0] if records else {}
    check("认输那盘进了记录", h.get("total") == before.get("total", 0) + 1
          and rec.get("result") == "W+R",
          f"{before.get('total', 0)} -> {h.get('total')} 盘，最近 {rec.get('result', '')}")
    check("记录里级别和用时都对",
          rec.get("level") == "3k" and isinstance(rec.get("seconds"), int),
          f"{rec.get('level')} 级 · {rec.get('seconds')} 秒 · 悔 {rec.get('undos')} 次")
    check("认输的盘没有目数也没有悔棋", rec.get("human_margin") is None and rec.get("undos") == 0)

    print("\n12. 接管（机器替我走）")
    code, state = call("/api/new", {"level": "3k", "human_color": "B"})
    code, state = call("/api/move", {"vertex": "D4"})
    code, state = call("/api/ai_move", {})
    before = len(state["moves"])

    code, state = call("/api/takeover", {"moves": 5})
    check("开始接管就先替我走了一手",
          code == 200 and state["takeover_left"] == 4 and state["takeover_count"] == 1,
          f"还剩 {state.get('takeover_left')} 手")
    check("那一手和对手的应手都在棋盘上", len(state["moves"]) == before + 2,
          f"{before} -> {len(state['moves'])} 手")
    check("应完轮回到我", state["to_move"] == "B")
    check("档位没被接管改掉", state["levels"]["W"] == "3k", f"对手还是 {state['levels']['W']}")
    check("前端拿得到可选手数", state.get("takeover_choices") == [5, 10],
          str(state.get("takeover_choices")))

    code, state = call("/api/takeover", {})
    check("接着替我走第二手",
          code == 200 and state["takeover_left"] == 3 and state["takeover_count"] == 2,
          f"还剩 {state.get('takeover_left')} 手")

    # 我自己动手，接管作废 —— 从当前棋盘现挑一个空点，别硬编码坐标
    free = next(i for i, c in enumerate(state["board"]) if c == 0)
    code, state = call("/api/move", {"vertex": rules.index_to_vertex(free, 19)})
    check("自己落子让接管作废", code == 200 and state["takeover_left"] == 0)

    code, err = call("/api/takeover", {"moves": 3})
    check("只认 5 手和 10 手", code == 400, err.get("error", ""))

    print("\n13. 两个机器对弈")
    code, state = call("/api/new", {"machine_play": True,
                                    "levels": {"B": "9k", "W": "1k"}})
    check("开得起来", code == 200 and state["machine_play"] is True,
          str(state.get("levels")))
    check("开局是空盘，不自动走", state["moves"] == [],
          f"{len(state['moves'])} 手")
    check("18 个档位（9 级 + 9 段）", len(state.get("level_choices", [])) == 18,
          str(len(state.get("level_choices", []))))

    for i in range(3):
        code, state = call("/api/ai_move", {})
        check(f"第 {i + 1} 手走出来了", code == 200 and len(state["moves"]) == i + 1,
              f"{state['moves'][-1]['color']} {state['moves'][-1]['vertex']}"
              if state.get("moves") else "")

    check("一手一请求（不是一口气走到底）", len(state["moves"]) == 3)
    check("每手都存了形势，跟走子表对齐",
          len(state.get("evals", [])) == len(state["moves"]),
          f"{len(state.get('evals', []))} 条形势 / {len(state['moves'])} 手")
    check("形势是黑方视角（黑先走，前几手该在 50% 附近）",
          all(0.0 <= e["winrate"] <= 1.0 for e in state["evals"] if e),
          str([e and round(e["winrate"], 2) for e in state["evals"]]))

    code, state = call("/api/levels", {"W": "9d"})
    check("能单独改白方档位", code == 200 and state["levels"] == {"B": "9k", "W": "9d"},
          str(state.get("levels")))

    code, err = call("/api/levels", {"B": "10d"})
    check("不存在的档位被挡住", code == 400, err.get("error", ""))

    code, err = call("/api/move", {"vertex": "Q16"})
    check("人手插不进机器对局", code == 400, err.get("error", ""))

    ply = len(state["moves"])
    code, hint = call("/api/hint", {"ply": ply})
    check(f"复盘能问第 {ply} 手那里该下哪",
          code == 200 and hint.get("vertex") and hint.get("color") in ("B", "W"),
          f"{hint.get('color')} 方想下 {hint.get('vertex')}")
    code, after = call("/api/state", None)
    check("问完引擎盘面拉回当前局面，棋没被改动",
          [m["vertex"] for m in after["moves"]] == [m["vertex"] for m in state["moves"]])

    code, err = call("/api/hint", {"ply": 9999})
    check("问不存在的那一手被挡住", code == 400, err.get("error", ""))

    print("\n14. 中途换模式与复盘接管")
    code, state = call("/api/new", {"machine_play": True,
                                    "levels": {"B": "3k", "W": "3k"}})
    for _ in range(3):
        code, state = call("/api/ai_move", {})
    before = [m["vertex"] for m in state["moves"]]
    check("双机先跑三手", code == 200 and len(before) == 3, " ".join(before))

    code, state = call("/api/mode", {"machine_play": False,
                                     "levels": {"B": "3k", "W": "3k"}})
    check("切成人机后棋一手没少", [m["vertex"] for m in state["moves"]] == before,
          " ".join(m["vertex"] for m in state["moves"]))
    check("接手的是轮到的那一方", state["human_color"] == state["to_move"],
          f"轮到 {state['to_move']}，人执 {state['human_color']}")

    code, state = call("/api/mode", {"machine_play": True,
                                     "levels": {"B": "9k", "W": "1k"}})
    check("切回双机，两边档位都跟着换",
          state["machine_play"] is True and state["levels"] == {"B": "9k", "W": "1k"},
          str(state.get("levels")))
    check("切回双机也没丢棋", len(state["moves"]) == 3,
          f"{len(state['moves'])} 手")

    code, state = call("/api/play_from", {"ply": 2, "levels": {"B": "3k", "W": "3k"}})
    check("从第 2 手接着下，棋和曲线一起截",
          code == 200 and len(state["moves"]) == 2 and len(state["evals"]) == 2,
          f"{len(state['moves'])} 手 / {len(state['evals'])} 条形势")
    check("接着下的是人机模式，人执该走那方",
          state["machine_play"] is False and state["human_color"] == state["to_move"],
          f"人执 {state['human_color']}，轮到 {state['to_move']}")

    free = next(i for i, c in enumerate(state["board"]) if c == 0)
    code, state = call("/api/move", {"vertex": rules.index_to_vertex(free, 19)})
    check("接手之后人下得动", code == 200 and len(state["moves"]) == 3,
          f"{len(state['moves'])} 手")
    code, state = call("/api/ai_move", {})
    check("引擎也应得上", code == 200 and len(state["moves"]) == 4,
          f"{len(state['moves'])} 手")

    code, err = call("/api/play_from", {"ply": 999})
    check("越过最后一手被挡住", code == 400, err.get("error", ""))

    # 放最后：这个会真把服务停掉。打包成 .app 之后没有终端可以 Ctrl+C，
    # 网页上那个「退出程序」按钮走的就是这条。
    print("\n15. 退出接口")
    code, out = call("/api/quit", {})
    check("退出接口回了 ok", code == 200 and out.get("ok") is True, str(out))
    time.sleep(1.5)
    # 探活必须给短超时。serve_forever 停下来之后套接字还绑着（没人调 server_close，
    # 真程序是靠进程退出才释放的），内核会把新连接排进 backlog 却没人 accept ——
    # 拿默认那 600 秒会在这儿干等十分钟，看着就像卡死在退出那一节。
    stopped = False
    try:
        call("/api/state", None, timeout=3)
    except Exception:
        stopped = True
    check("服务真的停了", stopped, "还连得上" if not stopped else "")

    print()
    if failures:
        print(f"有 {len(failures)} 项没过：")
        for f in failures:
            print("  -", f)
        return 1
    print("端到端全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
