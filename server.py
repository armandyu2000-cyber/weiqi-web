"""本地 Web 服务：把规则引擎和 KataGo 拼成一个能下的程序。

只用标准库的 http.server，不装框架 —— 单机一个人用，没有并发压力。

两条重要的分工：
  - rules.Game 是棋盘的唯一权威，合法性和胜负都由它说了算
  - 引擎只是“你觉得这手该下哪”的顾问，它说的每一手都要过一遍我们的规则
"""

import json
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import katago
from rules import Game, IllegalMove, vertex_to_index

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
PORT = 8731
IS_FROZEN = getattr(sys, "frozen", False)


def _data_dir():
    """写出去的东西（存档、对弈记录、引擎日志）放哪个目录。

    平时就是项目目录。打包成 .app / .exe 之后挪去用户目录：程序可能被放在
    只读的位置（Mac 上没签名的包还会被 Gatekeeper 挂到随机只读路径），
    而且重新打包一次不该把之前下的棋一起冲掉。代码和模型仍然从 ROOT 读，
    那些不用写。启动脚本也可以直接给一个 GO_DATA_DIR 把它顶掉。
    """
    if not IS_FROZEN:
        return ROOT
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Go"
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Go"


DATA = Path(os.environ.get("GO_DATA_DIR") or _data_dir())
GAMES = DATA / "games"

# 18 档：业余 9 级 → 1 级，业余 1 段 → 9 段。档位名就是 kata 的 rank 后缀。
# 档位定义在 katago 那边，因为它同时决定「模仿谁」和「搜多深」。
LEVELS = katago.LEVELS
DEFAULT_LEVEL = "3k"           # 开局默认档位（沿用改造前那个「3 级」）

HINT_LEVEL = "1k"             # 支招用的档位：「一级选手」

# 接管（机器替我走）用最高段位。注意人机模型的档位只到 9 段，而且配置注释
# 自己说了：高段位「不指望达到被模仿者的真实强度，因为模型不做搜索」——
# 所以段位档在 katago.search_params 里会连带把搜索打开。
TAKEOVER_LEVEL = "9d"
TAKEOVER_CHOICES = (5, 10)    # 一次接管替我走几手

# 引擎认输的门槛。KataGo 自带的认输在 humanSL 配置下够不着 —— 四个阈值
# （Threshold / ConsecTurns / MinScoreDifference / MinMovesPerBoardArea）
# 全调松了它也只会还手，所以拿它自己报的胜率自己判。
# 要连着 RESIGN_TURNS 手都满足才认，免得被一次搜索抖动骗了。
RESIGN_WINRATE = 0.02         # 引擎自己的胜率掉到 2% 以下
RESIGN_LEAD = -15.0           # 而且落后 15 目以上
RESIGN_TURNS = 3


def history_path():
    """对弈记录的落点。从 GAMES 推出来，测试改了 server.GAMES 就一起跟着改。

    叫 .jsonl 不叫 .json 是有意的：一行一条、只追加。这样写记录不用
    读-改-写整个文件，也顺带躲开了 list_games() 的 *.json 通配。
    """
    return GAMES / "history.jsonl"


def load_history():
    """读全部对弈记录。文件不在、或者中间有写坏的行，都跳过 ——

    这是个统计功能，不该因为它读不出来就把下棋搞挂。
    """
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_history(record):
    history_path().parent.mkdir(exist_ok=True)
    with history_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# 自然音文件夹。跟 GAMES 一样挂在 DATA 下面 —— 打包之后程序目录可能在只读位置，
# 用户没法往里丢文件，所以必须跟着数据目录走。
NATURE = DATA / "nature"

# 认的后缀。都是浏览器 <audio> 原生能解的，不做转码。
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}

# 随程序发的自然音，setup.py 下到这里。第一次运行时拷进 nature/，见 seed_nature()。
BUILTIN_SOUNDS = ROOT / "vendor" / "sounds"


def seed_nature():
    """第一次运行时，把内置的自然音拷一份进 nature/。

    拿「nature/ 整个不存在」当标记，而不是「nature/ 是空的」：用户删掉内置那几段、
    或者整批换成自己录的，都不该被程序又塞回来。
    """
    if NATURE.exists() or not BUILTIN_SOUNDS.is_dir():
        return
    NATURE.mkdir(parents=True, exist_ok=True)
    for src in sorted(BUILTIN_SOUNDS.iterdir()):
        if src.is_file() and src.suffix.lower() in AUDIO_EXTS:
            shutil.copy(src, NATURE / src.name)


