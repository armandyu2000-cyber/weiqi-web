"""人工延迟到底进不进搜索？（结论：不进，它只影响墙钟）

cfg 里默认 delayMoveScale = 2.0 / delayMoveMax = 10.0，注释写着这是「随机停一下
再答话，免得答得太快」。听起来像「想得久一点」，其实是**搜索完成之后**的等待。

两段：
  ① 同一局面问三次，前两次延迟 2.0（cfg 默认）、第三次 0。
     选点定不死 —— 压了 chosenMoveTemperature 和 chosenMoveTemperatureEarly 也
     照样采样（humanSL 的选点不吃这俩参数，实测同设置两次给出 F17 / O3）。
     所以判据不是「落点一样」，而是**形势判断一样**：胜率到小数点后四位都不动，
     而落点 / visits 的抖动在同设置下也一样有 —— 那是采样噪声，不是延迟。
  ② 开头先回读启动参数（必须是没动过任何参数的时候），再实测各档位每手耗时。

跑法：python probe_delay.py
一次性诊断脚本，不是产品的一部分。
"""

import time

from katago import KataGo

OPENING = ["Q16", "D4", "Q4", "D16", "R14"]     # 5 手，轮到白
LEVEL = "5d"                                     # 段位档：搜索真参与选点，最容易被影响


def setup(e):
    e.setup_board(size=19, komi=7.5, rules="chinese")
    for i, v in enumerate(OPENING):
        e.play("B" if i % 2 == 0 else "W", v)


def one_move(e, tag):
    """摆好同一个局面问一手，走原样返回 —— visits 只在 info 行里，解析器不给。"""
    setup(e)
    t0 = time.time()
    raw = e.cmd("kata-genmove_analyze W")
    dt = time.time() - t0
    vertex, visits, winrate = "", None, None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("info"):
            f = line.split()
            if visits is None and "visits" in f:
                visits = int(f[f.index("visits") + 1])
            if winrate is None and "winrate" in f:
                winrate = round(float(f[f.index("winrate") + 1]), 4)
        elif line:
            vertex = line.removeprefix("play ").strip()
    print(f"  {tag}: 落点={vertex} visits={visits} 胜率={winrate} 墙钟={dt:.2f}s")
    return vertex, visits, winrate, dt


def readback(e):
    """必须在动过任何参数之前做 —— kata-set-param 会盖掉启动参数，
    先跑实验再回读，读到的是实验改过的值，白测。"""
    print("0. 启动参数回读（-override-config 关延迟）")
    for k in ("delayMoveScale", "delayMoveMax"):
        print(f"   {k} = {e.cmd('kata-get-param ' + k).strip()}")


def part1(e):
    print("\n① 延迟进不进搜索")
    e.set_level(LEVEL)
    # 定不死选点，但还是压上：能少一档随机就少一档
    e.cmd("kata-set-param chosenMoveTemperature 0")
    e.cmd("kata-set-param chosenMoveTemperatureEarly 0")
    e.cmd("kata-set-param delayMoveScale 2")     # 启动时已关掉，这儿摆回 cfg 默认
    e.cmd("kata-set-param delayMoveMax 10")

    a = one_move(e, "延迟 2.0")
    b = one_move(e, "延迟 2.0（同设置对照）")
    e.cmd("kata-set-param delayMoveScale 0")
    c = one_move(e, "延迟 0  ")

    print()
    print(f"  落点：{a[0]} / {b[0]} / {c[0]}   同设置两次也不同: {a[0] != b[0]}"
          "   ← 所以落点差异不能赖延迟")
    # 判据要跟「同设置的固有抖动」比，不能拍一个绝对阈值 —— 胜率本来就有
    # 千分之几的跑动。延迟带来的差 ≤ 同设置的差，就算没影响。
    print(f"  胜率：{a[2]} / {b[2]} / {c[2]}   延迟带来的差 {abs(a[2] - c[2]):.4f}"
          f" ≤ 同设置的差 {abs(a[2] - b[2]):.4f}: {abs(a[2] - c[2]) <= abs(a[2] - b[2])}"
          "   ← 这个才是判据")
    print(f"  visits：{a[1]} / {b[1]} / {c[1]}   同设置之间也差 {abs(a[1] - b[1])}"
          "（早停噪声）")
    print(f"  墙钟：{a[3]:.2f} / {b[3]:.2f} / {c[3]:.2f}"
          f"   延迟 2.0 那两次自己就差 {abs(a[3] - b[3]):.2f}s（延迟是随机的）")
    print(f"  延迟 0 时只剩 {c[3]:.2f}s 纯搜索，其余全是干等")

    # 收尾：摆回程序真正用的那套，别把后面测耗时的部分带偏
    e.cmd("kata-set-param delayMoveMax 0")


def part2(e):
    print("\n② 各档位每手耗时（延迟已关）")
    e.setup_board(size=19, komi=7.5, rules="chinese")
    turn = 0
    for level, n in (("9k", 3), ("1k", 3), ("1d", 3), ("9d", 5)):
        e.set_level(level)
        times = []
        for _ in range(n):
            t0 = time.time()
            e.genmove_analyze("B" if turn % 2 == 0 else "W")   # 黑白交替，别连下两手
            turn += 1
            times.append(time.time() - t0)
        line = "  ".join(f"{t:.2f}s" for t in times)
        print(f"  {level:>3}: {line}   最慢 {max(times):.2f}s")


if __name__ == "__main__":
    engine = KataGo()
    try:
        readback(engine)
        part1(engine)
        part2(engine)
    finally:
        engine.close()
