"""M0 探针：拿真引擎验证调研得出的结论。

调研是读源码得出的，没跑过程序。这个脚本专门去证伪它。

已经推翻的两条（以实测为准）：
  - 调研说 play 会放行自杀 —— 实测会拒
  - 调研说配置默认档位是 rank_5k —— 实际是 preaz_5k（2016 年的老人类棋风）

关键协议事实：genmove 会把子落在引擎内部棋盘上，落完不能再 play 一遍。

跑法：python probe_m0.py
一次性诊断脚本，不是产品的一部分。
"""

import time

from katago import KataGo

LEVELS = list(range(9, 0, -1))

# 一个标准劫形。E5 是白子（只有 E4 一口气），E4 空着，周围全是黑。
#
#      D    E    F
#  6   B    B    B
#  5   B    W    B      <- 白 E5
#  4   B    .    B      <- E4 空，黑下这里提 E5
#  3   B    B    B
#
# 黑下 E4 提白 E5 后，黑 E4 也只剩 E5 一口气，白立即回提就复原了局面 —— 这就是劫。
KO_SETUP = [
    ("B", "A19"), ("W", "E5"),
    ("B", "D3"),  ("W", "T19"),
    ("B", "E3"),  ("W", "T18"),
    ("B", "F3"),  ("W", "T17"),
    ("B", "D4"),  ("W", "T16"),
    ("B", "F4"),  ("W", "T15"),
    ("B", "D6"),  ("W", "T14"),
    ("B", "E6"),  ("W", "T13"),
    ("B", "F6"),  ("W", "T12"),
    ("B", "D5"),  ("W", "T11"),
    ("B", "F5"),  ("W", "T10"),
]


def header(text):
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def try_play(k, color, vertex, label):
    """返回 True 表示被接受了。"""
    try:
        k.play(color, vertex)
        print(f"  [接受] {label}")
        return True
    except Exception as exc:
        print(f"  [拒绝] {label}  ->  {exc}")
        return False


def probe_play_legality(k):
    header("1. play 到底检查什么")

    k.setup_board()
    k.play("B", "D4")
    try_play(k, "B", "D4", "落在已有子上（应当拒绝）")
    try_play(k, "B", "Q16", "黑连下两手，不检查轮次（预期接受）")
    try_play(k, "W", "Z99", "场外坐标（应当拒绝）")

    k.cmd("clear_board")
    try_play(k, "B", "A2", "黑 A2")
    try_play(k, "W", "T19", "白 T19")
    try_play(k, "B", "B1", "黑 B1")
    try_play(k, "W", "T18", "白 T18")
    try_play(k, "B", "T17", "黑 T17")
    try_play(k, "W", "A1", "白 A1 自杀，白子无气（应当拒绝）")

    # --- 劫 ---
    print("\n  劫：")
    k.cmd("clear_board")
    for color, vertex in KO_SETUP:
        k.play(color, vertex)
    print("  劫形就位，看棋盘:")
    print("   ", "\n    ".join(k.cmd("showboard").splitlines()[1:4]))

    try_play(k, "B", "E4", "黑 E4 提白 E5")
    board = k.cmd("showboard")
    print("     E5 还有白子吗:", "E5" in board and "O" in board.splitlines()[7])

    try_play(k, "W", "E5", "白立即回提（劫，应当拒绝）")
    try_play(k, "W", "T9", "白找劫材 T9")
    try_play(k, "B", "T8", "黑应劫")
    try_play(k, "W", "E5", "白再回提（劫材交换过，应当接受）")


def probe_genmove_format(k):
    header("2. genmove 的输出格式与坐标约定")
    k.setup_board()
    for color in ["B", "W", "B"]:
        t0 = time.time()
        move = k.genmove(color)
        print(f"  genmove {color} -> {move!r}   ({time.time() - t0:.2f}s)  "
              f"[引擎已自行落子，不要再 play]")

    print("\n  坐标约定自检：黑 A19、白 T1")
    k.cmd("clear_board")
    k.play("B", "A19")
    k.play("W", "T1")
    for line in k.cmd("showboard").splitlines()[:3]:
        print("   ", line)


def probe_levels(k):
    header("3. humanSLProfile 档位")

    print(f"  配置里的默认档位: {k.cmd('kata-get-param humanSLProfile')!r}")
    print("    -> preaz_ 是 2016 年 AlphaGo 之前的人类棋风，"
          "rank_ 才是现代的。必须显式覆盖。\n")

    for kyu in LEVELS:
        profile = k.set_level(kyu)
        got = k.cmd("kata-get-param humanSLProfile")
        print(f"  {profile:<10} 读回 {got:<10} {'[一致]' if profile == got else '[!! 不一致]'}")

    try:
        k.cmd("kata-set-param humanSLProfile rank_30k")
        print("  [问题] 非法档位 rank_30k 被接受了")
    except Exception as exc:
        print(f"  [符合预期] 非法档位被拒: {exc}")

    print("\n  同一局面下弱档与强档的选点（各 6 手，棋风不同才会不一样）:")
    for profile in ["rank_20k", "rank_9d"]:
        k.cmd(f"kata-set-param humanSLProfile {profile}")
        k.setup_board()
        moves = [k.genmove("B" if i % 2 == 0 else "W") for i in range(6)]
        print(f"    {profile:<10} {' '.join(moves)}")


def probe_timing(k):
    header("4. 每步耗时（当前是配置里的单线程）")
    for kyu in LEVELS:
        k.set_level(kyu)
        k.setup_board()
        times = []
        for i in range(4):
            t0 = time.time()
            k.genmove("B" if i % 2 == 0 else "W")
            times.append(time.time() - t0)
        print(f"  {kyu} 级: 平均 {sum(times)/len(times):.2f}s  最慢 {max(times):.2f}s")


def probe_scoring(k):
    header("5. 规则与终局")
    k.setup_board(rules="chinese", komi=7.5)
    print("  kata-get-rules:", k.cmd("kata-get-rules"))

    k.cmd("clear_board")
    k.play("B", "D4")
    k.play("W", "Q16")
    print("\n  刻意不等终局就问 final_score（中国规则下它返回的是神经网络估算，不是数子）:")
    t0 = time.time()
    print("    final_score:", k.cmd("final_score"), f"({time.time() - t0:.2f}s)")

    k.cmd("clear_board")
    k.play("B", "pass")
    k.play("W", "pass")
    try:
        dead = k.cmd("final_status_list dead")
        print("  双方 pass 后 final_status_list dead:", dead or "(空)")
    except Exception as exc:
        print(f"  final_status_list dead 失败: {exc}")


def main():
    k = KataGo(timeout=300)
    try:
        probe_play_legality(k)
        probe_genmove_format(k)
        probe_levels(k)
        probe_timing(k)
        probe_scoring(k)
    finally:
        k.close()
    header("M0 探针结束")


if __name__ == "__main__":
    main()