def list_nature():
    """自然音文件夹的清单。

    文件夹不存在就顺手建出来 —— 用户总得有个地方丢自己的录音，而且删光了内置
    那几段之后，程序还应该能重新把它建回来。
    """
    try:
        NATURE.mkdir(parents=True, exist_ok=True)
        files = sorted(p.name for p in NATURE.iterdir()
                       if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    except OSError:
        files = []
    return {"dir": str(NATURE), "files": files}


def nature_file(name):
    """把 URL 里的文件名换成路径，不认的一律 None。

    这是全程序唯一「用户给的字符串会变成文件路径」的地方，所以只认单纯的
    文件名：带分隔符、带 .. 、不在 nature/ 里的全挡掉。挡掉之后 NATURE / name
    必定是它的直接子项，穿越无从谈起。
    """
    if not name or Path(name).name != name:
        return None
    if Path(name).suffix.lower() not in AUDIO_EXTS:
        return None
    path = NATURE / name
    try:
        if not path.is_file():
            return None
    except (OSError, ValueError):     # ValueError：文件名里有 NUL 之类的怪字符
        return None
    return path


class Session:
    """一盘棋的全局状态。单机单人，不做会话隔离。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.engine = None
        self.game = Game()
        # 每方各自的档位。人机模式下只有引擎那一边有意义（见 _opponent_level），
        # 机机模式下两边都得管。
        self.levels = {"B": DEFAULT_LEVEL, "W": DEFAULT_LEVEL}
        self.machine_play = False     # True = 两个机器对下，人只看
        # 跟 game.moves 一一对齐的形势表，复盘画曲线用。每项是「这一手落下之前」
        # 的形势，一律黑方视角；引擎当时没报数就是 None。
        self.evals = []
        self.human_color = "B"
        self.game_id = None
        self.game_name = ""
        self.resigned = None      # 认输方，没认输就是 None
        self.eval = None          # 最近一次形势判断（人的视角），见 _human_eval
        self.ownership = None     # 每点的归属，前端画地盘用
        self.resign_proposal = None   # 引擎提了认输、人等答复，存引擎的颜色
        self.resign_disabled = False  # 人说过「这盘不接受认输」
        self.resign_streak = 0        # 连续多少手引擎都快输了
        self.started_at = None        # 开局时刻，算对弈时间用
        self.undo_count = 0
        self.hint_count = 0
        self.takeover_left = 0        # 本次接管还剩几手，0 就是没在接管
        self.takeover_count = 0       # 这盘累计机器替我走了几手
        self.recorded = False         # 这盘记过账了没有

    # --- 引擎 -------------------------------------------------------------

    def start_engine(self):
        """起引擎并设好规则。慢（要加载模型），所以只在服务启动时做一次。

        这里不设档位：档位由每一手在 _one_engine_move 里按走子方现设 ——
        两边档位不同，就没有一个「当前档位」能在启动时一次定死。
        """
        self.engine = katago.KataGo()
        self.engine.setup_board(size=self.game.size, komi=self.game.komi, rules="chinese")

    def sync_engine(self):
        """把引擎的棋盘拉回与我们一致：清盘重放。

        悔棋、载入存档、以及引擎给出我们不认的一手之后，都走这里。
        比逐个 undo 多几个往返，但换来的是永不失步 —— 两边棋盘不同步
        是最难查的一类 bug，宁可多花几十毫秒。

        注意 genmove 返回时已经落过子了，而这里的每一手都还没落，所以要 play。
        """
        if self.engine is None:
            return
        self.engine.setup_board(size=self.game.size, komi=self.game.komi, rules="chinese")
        for move in self.game.moves:
            self.engine.play(move["color"], move["vertex"])

    # --- 对局 -------------------------------------------------------------

    def new_game(self, level=None, human_color=None, levels=None, machine_play=None):
        """开新局。

        level 是人机模式那一个选择器给的（两边设成一样，反正只有引擎那边的
        有意义）；levels 是机机模式给的两边各自的档位。两个都收，省得前端
        按模式分两条路。
        """
        with self.lock:
            if level is not None:
                self.levels = {"B": self._check_level(level),
                               "W": self._check_level(level)}
            if levels is not None:
                self.levels = {color: self._check_level(levels.get(color, DEFAULT_LEVEL))
                               for color in ("B", "W")}
            if machine_play is not None:
                self.machine_play = bool(machine_play)
            if human_color in ("B", "W"):
                self.human_color = human_color
            self.game = Game()
            self.game_id = None
            self.game_name = ""
            self.resigned = None
            self.eval = None
            self.ownership = None
            self.resign_proposal = None
            self.resign_disabled = False
            self.resign_streak = 0
            self.started_at = time.time()
            self.undo_count = 0
            self.hint_count = 0
            self.takeover_left = 0
            self.takeover_count = 0
            self.recorded = False
            self.evals = []
            self.sync_engine()
            # 机机模式不自动开局：人得先看见空盘，点了「开始对弈」才走。
            if self.engine and not self.machine_play \
                    and self.game.to_move != self.human_color:
                self._ai_reply()
            return self.state()

    def switch_mode(self, machine_play, levels=None, human_color=None):
        """对局中途换模式／换执色，不重开一局。

        跟 new_game 就差一句话：棋留着。棋盘、走子表、形势曲线一概不动 ——
        这正是它存在的理由，换个模式不该把刚下的一盘丢掉。

        人机模式下让谁执子有两种给法：
          - 不传 human_color：轮谁就接谁（点「人机对局」接手走这条）
          - 传：就用那一方（设置卡里点「我执白」走这条）。这时若轮到机器，
            先让它应到轮人为止，否则人会对着一个不是自己的回合发呆。
        """
        with self.lock:
            if self.game.finished or self.resigned:
                raise IllegalMove("这盘已经结束了，开新局吧")
            if self.engine is None:
                raise IllegalMove("引擎还没起来")
            self.machine_play = bool(machine_play)
            if levels is not None:
                self.levels = {c: self._check_level(levels.get(c, DEFAULT_LEVEL))
                               for c in ("B", "W")}
            if not self.machine_play:
                self.human_color = (human_color if human_color in ("B", "W")
                                    else self._current_name())
            self.takeover_left = 0
            self.resign_proposal = None   # 机机模式没人答复认输，留着就是个死提案
            self.resign_streak = 0
            self.eval = None
            self.sync_engine()
            if not self.machine_play and self.game.to_move != self._human_as_int():
                self._ai_reply()
            return self.state()

    def play_from(self, ply, levels=None):
        """复盘到第 ply 手，从这里接着当实战下。

        第 ply 手之后的棋全部丢掉 —— 只在内存里丢，原存档文件一个字不动：
        接着下的是一盘新棋，不该把老棋谱改掉。存也存成新的一份（game_id
        清空），名字后面缀「接续」，好跟原局分得开。

        截完该谁走，人就接谁。已经下完的棋也能这么救活（resigned 清掉、
        终局标记跟着棋盘一起重放掉）。
        """
        with self.lock:
            if self.engine is None:
                raise IllegalMove("引擎还没起来")
            if not 0 <= ply < len(self.game.moves):
                raise IllegalMove("得停在两手之间才能接着下")
            old_name = self.game_name
            # 重放前 ply 手建个新棋盘，比连着 undo 干净：提子数、劫禁着、
            # 终局标记全是重放出来的，不会漏掉哪一样。
            self.game = Game.from_dict({**self.game.to_dict(),
                                        "moves": self.game.moves[:ply]})
            del self.evals[ply:]
            self.game_id = None
            self.game_name = f"{old_name} 接续" if old_name else ""
            self.resigned = None
            self.recorded = False          # 这是一盘新棋，得重新记一笔
            self.started_at = time.time()
            self.undo_count = 0
            self.hint_count = 0
            self.takeover_count = 0
            self.eval = None
            self.ownership = None
            return self.switch_mode(False, levels)

    def human_move(self, vertex):
        """只落人的这一手就返回，不等引擎 —— 前端要立刻把子显示出来。

        引擎那一手由 ai_move() 单独要。拆成两次往返看着多，但人点击到看见
        自己的子在屏幕上，中间只隔着一次本地 HTTP，不用等引擎那几秒。
        """
        with self.lock:
            if self.machine_play:
                raise IllegalMove("双机对局中，人手插不进去")
            color = self._current_name()
            self.game.play(color, vertex)          # 不合法会抛，棋盘不动
            self.evals.append(None)                # 人这一手没有形势，占位对齐
            self._engine_play(color, vertex)
            self.takeover_left = 0                 # 你自己动手了，接管作废
            self._record_result()                  # 自己停一手收的官，这盘就到这儿了
            return self.state()

    def ai_move(self):
        """推进引擎。

        人机模式下「一直走到轮到我」，机机模式下「只走一手」—— 一手一请求
        前端才能把每一步画出来，也才停得下来。
        """
        with self.lock:
            if self.machine_play:
                self._machine_move()
            else:
                self._ai_reply()
            return self.state()

    def undo(self):
        """悔到该人走为止：连着退掉引擎那一手和人的那一手。"""
        with self.lock:
            if not self.game.moves:
                raise IllegalMove("没有可悔的棋了")
            self.undo_count += 1
            self.takeover_left = 0                 # 棋都退回去了，接管作废
            while True:
                last = self.game.moves[-1]
                self.game.undo()
                if last["color"] == self.human_color or not self.game.moves:
                    break
            # 形势和地盘都是上一手的，棋盘都退回去了就别再显示
            self.eval = None
            self.ownership = None
            del self.evals[len(self.game.moves):]   # 曲线要跟着退，否则错位
            self.sync_engine()
            return self.state()

    def resign(self):
        with self.lock:
            self.game.finished = True
            self.resigned = self.human_color
            self._record_result()
            return self.state()

    def hint(self):
        """给一手建议，不落子。

        临时切到 HINT_LEVEL 问一手 —— 人问的是「一级选手会怎么下」，
        跟他当前选的对手棋力无关。问完 sync_engine 把档位和棋盘一起拨回来：
        search_analyze 实测不落子，但万一哪天变了，重放一遍就永远对齐。
        """
        with self.lock:
            if self.game.finished or self.resigned:
                raise IllegalMove("这盘已经结束了")
            if self.game.to_move != self._human_as_int():
                raise IllegalMove("现在不是你在走")
            if self.engine is None:
                raise IllegalMove("引擎还没起来")
            self.engine.set_level(HINT_LEVEL)
            try:
                vertex, winrate, lead = self.engine.search_analyze(self.human_color)
            finally:
                # 档位拨回对手那套。sync_engine 只管棋盘，不管档位 ——
                # 每手落子时本来也会重设，但留着这个不变量，引擎的状态问起来
                # 永远跟「现在该谁走」对得上。
                self.engine.set_level(self._opponent_level())
                self.sync_engine()
            self.hint_count += 1
            return {"vertex": vertex, "level": HINT_LEVEL,
                    "eval": self._human_eval(self.human_color, winrate, lead)}

    def takeover(self, moves=None):
        """机器替我走一手。

        moves 有值 = 开始一次新的接管（5 或 10 手），没值 = 接着走下一手。
        前端一手一请求，跟落子那条路一样 —— 所以每手都能立刻画出来，
        也不用让一个请求闷头跑十手、把界面晾着。
        """
        with self.lock:
            if self.game.finished or self.resigned:
                raise IllegalMove("这盘已经结束了")
            if self.engine is None:
                raise IllegalMove("引擎还没起来")
            if moves is not None:
                moves = int(moves)
                if moves not in TAKEOVER_CHOICES:
                    raise IllegalMove("接管手数只能是 5 或 10")
                self.takeover_left = moves
            if self.takeover_left <= 0:
                raise IllegalMove("现在没有在接管")
            if self.game.to_move != self._human_as_int():
                raise IllegalMove("现在不是你在走")
            self._takeover_move()
            return self.state()

    def accept_resign(self):
        """人接受引擎认输。"""
        with self.lock:
            if self.resign_proposal is None:
                raise IllegalMove("电脑没有认输")
            self.resigned = self.resign_proposal
            self.resign_proposal = None
            self.game.finished = True
            self._record_result()
            return self.state()

    def decline_resign(self):
        """人不接受，接着下。这盘之后引擎不再提认输 ——

        不然它下一手又要认一次，等于没得选。
        """
        with self.lock:
            if self.resign_proposal is None:
                raise IllegalMove("电脑没有认输")
            self.resign_proposal = None
            self.resign_disabled = True
            self.resign_streak = 0
            # 提认输那一下引擎没落子，但这几手它可能已经在自己盘上走过了
            # （我们判它认输时它其实已经落了一手），重放一遍最稳。
            self.sync_engine()
            self._ai_reply()
            return self.state()

    # --- 内部 -------------------------------------------------------------

    def _current_name(self):
        from rules import COLOR_NAMES
        return COLOR_NAMES[self.game.to_move]

    def _check_level(self, level):
        """档位名过一遍白名单。前端传什么都不信。"""
        level = str(level)
        if level not in LEVELS:
            raise IllegalMove(f"没有这个棋力档位：{level}")
        return level

    def _engine_side(self):
        """人机模式下引擎执的那一方。

        机机模式没有「引擎那一方」，但那时也没人问这个问题 —— 用它的人都
        先判过 machine_play。
        """
        return "W" if self.human_color == "B" else "B"

    def _opponent_level(self):
        """人机模式下「对手的档位」，记录和显示都用它。"""
        return self.levels[self._engine_side()]

    def _engine_play(self, color, vertex):
        if self.engine is None:
            return
        try:
            self.engine.play(color, vertex)
        except Exception as exc:
            print(f"[警告] 引擎拒绝了我们已接受的 {color} {vertex}：{exc}")

    def _ai_reply(self):
        """一直让引擎走到该人走为止。"""
        from rules import COLOR_NAMES
        while (not self.game.finished) and self.game.to_move != self._human_as_int():
            if not self._one_engine_move(COLOR_NAMES[self.game.to_move]):
                break
        self._record_result()

    def _machine_move(self):
        """两个机器对下时的下一手：谁该走就让谁按自己的档位走。

        一手就返回（不像 _ai_reply 那样循环）—— 前端一请求一手，每一步都画得
        出来，人也能随时喊停。引擎不许认输：自对弈没人答复，认了这盘就没了。
        """
        from rules import COLOR_NAMES
        if self.engine is None:
            raise IllegalMove("引擎还没起来")
        if self.game.finished:
            return
        self._one_engine_move(COLOR_NAMES[self.game.to_move], allow_resign=False)
        self._record_result()

    def _takeover_move(self):
        """替我走一手（最高段位），再让对手应到又轮到我。

        档位只在这一次调用里临时拨高 —— 每手都会按走子方重设档位，所以走完
        不用拨回来，下一手自己就设对了。不重放棋谱：实测中途来回切
        humanSLProfile，引擎自己的棋盘一直是对的。
        """
        from rules import COLOR_NAMES
        self._one_engine_move(COLOR_NAMES[self._human_as_int()],
                              level=TAKEOVER_LEVEL, allow_resign=False)
        self.takeover_left -= 1
        self.takeover_count += 1
        self._ai_reply()

    def _one_engine_move(self, color, level=None, allow_resign=True):
        """让引擎走一手并落到我们的棋盘上。返回 False 表示该收手了。

        level 不为空就临时用那个档位，否则用走子方自己的档位。
        allow_resign=False 表示这手是替人走的，引擎不能替人认输，
        它的胜率也不能拿去算「对手该不该认输」。

        正常情况下这里不重放棋谱：人走一手、引擎答一手，两边的棋盘天然是一致的。
        sync_engine 是给“已经不同步了”用的，不是每手都做 —— 那是 O(n) 的往返。
        """
        for attempt in range(3):
            winrate = lead = None
            # 档位每轮重设：走子方可能是黑也可能是白，两边档位未必一样；
            # 重试分支里的 sync_engine 也会动引擎那边。
            self.engine.set_level(level or self.levels[color])
            try:
                # genmove_analyze 跟 genmove 是同一次搜索，只是顺手把形势带回来，
                # 不额外花时间 —— 人「现在赢面多大」就这么白捡了。
                vertex, winrate, lead = self.engine.genmove_analyze(color)
            except Exception as exc:
                print(f"[警告] genmove 失败：{exc}")
                vertex = "pass"
            self.eval = self._human_eval(color, winrate, lead)

            if vertex == "resign":
                # 引擎自己认输。这个配置下几乎见不到（阈值全调松它也只还手），
                # 但真出现了就照办 —— 除非人已经说过这盘不接受认输，那就当它停一手。
                if allow_resign and not self.resign_disabled:
                    self.resign_proposal = color
                    self.takeover_left = 0     # 要人答复了，接管先停下
                    return False
                vertex = "pass"

            if allow_resign and self._engine_gives_up(winrate, lead):
                self.resign_proposal = color
                self.takeover_left = 0
                return False

            try:
                # 引擎自己的搜索是严格的，但我们的劫规则比它严（位置超级劫），
                # 极少数情况下它会给出我们不认的一手。
                self.game.play(color, vertex)
                self._record_eval(color, winrate, lead)
                self._refresh_ownership()
                return True
            except IllegalMove as exc:
                print(f"[警告] 引擎给出我们不认的一手 {vertex}（{exc}），"
                      f"第 {attempt + 1} 次重试")
                self.sync_engine()

        # 三次都不认，就替它停一手 —— 宁可让这盘棋难看地走完，也别卡死
        print(f"[警告] 引擎连续给出非法手，{color} 改为停一手")
        self.game.play(color, "pass")
        self._record_eval(color, winrate, lead)
        self._refresh_ownership()
        return True

    def _record_eval(self, color, winrate, lead):
        """把这一手落下之前的形势存进 evals，一律翻成黑方视角。

        曲线只有一条，黑白不统一视角的话两边会反着跳，看着像血压计。
        只有落子成功了才记，所以 evals 的长度永远跟着 game.moves 走。
        """
        self.evals.append(self._view_from("B", color, winrate, lead))

    def _human_as_int(self):
        from rules import BLACK, WHITE
        return BLACK if self.human_color == "B" else WHITE

    def review_hint(self, ply=0):
        """复盘用：拨回第 ply 手之前那个局面，问引擎「这里该下哪」。

        跟普通支招不是一回事 —— 支招问的是「现在」，这个问的是「当时」。
        引擎的棋盘只有当前局面，所以要重放回去，问完再 sync 回来。因此它比
        支招慢（多一趟 O(ply) 的重放），但只在人点按钮时发生。

        ply 处该谁走，就问谁：evals[i] 说的正是「第 i 手落下之前」，两者对齐。
        """
        with self.lock:
            if self.engine is None:
                raise IllegalMove("引擎还没起来")
            total = len(self.game.moves)
            if not 0 <= ply <= total:
                raise IllegalMove("没有这一手")
            color = self._current_name() if ply == total else self.game.moves[ply]["color"]
            try:
                self.engine.setup_board(size=self.game.size, komi=self.game.komi,
                                        rules="chinese")
                for move in self.game.moves[:ply]:
                    self.engine.play(move["color"], move["vertex"])
                vertex, winrate, lead = self.engine.search_analyze(color)
            finally:
                self.sync_engine()
            return {"vertex": vertex, "color": color, "ply": ply,
                    "eval": self._view_from("B", color, winrate, lead)}

    def _human_eval(self, engine_color, winrate, lead):
        """把引擎报的形势翻成人的视角。

        引擎报的永远是它自己的胜率（实测：传什么颜色就报那个颜色的），
        而人要的是「我赢面多大」，所以得翻过来。
        字段缺了就返回 None —— 宁可显示「—」，也别留个上一局的旧数字在那。
        """
        return self._view_from(self.human_color, engine_color, winrate, lead)

    @staticmethod
    def _view_from(whose, engine_color, winrate, lead):
        """把引擎报的形势翻成「whose」那一方的视角。

        引擎报的永远是它被问的那个颜色的胜率（实测：同一个局面问黑得 0.058、
        问白得 0.991）。字段缺了就返回 None —— 宁可显示「—」，也别留个
        上一局的旧数字在那。
        """
        if winrate is None:
            return None
        flip = engine_color != whose
        return {
            "winrate": round(1 - winrate if flip else winrate, 3),
            "lead": None if lead is None else round(-lead if flip else lead, 1),
        }

    def _engine_gives_up(self, winrate, lead):
        """引擎是不是该认输了（它自己不会认，见模块头那几个常量的注释）。

        要连着 RESIGN_TURNS 手都满足才认。人说过不接受认输就一路 False。
        """
        if self.resign_disabled or winrate is None or lead is None:
            self.resign_streak = 0
            return False
        if winrate <= RESIGN_WINRATE and lead <= RESIGN_LEAD:
            self.resign_streak += 1
        else:
            self.resign_streak = 0
        return self.resign_streak >= RESIGN_TURNS

    def _record_result(self):
        """这盘下完就记一笔。没下完、或者已经记过了，什么都不做。

        只有最后这一下会去问引擎判死子（result() 里那次），所以落在
        human_move 里也不拖累平时落子的速度 —— 一盘只走这一次。
        """
        if self.recorded or not (self.game.finished or self.resigned):
            return
        self.recorded = True

        machine = self.machine_play
        outcome = self.result()
        margin = outcome.get("margin")
        # 机机模式没有「人」这一方，目数就按黑方视角原样记
        if not machine and margin is not None and self.human_color != "B":
            margin = -margin          # 翻成人的视角：正数就是他赢多少目
        record = {
            "ended": time.strftime("%Y-%m-%d %H:%M"),
            "seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "machine_play": machine,
            "levels": dict(self.levels),
            "level": None if machine else self._opponent_level(),   # 给老前端看
            "human_color": None if machine else self.human_color,
            "undos": self.undo_count,
            "hints": self.hint_count,
            "takeover": self.takeover_count,
            "moves": len(self.game.moves),
            "result": outcome.get("result", ""),
            "winner": outcome.get("winner"),
            # margin 一律黑方视角；human_margin 是人机模式那个「人」的视角
            "margin": None if margin is None else round(margin, 1),
            "human_margin": None if (machine or margin is None) else round(margin, 1),
            "human_won": None if machine else outcome.get("winner") == self.human_color,
        }
        try:
            append_history(record)
        except OSError as exc:
            # 记不上账不该把刚下完的这盘棋搞崩
            print(f"[警告] 写对弈记录失败：{exc}")

    @staticmethod
    def history(limit=20):
        """对弈记录，新的在前。只回最近的几条 —— 前端每手都会来拉一次。"""
        records = load_history()
        return {"total": len(records), "records": records[-limit:][::-1]}

    def _refresh_ownership(self):
        """取一次每点归属给前端画地盘。引擎不在或取不到就清空。"""
        if self.engine is None:
            self.ownership = None
            return
        try:
            self.ownership = self.engine.raw_ownership()
        except Exception as exc:
            print(f"[警告] 取归属图失败：{exc}")
            self.ownership = None

    # --- 结果 -------------------------------------------------------------

    def result(self):
        """终局判定。死子交给引擎判，数子由我们自己做。

        中国规则下 KataGo 的 final_score 返回的是神经网络估算值而不是数子，
        所以不能直接用它。
        """
        if self.resigned:
            winner = "W" if self.resigned == "B" else "B"
            return {"finished": True, "resigned": self.resigned,
                    "winner": winner, "result": f"{winner}+R"}
        if not self.game.finished:
            # 没下完就别给比分，免得把人误导了
            return {"finished": False}

        dead = []
        if self.engine is not None:
            try:
                raw = self.engine.cmd("final_status_list dead")
                # 引擎的返回是外部输入，先过一遍坐标解析再交给数子。
                # 一个畸形坐标不该让整个终局判定炸掉。
                for vertex in raw.split():
                    try:
                        vertex_to_index(vertex, self.game.size)
                        dead.append(vertex)
                    except IllegalMove:
                        print(f"[警告] 引擎给了个解析不了的死子坐标，已跳过：{vertex!r}")
            except Exception as exc:
                print(f"[警告] 取死子失败，按无死子数：{exc}")
        return {"finished": True, "dead": dead, **self.game.score(dead)}

    # --- 存档 -------------------------------------------------------------

    @staticmethod
    def _free_id():
        """存档 id 是秒级时间戳，同一秒里存两次会撞。而「从某一手接着下」
        恰恰就是刚存完就从那儿重开 —— 撞上就把原局盖了，正好毁掉那个功能
        唯一的承诺。撞了往后加一位。
        """
        stem = time.strftime("%Y%m%d-%H%M%S")
        game_id, n = stem, 1
        while (GAMES / f"{game_id}.json").exists():
            game_id = f"{stem}-{n}"
            n += 1
        return game_id

    def save(self, name=""):
        with self.lock:
            GAMES.mkdir(exist_ok=True)
            if self.game_id is None:
                self.game_id = self._free_id()
            self.game_name = name or self.game_name or time.strftime("对局 %m-%d %H:%M")
            payload = {
                "name": self.game_name,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "machine_play": self.machine_play,
                "levels": dict(self.levels),
                "evals": self.evals,
                "level": None if self.machine_play else self._opponent_level(),
                "human_color": None if self.machine_play else self.human_color,
                **self.game.to_dict(),
            }
            (GAMES / f"{self.game_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            return {"id": self.game_id, "name": self.game_name}

    @staticmethod
    def _levels_from_payload(payload):
        """存档里的档位。

        老存档（这个功能之前存的）只有一个人机模式的 level，而且是整数 9~1。
        新存档是 {"B": "5k", "W": "9d"}。两种都得认，不然以前存的棋谱全打不开。
        """
        levels = payload.get("levels")
        if levels:
            return {c: str(levels.get(c) or DEFAULT_LEVEL) for c in ("B", "W")}
        old = payload.get("level")
        if old is None:
            token = DEFAULT_LEVEL
        elif isinstance(old, str):
            token = old
        else:
            token = f"{int(old)}k"
        return {"B": token, "W": token}

    def load(self, game_id):
        with self.lock:
            payload = json.loads((GAMES / f"{game_id}.json").read_text(encoding="utf-8"))
            self.game = Game.from_dict(payload)
            self.levels = self._levels_from_payload(payload)
            self.machine_play = bool(payload.get("machine_play", False))
            # 老存档没有 evals。补/截到跟走子表一样长 —— 曲线错位比缺一段难查。
            evals = payload.get("evals") or []
            self.evals = (evals + [None] * len(self.game.moves))[:len(self.game.moves)]
            self.human_color = payload.get("human_color") or "B"
            self.game_id = game_id
            self.game_name = payload.get("name", "")
            self.eval = None
            self.ownership = None
            self.resign_proposal = None
            self.takeover_left = 0             # 接管是临时的，不随存档走
            self.takeover_count = 0
            self.sync_engine()
            return self.state()

    @staticmethod
    def list_games():
        GAMES.mkdir(exist_ok=True)
        out = []
        for path in sorted(GAMES.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "id": path.stem,
                "name": payload.get("name", path.stem),
                "created": payload.get("created", ""),
                "moves": len(payload.get("moves", [])),
                "machine_play": bool(payload.get("machine_play", False)),
                "levels": Session._levels_from_payload(payload),
            })
        return out

    # --- 序列化 -----------------------------------------------------------

    def state(self):
        return {
            **self.game.state(),          # 里面已经有 to_move（"B"/"W"）
            "resigned": self.resigned,
            "levels": dict(self.levels),        # 每方当前档位
            "level_choices": LEVELS,            # 可选档位
            "machine_play": self.machine_play,
            "evals": self.evals,
            "human_color": self.human_color,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "engine_ready": self.engine is not None,
            "eval": self.eval,
            "ownership": self.ownership,
            "resign_proposal": self.resign_proposal,
            "takeover_left": self.takeover_left,
            "takeover_count": self.takeover_count,
            "takeover_choices": list(TAKEOVER_CHOICES),
        }


SESSION = Session()


# --- HTTP -----------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """多这一层就为了关掉 allow_reuse_address（Windows 上）。

    http.server 默认开 SO_REUSEADDR。在 Windows 上它允许第二个进程绑同一个
    端口 —— 之后连接全跑去找先绑的那个，看着就像「改了代码重启了却不生效」。
    这个项目里已经为它查过一次假故障了。
    macOS/Linux 上 SO_REUSEADDR 顶不掉活着的监听，留着对快速重启有好处。
    """
    allow_reuse_address = platform.system() != "Windows"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 本地单机，每个请求刷一行日志只是噪音

    def _quit(self):
        """把 serve_forever 叫停，让 main() 走正常的收尾。

        打包成 .app 之后没有终端可以按 Ctrl+C，不给这条路就只能去活动监视器
        强杀进程了。先等一下再叫停 —— 得让上面那个响应先发出去，不然浏览器
        那边看到的是连接被掐断。
        """
        time.sleep(0.2)
        self.server.shutdown()

    # --- 工具 ---

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_json({"error": "没有这个文件"}, 404)
            return
        body = path.read_bytes()
        kind = {".html": "text/html", ".js": "text/javascript",
                ".css": "text/css", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
                ".oga": "audio/ogg", ".opus": "audio/ogg",
                ".wav": "audio/wav",
                }.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        # 音频是二进制，后面贴个 charset 是胡来；只有文本才需要。
        # ponytail: 没做 Range —— 本地曲子几 MB，浏览器整个缓冲，切歌不受影响，
        # 只是在缓冲完之前进度条拖不动。真要拖进度条再加 206。
        self.send_header("Content-Type",
                         f"{kind}; charset=utf-8" if kind.startswith("text/")
                         else kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_nature(self, raw_name):
        """nature/ 里的一条。名字先过 nature_file()，认不出来的就是 404。"""
        path = nature_file(unquote(raw_name))
        if path is None:
            return self.send_json({"error": "没有这个声音"}, 404)
        return self.send_file(path)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # --- 路由 ---

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                return self.send_file(WEB / "index.html")
            if self.path.startswith("/web/"):
                return self.send_file(WEB / self.path[len("/web/"):])
            if self.path == "/api/state":
                return self.send_json(SESSION.state())
            if self.path == "/api/games":
                return self.send_json(SESSION.list_games())
            if self.path == "/api/history":
                return self.send_json(Session.history())
            if self.path == "/api/nature":
                return self.send_json(list_nature())
            if self.path.startswith("/nature/"):
                return self.send_nature(self.path[len("/nature/"):])
            if self.path == "/api/result":
                return self.send_json(SESSION.result())
            return self.send_json({"error": "没有这个地址"}, 404)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "服务端出错了，看控制台"}, 500)

    def do_POST(self):
        try:
            body = self.read_body()
            if self.path == "/api/new":
                return self.send_json(SESSION.new_game(
                    body.get("level"), body.get("human_color"),
                    body.get("levels"), body.get("machine_play")))
            if self.path == "/api/mode":
                # 对局中途换模式／换执色：棋留着，只换谁在下、按哪套档位。
                # human_color 不传就是「轮谁就接谁」。
                return self.send_json(SESSION.switch_mode(
                    body.get("machine_play"), body.get("levels"),
                    body.get("human_color")))
            if self.path == "/api/play_from":
                # 复盘到某一手，从这里接着当实战下。
                return self.send_json(SESSION.play_from(
                    int(body.get("ply", 0)), body.get("levels")))
            if self.path == "/api/move":
                return self.send_json(SESSION.human_move(body.get("vertex", "")))
            if self.path == "/api/ai_move":
                return self.send_json(SESSION.ai_move())
            if self.path == "/api/hint":
                # 带 ply 是复盘问「当时该下哪」，不带是给现在支招
                if body.get("ply") is None:
                    return self.send_json(SESSION.hint())
                return self.send_json(SESSION.review_hint(int(body["ply"])))
            if self.path == "/api/takeover":
                # 带 moves 是开一次新接管，不带就是接着走下一手
                return self.send_json(SESSION.takeover(body.get("moves")))
            if self.path == "/api/accept_resign":
                return self.send_json(SESSION.accept_resign())
            if self.path == "/api/decline_resign":
                return self.send_json(SESSION.decline_resign())
            if self.path == "/api/undo":
                return self.send_json(SESSION.undo())
            if self.path == "/api/resign":
                return self.send_json(SESSION.resign())
            if self.path == "/api/level":
                # 人机模式那一个选择器：两边设成一样，反正只有引擎那边的有意义
                level = SESSION._check_level(body.get("level", SESSION.levels["B"]))
                SESSION.levels = {"B": level, "W": level}
                return self.send_json(SESSION.state())
            if self.path == "/api/levels":
                # 机机模式：黑白各设各的。只改传进来的那几个颜色。
                for color in ("B", "W"):
                    if color in body:
                        SESSION.levels[color] = SESSION._check_level(body[color])
                return self.send_json(SESSION.state())
            if self.path == "/api/quit":
                self.send_json({"ok": True})
                threading.Thread(target=self._quit, daemon=True).start()
                return
            if self.path == "/api/save":
                return self.send_json(SESSION.save(body.get("name", "")))
            if self.path == "/api/load":
                return self.send_json(SESSION.load(body.get("id")))
            return self.send_json({"error": "没有这个地址"}, 404)
        except IllegalMove as exc:
            # 规则层拒绝的手：不是服务器错误，是说给用户听的理由
            self.send_json({"error": str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "服务端出错了，看控制台"}, 500)


def _already_running(url):
    """端口被占时，判断占它的到底是不是我们自己 —— 是的话直接把浏览器叫出来，
    不是的话得说清楚，别让他对着一个一闪就没的黑窗口猜。"""
    try:
        with urllib.request.urlopen(url + "api/state", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    url = f"http://127.0.0.1:{PORT}/"

    # 双击启动时的工作目录是 exe 所在的那个文件夹，可能是只读的（比如被放进
    # Program Files）。引擎的 GTP 日志是相对路径（配置里 logDir = gtp_logs），
    # 建不出那个文件夹它就不肯启动 —— 所以先把工作目录挪到能写的地方。
    if IS_FROZEN:
        DATA.mkdir(parents=True, exist_ok=True)
        os.chdir(DATA)

    # 第一次运行时把内置的自然音铺进 nature/。放在起引擎之前 —— 它只是拷几个
    # 文件，失败了也不该拦住下棋。
    seed_nature()

    # 先占端口再起引擎。引擎要加载一百多 MB 的模型、十几秒，端口起不来就没必要
    # 白等这一趟 —— 而且打包成 .app 之后这些输出只落在日志里，越早失败越好。
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        # 几乎只会是端口被占：多半是已经开着一个了。给一句能照做的话，
        # 而不是一串 traceback。
        print(f"端口 {PORT} 起不来（{exc}）。")
        if _already_running(url):
            # 双击第二次是最常见的操作，这时候报错等于没报，直接把窗口给他。
            print(f"已经开着一个了，给你把浏览器叫出来：{url}")
            webbrowser.open(url)
        else:
            print(f"端口被别的程序占了，{url} 不是我们。")
            input("（按回车关掉这个窗口）")
        return 1

    try:
        print("正在启动 KataGo 引擎……（首次加载模型要十几秒）")
        SESSION.start_engine()
    except FileNotFoundError as exc:
        # 引擎还没下载。给一句能照做的话，而不是一串 traceback。
        print(f"\n{exc}\n")
        print("先跑一次： python setup.py")
        server.server_close()
        return 1

    print(f"引擎就绪，可以下了：{url}   （Ctrl+C 退出）")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收工")
    finally:
        if SESSION.engine:
            SESSION.engine.close()
    return 0


if __name__ == "__main__":
    # 带上 raise：不然 main() 返回的 1（端口被占、引擎没下）会被丢掉，
    # 进程一律以 0 退出，失败看着像成功。
    raise SystemExit(main())
