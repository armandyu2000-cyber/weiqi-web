"""KataGo 的 GTP 客户端：把引擎当子进程，用 stdin/stdout 说 GTP。

只负责“把命令发出去、把回答读回来”。围棋规则不在这里 —— 引擎的 play 命令
不检查自杀/打劫/轮次，它不是裁判，只是个出主意的。
"""

import platform
import queue
import shutil
import subprocess
import threading
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
IS_MAC = platform.system() == "Darwin"

# 每个后端：可执行文件叫什么 + 必须的启动参数。
#
# Windows：这台机器有 RTX 3060，但实测 OpenCL 后端一搜索就崩
#   （CL_OUT_OF_RESOURCES，显存其实是空的，是驱动层的问题，各种配置组合都试过）。
#   DirectML 走 DirectX 12，绕开 OpenCL 驱动栈，实测正常。Eigen 纯 CPU 作退路。
#
# macOS：DirectML 是 Windows 专属，没有对应物。而且 KataGo 官方从来不发 macOS
#   预编译包（v1.0 到 v1.18.2 全部 61 个 release，一个 mac 资源都没有），
#   只能 brew install katago —— 所以这一项的可执行文件不在 vendor 里，
#   得去 PATH 上找，见 engine_paths()。
ENGINES = {
    "directml": {"exe": "katago.exe", "overrides": {"onnxProvider": "directml"}},
    "eigen": {"exe": "katago.exe", "overrides": {}},
    "macos": {"exe": "katago", "overrides": {}},
}
ENGINE = "macos" if IS_MAC else "directml"

CONFIG_NAME = "gtp_human5k_example.cfg"

# 业余档位：级位 9 级 → 1 级，段位 1 段 → 9 段，共 18 档。
# 档位名就是 humanSL 的 rank 后缀，rank_9k / rank_9d 这样。
LEVELS = [f"{n}k" for n in range(9, 0, -1)] + [f"{n}d" for n in range(1, 10)]

# 档位决定的不只是「模仿谁」，还有「搜多深」。
#
# 配置文件只给了 5 级那一套值，而档位是运行时切的 —— 参数不跟着切的话，
# 「9 段」就只是换了个模仿对象，强度仍然被 5 级那套配置卡死。引擎自带的
# cfg 里有一句原话：高段和职业档位不能指望达到被模仿者的真实强度，
# 因为模型不做搜索。
#
# 所以：级位纯模仿（PiklLambda 顶到 1e8，搜索不参与选点），段位把 PiklLambda
# 压到 0.08 + RootExplore 抬到 0.8，让搜索真的参与选点、把引擎看不上的手抑制掉。
#
# 实测（见 README「实测」那节）：同一个局面下，段位越高搜得越多，25 → 135，
# 跟着 maxVisits 的上限走，第一选点也会变。所以这张表是有效的。
#
# ⚠️ 但高段位不保证比低段位强 —— cfg 自己警告过模型不做强度校准，它是按
# 「某个段位的人会怎么下」采样的。实测见过 7 段输给 5 段，属预期内。
_KYU_PARAMS = {
    "maxVisits": 40,
    "humanSLChosenMovePiklLambda": 100000000,   # 大到等于关掉纠偏
    "humanSLRootExploreProbWeightless": 0.0,
    "chosenMoveTemperature": 0.70,
}


def search_params(level):
    """档位 -> 该设的一组搜索参数。纯函数，不碰引擎，方便测。

    段位越高的 visits 上限越高（1 段 100 → 9 段 260）。这只是上限，引擎会提前
    收手，但同一局面下实测确实是单调的（1d 搜 25、9d 搜 135），见上面那段注释。
    """
    if not level.endswith("d"):
        return dict(_KYU_PARAMS)
    return {
        "maxVisits": 100 + (int(level[:-1]) - 1) * 20,
        "humanSLChosenMovePiklLambda": 0.08,     # 强烈抑制引擎看不上的手
        "humanSLRootExploreProbWeightless": 0.8,  # 八成搜索量用来评估模仿手
        "chosenMoveTemperature": 0.25,
    }

HUMAN_MODEL = VENDOR / "models" / "b18c384nbt-humanv0.bin.gz"
NORMAL_MODEL = VENDOR / "models" / "g170-b15c192-s497233664-d149638345.bin.gz"

