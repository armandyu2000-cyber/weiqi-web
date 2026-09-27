"""M0 补充探针：CPU 与 GPU 后端的速度对比。

CPU(Eigen) 的结论已经量出来了 —— 1 线程 11.8s，4 线程 7.9s，8 线程反而
9.3s。加线程没用，说明卡在不随线程缩放的固定开销上（人类网的前向传播）。
所以这台机器必须走 GPU。

注意两件事：
  - genmove 会把子落在引擎内部棋盘上，落完不能再 play 一遍
  - visits 数不要为了提速而调大。强度由档位(profile)和 visits 共同决定，
    调大 visits 会让 AI 比标称段位强。这里只调线程数，visits 保持 40。
"""

import time

from katago import KataGo

OPENING = [
    ("B", "Q16"), ("W", "D4"), ("B", "Q4"), ("W", "D16"),
    ("B", "R14"), ("W", "F17"), ("B", "D10"), ("W", "K10"),
]


def measure(engine, threads, visits=40, profile="rank_3k", moves=6):
    k = KataGo(
        engine=engine,
        timeout=600,
        overrides={
            "numSearchThreads": threads,
            "maxVisits": visits,
            "humanSLProfile": profile,  # 覆盖配置里的 preaz_5k 默认值
        },
    )
    try:
        k.setup_board()
        for color, vertex in OPENING:
            k.play(color, vertex)
        times = []
        for i in range(moves):
            color = "B" if (len(OPENING) + i) % 2 == 0 else "W"
            t0 = time.time()
            k.genmove(color)
            times.append(time.time() - t0)
        return times
    finally:
        k.close()


def main():
    print("同样局面、同样 visits=40、同样档位 rank_3k，只换后端和线程数\n")
    print(f"{'后端':<8}{'线程':>4}  {'平均':>8}  {'最慢':>8}   逐手")
    print("-" * 72)

    for engine, threads in [("directml", 1), ("directml", 8), ("directml", 16)]:
        try:
            times = measure(engine, threads)
        except Exception as exc:
            print(f"{engine:<8}{threads:>4}  失败: {exc}", flush=True)
            continue
        avg = sum(times) / len(times)
        print(f"{engine:<8}{threads:>4}  {avg:>7.2f}s  {max(times):>7.2f}s   "
              + " ".join(f"{t:.1f}" for t in times), flush=True)


if __name__ == "__main__":
    main()