# 关掉引擎自带的人工延迟。cfg 自己写着（第 49 行）：这是「随机停一下再答话，
# 免得答得太快」，不是「多想一会儿」—— 延迟发生在搜索完成之后。
#
# 实测（probe_delay.py，同局面、温度压 0 定死选点）：
#     delayMoveScale=2.0 → 落点 R3，visits 53，胜率 0.5476，墙钟 4.15s
#     delayMoveScale=0   → 落点 R3，visits 53，胜率 0.5476，墙钟 0.25s
# 落点、搜索量、形势判断全都一模一样，差的就是那 3.9 秒干等。
# 所以这里一律关掉：胜负还是下完数子，时间不浪费在空等上。
#
# ⚠️ 关的是延迟，不是 maxVisits —— 后者是真的在改棋（1 段搜 25、9 段搜 135，
#    第一选点都不同），别顺手一起动。
NO_DELAY = {"delayMoveScale": 0, "delayMoveMax": 0}


def engine_paths(engine=None):
    """返回 (可执行文件, 配置文件)。

    可执行文件先在 vendor/engines/<后端>/ 里找，找不到退回 PATH —— Mac 上的
    katago 是 brew 装的，只会出现在 PATH 上。

    配置文件则必须在 vendor 里：brew 那个包只给一个二进制，不带
    gtp_human5k_example.cfg，那份得 setup.py 单独下（它才带 humanSLProfile、
    delayMove 这些关键设置）。
    """
    engine = engine or ENGINE
    spec = ENGINES.get(engine)
    if spec is None:
        raise FileNotFoundError(f"不认识的引擎后端 {engine!r}")

    folder = VENDOR / "engines" / engine
    exe = folder / spec["exe"]
    if not exe.exists():
        found = shutil.which(spec["exe"])
        if found:
            exe = Path(found)
    if not exe.exists():
        hint = "，或者 brew install katago" if IS_MAC else ""
        raise FileNotFoundError(f"找不到引擎 {engine}。先跑 python setup.py{hint}")

    config = folder / CONFIG_NAME
    if not config.exists():
        raise FileNotFoundError(f"找不到配置 {config}。先跑 python setup.py")
    return exe, config


_EOF = object()  # 读线程发现引擎死了就往队列里塞这个


class GTPError(RuntimeError):
    """引擎回了 '? ...'，或者干脆没回。"""


def parse_analyze(text):
    """拆 kata-genmove_analyze 的返回，得到 (坐标, 胜率, 目差)。

    cmd() 剥掉 '=' 之后的真实长相：
        ''
        'info move R17 visits 39 ... winrate 0.473481 ... scoreLead -0.597216 ... pv R17'
        'play C10'

    winrate / scoreLead 是站在「你请求的那个颜色」这边报的 —— 实测过：
    同一个 komi 35 的局面，问黑得 0.058，问白得 0.991。

    info 行可能有好几行（每个候选点一行），只认第一行，那是最好的那一手，
    也就是这个局面的评估。字段缺了就给 None，让上层知道没得显示，别编个 0 出来。
    """
    vertex, winrate, lead = "", None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("info"):
            fields = line.split()
            if winrate is None and "winrate" in fields:
                winrate = float(fields[fields.index("winrate") + 1])
            if lead is None and "scoreLead" in fields:
                lead = float(fields[fields.index("scoreLead") + 1])
        else:
            vertex = line
    # 分析模式给这一手带了个 'play ' 前缀，普通 genmove 不带 —— 两种都认
    return vertex.removeprefix("play ").strip(), winrate, lead


OWNERSHIP_SIZE = 19


def parse_ownership(text):
    """从 kata-raw-nn 的返回里取出每一点的归属，361 个数。

    长相是一行 'whiteOwnership' 表头，后面正好跟 19 行、每行 19 个数。
    正值偏白、负值偏黑，大约在 -1..1 之间。

    版式核对过：第 0 行就是棋盘最上面一行，跟 rules 的 y=0 是同一套下标，
    可以直接按下标用，不用翻转。
    """
    rows, grabbing = [], False
    for line in text.splitlines():
        if line.strip() == "whiteOwnership":
            grabbing = True
            continue
        if grabbing:
            rows.append([round(float(v), 2) for v in line.split()])
            if len(rows) == OWNERSHIP_SIZE:
                break
    if len(rows) != OWNERSHIP_SIZE or any(len(r) != OWNERSHIP_SIZE for r in rows):
        raise GTPError("归属图的形状不对，引擎输出格式可能变了")
    return [v for row in rows for v in row]


class KataGo:
    def __init__(self, engine=None, exe=None, config=None,
                 model=NORMAL_MODEL, human_model=HUMAN_MODEL,
                 overrides=None, timeout=120):
        engine = engine or ENGINE
        default_exe, default_config = engine_paths(engine)
        exe = exe or default_exe
        config = config or default_config

        # 每种后端有自己必需的启动参数（比如 ONNX 必须点名 onnxProvider），
        # 调用方给的覆盖项优先。NO_DELAY 夹在中间：它得盖过 cfg，但别挡住调用方。
        merged = dict(ENGINES.get(engine, {}).get("overrides", {}))
        merged.update(NO_DELAY)
        merged.update(overrides or {})

        args = [str(exe), "gtp", "-config", str(config), "-model", str(model)]
        if human_model:
            args += ["-human-model", str(human_model)]
        if merged:
            args += ["-override-config", ",".join(f"{k}={v}" for k, v in merged.items())]

        self.timeout = timeout
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # 启动横幅（版本号、加载路径、GTP ready）全在 stderr。
            # 绝不能把它并进 stdout —— 读循环会被那几行非 GTP 文本噎住。
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True).start()

    def _pump(self, stream):
        for line in stream:
            self._queue.put(line)
        self._queue.put(_EOF)

    def cmd(self, command, timeout=None):
        """发一条 GTP 命令，返回负载文本。错误抛 GTPError。

        响应以空行结束，且可以是多行的（showboard / list_commands），
        所以要一直读到空行为止，不能只读一行。
        """
        if self.proc.poll() is not None:
            raise GTPError(f"引擎已退出（返回码 {self.proc.returncode}）：{command}")

        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

        lines = []
        deadline = timeout or self.timeout
        while True:
            try:
                item = self._queue.get(timeout=deadline)
            except queue.Empty:
                raise GTPError(f"引擎 {deadline}s 无响应：{command}") from None
            if item is _EOF:
                raise GTPError(f"引擎在回答 {command!r} 时退出")
            if item.strip() == "":
                break
            lines.append(item.rstrip("\r\n"))

        if not lines:
            raise GTPError(f"空响应：{command}")
        head = lines[0]
        if head.startswith("="):
            return "\n".join([head[1:].lstrip()] + lines[1:])
        if head.startswith("?"):
            raise GTPError(head[1:].strip())
        # 真出现这个说明有非 GTP 文本漏进 stdout，宁可炸掉也不要静默错位
        raise GTPError(f"非 GTP 响应：{head!r}")

    # --- 常用命令的薄封装 -------------------------------------------------

    def setup_board(self, size=19, komi=7.5, rules="chinese"):
        self.cmd(f"boardsize {size}")
        self.cmd("kata-set-rules " + rules)
        self.cmd(f"komi {komi}")
        self.cmd("clear_board")

    def play(self, color, vertex):
        return self.cmd(f"play {color} {vertex}")

    def genmove(self, color):
        return self.cmd(f"genmove {color}").strip()

    def genmove_analyze(self, color):
        """跟 genmove 落同一手，外加这个局面的形势（胜率、目差）。

        用的是同一次搜索，所以拿到形势不额外花时间 —— 别去另开一条
        kata-analyze 通道，那条是流式的，cmd() 那个「读到空行」的模型接不住。
        """
        return parse_analyze(self.cmd(f"kata-genmove_analyze {color}"))

    def search_analyze(self, color):
        """只搜不下，给人支招用（实测棋盘不动）。

        别换用 kata-search：那个也是只搜不下，但它给的是采样出来的那一手，
        同一局面连问两次会变（实测 R10 / K16 各一次）。kata-search_analyze
        的 info 行里 order 0 那一手才是确定的。
        """
        return parse_analyze(self.cmd(f"kata-search_analyze {color}"))

    def raw_ownership(self):
        """当前局面每一点的归属。单次前向、没有搜索，所以又快又糊。"""
        return parse_ownership(self.cmd("kata-raw-nn 0"))

    def undo(self, times=1):
        for _ in range(times):
            self.cmd("undo")

    def set_profile(self, profile):
        """切人类棋风的档位，要的是完整档位名（rank_9d、proyear_2023 那种）。

        运行时可切，不用重启引擎，也不用重放棋谱 —— 实测对局中途来回切，
        引擎自己的棋盘一直是对的。
        """
        self.cmd(f"kata-set-param humanSLProfile {profile}")
        return profile

    def set_level(self, level):
        """切档位，形如 "5k" / "9d"。运行时可切，不用重启引擎。

        档位决定的不只是模仿谁，还有搜多深，两样都得设上 —— 见 search_params。
        参数逐个发，别合并成 kata-set-params：实测那条命令会把引擎整个搞崩。
        """
        self.set_profile(f"rank_{level}")
        for key, value in search_params(level).items():
            self.cmd(f"kata-set-param {key} {value}")
        return level

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


if __name__ == "__main__":
    # 冒烟测试：起引擎、问版本、报模型加载情况
    e = KataGo()
    try:
        print("name    :", e.cmd("name"))
        print("version :", e.cmd("version"))
        print("protocol:", e.cmd("protocol_version"))
        print("models  :", e.cmd("kata-get-models"))
    finally:
        e.close()
