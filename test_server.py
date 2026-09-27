"""服务端对局逻辑的检查。用一个假引擎，不碰真的 KataGo —— 快，而且离线可跑。

真引擎那边（GTP 协议、模型加载）由 probe_m0.py 负责验证。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import katago
import rules
import server
import setup
from rules import BLACK, IllegalMove


class FakeEngine:
    """按剧本出招的假引擎。剧本走完就一路停一手。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.played = []
        self.level = None
        self.profile = None
        self.genmoves = []      # 每次出招时引擎处在哪个档位，接管测试靠这个看
        self.searched = []      # 支招问的是哪一方
        self.rejected = []
        # 引擎自报的形势。测试改这两个值就能造出「它快输了」的局面。
        self.winrate = 0.4
        self.lead = 6.0
        # 真引擎是按「你问的颜色」报胜率的，所以要能按色给不同值才测得准
        self.winrate_by_color = {}
        self.lead_by_color = {}
        self.hint_move = "Q16"

    def setup_board(self, **kwargs):
        self.played.clear()

    def set_profile(self, profile):
        self.profile = profile

    def set_level(self, level):
        self.level = level
        self.set_profile(f"rank_{level}")

    def play(self, color, vertex):
        if (color, vertex) in self.rejected:
            raise RuntimeError("假装引擎拒绝这一手")
        self.played.append((color, vertex))

    def genmove(self, color):
        return self.script.pop(0) if self.script else "pass"

    def genmove_analyze(self, color):
        """假引擎不做分析：坐标照剧本走，形势按 winrate/lead 报。"""
        self.genmoves.append((color, self.profile))
        return (self.genmove(color),
                self.winrate_by_color.get(color, self.winrate),
                self.lead_by_color.get(color, self.lead))

    def search_analyze(self, color):
        """支招：只回一手，不动棋盘。"""
        self.searched.append(color)
        return self.hint_move, 0.7, 3.0

    def raw_ownership(self):
        return [0.0] * 361

    def cmd(self, command):
        return ""


def make_session(script=None):
    session = server.Session()
    session.engine = FakeEngine(script)
    session.sync_engine()
    return session


_TMP = None
_ORIG_GAMES = None


def setUpModule():
    """整个模块共用一个临时存档目录。

    对局一结束就会自动写对弈记录，不挡一下的话跑一次测试就往人真的
    games/ 里灌一堆垃圾记录。
    """
    global _TMP, _ORIG_GAMES
    _TMP = tempfile.TemporaryDirectory()
    _ORIG_GAMES = server.GAMES
    server.GAMES = Path(_TMP.name)


def tearDownModule():
    server.GAMES = _ORIG_GAMES
    _TMP.cleanup()


def play(session, vertex):
    """人的一手 + 电脑的一手，合成一个来回。

    真实流程里这是两次请求（先 /api/move 显示人的子，再 /api/ai_move），
    拆开是为了让人不用等引擎。测试关心的是回合结果，所以在这里合回去。
    """
    state = session.human_move(vertex)
    if not state["finished"] and not state["resigned"]:
        state = session.ai_move()
    return state


class TestTurnFlow(unittest.TestCase):
    def test_levels_cover_every_rank_up_to_nine_dan(self):
        """档位不限于 9 级到 1 级，还得一路到 9 段。"""
        self.assertEqual(server.LEVELS, katago.LEVELS)
        self.assertIn("9k", server.LEVELS)
        self.assertIn("1d", server.LEVELS)
        self.assertIn("9d", server.LEVELS)
        self.assertNotIn("10d", server.LEVELS)

    def test_human_move_returns_before_engine_moves(self):
        """人的子必须能单独拿到 —— 前端靠这个立刻显示，不等引擎想完。"""
        s = make_session(["Q16"])
        state = s.human_move("D4")
        self.assertEqual([m["vertex"] for m in state["moves"]], ["D4"])
        self.assertEqual(state["to_move"], "W")     # 轮到电脑，还没走

        state = s.ai_move()
        self.assertEqual([m["vertex"] for m in state["moves"]], ["D4", "Q16"])

    def test_human_move_then_ai_reply(self):
        s = make_session(["Q16"])
        state = play(s, "D4")
        self.assertEqual([m["vertex"] for m in state["moves"]], ["D4", "Q16"])
        self.assertEqual(state["to_move"], "B")     # 又轮到人了

    def test_illegal_move_is_rejected_and_changes_nothing(self):
        s = make_session(["Q16"])
        play(s, "D4")
        with self.assertRaises(IllegalMove):
            play(s, "Q16")                      # 已经有子了
        self.assertEqual(len(s.game.moves), 2)

    def test_ai_plays_first_when_human_takes_white(self):
        s = make_session(["Q16"])
        state = s.new_game(human_color="W")
        self.assertEqual(state["moves"][0]["color"], "B")   # 电脑执黑先走
        self.assertEqual(state["to_move"], "W")

    def test_engine_moves_reach_our_board(self):
        s = make_session(["Q16"])
        play(s, "D4")
        self.assertEqual(s.game.moves[1]["color"], "W")
        self.assertEqual(s.game.moves[1]["vertex"], "Q16")

    def test_pass_returns_turn(self):
        s = make_session(["Q16"])
        state = play(s, "pass")
        self.assertEqual(state["moves"][0]["vertex"], "pass")
        self.assertEqual(state["to_move"], "B")


class TestUndo(unittest.TestCase):
    def test_undo_removes_both_plies(self):
        s = make_session(["Q16", "Q4"])
        play(s, "D4")
        play(s, "D16")
        self.assertEqual(len(s.game.moves), 4)
        state = s.undo()
        self.assertEqual([m["vertex"] for m in state["moves"]], ["D4", "Q16"])
        self.assertEqual(state["to_move"], "B")

    def test_undo_at_start_raises(self):
        s = make_session([])
        with self.assertRaises(IllegalMove):
            s.undo()

    def test_undo_resyncs_engine(self):
        s = make_session(["Q16"])
        play(s, "D4")
        s.undo()
        # 假引擎的 setup_board 会清空 played，然后重放剩下的棋
        self.assertEqual(s.engine.played, [])

    def test_undo_after_finish_reopens(self):
        s = make_session(["pass"])
        play(s, "D4")      # 电脑停一手
        play(s, "pass")
        self.assertTrue(s.game.finished)
        s.undo()
        self.assertFalse(s.game.finished)


class TestEngineRejectsOurRules(unittest.TestCase):
    def test_falls_back_to_pass_after_repeated_rejection(self):
        s = make_session(["D4", "D4", "D4"])    # 引擎硬要下已有的点
        play(s, "D4")                      # 人先占了 D4
        # 引擎连着给三次 D4 都被拒，最后应替它停一手而不是卡死
        self.assertEqual(s.game.moves[-1]["vertex"], "pass")
        self.assertEqual(s.game.to_move, BLACK)      # 又轮回到人

    def test_engine_resign_becomes_a_proposal(self):
        """引擎自己在协议层认输，也走「先问人」那条路，不直接结束。"""
        s = make_session(["resign"])
        state = play(s, "D4")
        self.assertEqual(state["resign_proposal"], "W")
        self.assertFalse(state["finished"], "人还没答复呢")

    def test_engine_resign_is_ignored_once_declined(self):
        s = make_session(["resign"])
        s.resign_disabled = True
        state = play(s, "D4")
        self.assertIsNone(state["resign_proposal"])
        self.assertEqual(s.game.moves[-1]["vertex"], "pass", "说不认输了就当它停一手")


class TestResult(unittest.TestCase):
    def test_unfinished_has_no_score(self):
        s = make_session(["Q16"])
        play(s, "D4")
        self.assertFalse(s.result()["finished"])

    def test_resign_result(self):
        s = make_session(["resign"])
        play(s, "D4")
        s.accept_resign()                       # 得先接受才结束
        self.assertEqual(s.result()["result"], "B+R")    # 白(电脑)认输

    def test_human_resign(self):
        s = make_session([])
        s.resign()
        self.assertEqual(s.result()["result"], "W+R")


class TestSaveLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = server.GAMES
        server.GAMES = Path(self.tmp.name)

    def tearDown(self):
        server.GAMES = self._orig
        self.tmp.cleanup()

    def test_roundtrip(self):
        s = make_session(["Q16", "Q4"])
        play(s, "D4")
        play(s, "D16")
        before = [m["vertex"] for m in s.game.moves]
        info = s.save("测试棋谱")
        self.assertTrue(info["id"])

        fresh = make_session([])
        state = fresh.load(info["id"])
        self.assertEqual([m["vertex"] for m in state["moves"]], before)
        self.assertEqual(state["game_name"], "测试棋谱")

    def test_list_games(self):
        s = make_session(["Q16"])
        play(s, "D4")
        s.save("第一盘")
        listed = server.Session.list_games()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "第一盘")
        self.assertEqual(listed[0]["moves"], 2)

    def test_save_is_valid_json_with_moves(self):
        s = make_session(["Q16"])
        play(s, "D4")
        info = s.save("x")
        payload = json.loads((Path(self.tmp.name) / f"{info['id']}.json").read_text("utf-8"))
        self.assertEqual(payload["size"], 19)
        self.assertEqual(len(payload["moves"]), 2)


class TestHumanEval(unittest.TestCase):
    """形势判断这条链路：引擎的原始回报 → 解析 → 翻成人的视角 → 送到前端。"""

    # 真引擎的原样返回（1.18.1 + humanv0，抄自实测）。解析代码就是照着它写的，
    # 引擎升级改了格式的话，这条会第一个红。
    REAL_OUTPUT = (
        "\ninfo move R17 visits 39 edgeVisits 39 utility -0.055916 winrate 0.473481 "
        "scoreMean -0.597216 scoreStdev 23.7451 scoreLead -0.597216 scoreSelfplay "
        "-0.724183 prior 0.423093 pv R17\nplay C10"
    )

    def test_parses_real_engine_output(self):
        self.assertEqual(katago.parse_analyze(self.REAL_OUTPUT),
                         ("C10", 0.473481, -0.597216))

    def test_accepts_bare_vertex_too(self):
        """普通 genmove 不带 'play ' 前缀，两种都得认。"""
        self.assertEqual(katago.parse_analyze("\nD4")[0], "D4")

    def test_missing_fields_stay_none(self):
        """引擎没给形势时不能编个 0 出来。"""
        vertex, winrate, lead = katago.parse_analyze("\npass")
        self.assertEqual(vertex, "pass")
        self.assertIsNone(winrate)
        self.assertIsNone(lead)

    def test_flips_to_human_side(self):
        s = make_session([])
        s.human_color = "B"
        # 引擎报 0.9 是它自己的；它执白，那人就是 0.1
        self.assertEqual(s._human_eval("W", 0.9, 10.0), {"winrate": 0.1, "lead": -10.0})
        self.assertEqual(s._human_eval("B", 0.9, 10.0), {"winrate": 0.9, "lead": 10.0})
        self.assertIsNone(s._human_eval("W", None, None))

    def test_parses_ownership_grid(self):
        grid = "\n".join(" ".join(["0.5"] * 19) for _ in range(19))
        got = katago.parse_ownership("symmetry 0\nwhiteWin 0.5\nwhiteOwnership\n" + grid + "\n")
        self.assertEqual(len(got), 361)
        self.assertEqual(got[0], 0.5)

    def test_bad_ownership_shape_raises(self):
        """引擎升级改了版式要炸在这里，而不是悄悄画出一盘错的地盘。"""
        with self.assertRaises(katago.GTPError):
            katago.parse_ownership("whiteOwnership\n0.1 0.2\n")

    def test_ownership_reaches_state(self):
        s = make_session(["Q16"])
        self.assertEqual(len(play(s, "D4")["ownership"]), 361)

    def test_reaches_state_and_clears_on_undo(self):
        s = make_session(["Q16"])          # 假引擎报 0.4 / +6.0，它执白
        state = play(s, "D4")
        self.assertEqual(state["eval"], {"winrate": 0.6, "lead": -6.0})
        self.assertIsNone(s.undo()["eval"], "棋盘退回去了，旧形势必须跟着清掉")


class TestResignation(unittest.TestCase):
    """引擎认输是我们自己判的 —— KataGo 自带那个在这个 humanSL 配置下够不着。

    所以这里测的全是 server 侧的阈值和状态机，一行真引擎都不用碰。
    """

    def _drive_to_the_edge(self, s):
        """把距离认输线只差一手的局面摆好。"""
        s.engine.winrate = 0.005
        s.engine.lead = -40.0
        s.resign_streak = server.RESIGN_TURNS - 1

    def test_needs_several_bad_turns_in_a_row(self):
        s = make_session([])
        for _ in range(server.RESIGN_TURNS - 1):
            self.assertFalse(s._engine_gives_up(0.005, -40.0))
        self.assertTrue(s._engine_gives_up(0.005, -40.0))

    def test_one_good_turn_resets_the_count(self):
        s = make_session([])
        s._engine_gives_up(0.005, -40.0)
        s._engine_gives_up(0.005, -40.0)
        s._engine_gives_up(0.6, 2.0)          # 缓过来一手
        for _ in range(server.RESIGN_TURNS - 1):
            self.assertFalse(s._engine_gives_up(0.005, -40.0))

    def test_score_alone_is_not_enough(self):
        """落后很多但胜率还没塌，不算输 —— 不能光看目差。"""
        s = make_session([])
        for _ in range(server.RESIGN_TURNS + 1):
            self.assertFalse(s._engine_gives_up(0.5, -60.0))

    def test_winrate_alone_is_not_enough(self):
        s = make_session([])
        for _ in range(server.RESIGN_TURNS + 1):
            self.assertFalse(s._engine_gives_up(0.005, -3.0))

    def test_missing_numbers_never_trigger(self):
        s = make_session([])
        for _ in range(server.RESIGN_TURNS + 1):
            self.assertFalse(s._engine_gives_up(None, None))

    def test_proposes_instead_of_playing(self):
        s = make_session(["Q16"])
        self._drive_to_the_edge(s)
        state = play(s, "D4")
        self.assertEqual(state["resign_proposal"], "W")
        self.assertEqual(len(state["moves"]), 1, "提认输那一手不该再落子")
        self.assertFalse(state["finished"], "还没结束，等人在答复")

    def test_accept_ends_the_game(self):
        s = make_session(["Q16"])
        self._drive_to_the_edge(s)
        play(s, "D4")
        state = s.accept_resign()
        self.assertIsNone(state["resign_proposal"])
        self.assertTrue(state["finished"])
        self.assertEqual(s.result()["result"], "B+R")   # 白（电脑）认输

    def test_decline_keeps_playing_and_mutes_later_offers(self):
        s = make_session(["Q16", "Q4"])
        self._drive_to_the_edge(s)
        play(s, "D4")
        state = s.decline_resign()
        self.assertIsNone(state["resign_proposal"])
        self.assertTrue(s.resign_disabled)
        self.assertEqual(len(state["moves"]), 2, "不接受就得真接着下")

    def test_answering_without_a_proposal_raises(self):
        s = make_session([])
        with self.assertRaises(IllegalMove):
            s.accept_resign()
        with self.assertRaises(IllegalMove):
            s.decline_resign()

    def test_new_game_clears_the_mute(self):
        s = make_session(["Q16"])
        self._drive_to_the_edge(s)
        play(s, "D4")
        s.decline_resign()
        state = s.new_game()
        self.assertFalse(state["resign_proposal"])
        self.assertFalse(s.resign_disabled, "新的一盘该重新开始算")


class TestHint(unittest.TestCase):
    def test_hint_does_not_play_and_puts_the_level_back(self):
        s = make_session([])
        s.levels = {"B": "3k", "W": "3k"}
        s.engine.set_level("3k")
        r = s.hint()
        self.assertEqual(r["vertex"], "Q16")
        self.assertEqual(r["level"], server.HINT_LEVEL)
        self.assertEqual(len(s.game.moves), 0, "支招不能真落子")
        self.assertEqual(s.engine.level, "3k", "问完要把档位拨回来")

    def test_hint_only_on_your_turn(self):
        s = make_session(["Q16"])
        s.human_move("D4")                      # 轮到电脑了
        with self.assertRaises(IllegalMove):
            s.hint()

    def test_hint_after_the_game_ends_raises(self):
        s = make_session([])
        s.resign()
        with self.assertRaises(IllegalMove):
            s.hint()


class TestTakeover(unittest.TestCase):
    """机器替我走：临时接管我这一方，走满 N 手再还给我。"""

    # 够走满 5 手接管的棋（我 5 手 + 对手 5 手），互不重叠
    SCRIPT = ["Q16", "D4", "Q4", "D16", "R5", "C6", "C14", "R14", "K10", "Q10"]

    def test_takeover_plays_five_of_my_moves_then_hands_back(self):
        s = make_session(self.SCRIPT)
        state = s.takeover(5)
        for _ in range(4):              # 第一手在上面的 takeover(5) 里已经走了
            state = s.takeover()

        self.assertEqual(state["takeover_left"], 0)
        self.assertEqual(state["takeover_count"], 5)
        self.assertEqual(state["to_move"], "B", "走完该还给我")
        self.assertEqual([m["vertex"] for m in state["moves"] if m["color"] == "B"],
                         self.SCRIPT[0::2])

    def test_takeover_plays_my_moves_at_nine_dan(self):
        """替我走的每一手用 9 段，对手的应手仍用它自己的档位。"""
        s = make_session(self.SCRIPT)
        s.takeover(5)
        self.assertEqual([p for c, p in s.engine.genmoves if c == "B"], ["rank_9d"])
        self.assertEqual([p for c, p in s.engine.genmoves if c == "W"], ["rank_3k"])

    def test_takeover_never_resigns_for_me(self):
        """引擎替我走时说 resign，不能变成「我认输」，当停一手。"""
        s = make_session(["resign", "Q16"])
        state = s.takeover(5)
        self.assertIsNone(state["resign_proposal"])
        self.assertEqual(state["moves"][0]["color"], "B")
        self.assertEqual(state["moves"][0]["vertex"], "pass")

    def test_takeover_moves_dont_feed_the_resign_streak(self):
        """替我走的那手报的是我的胜率，不能拿去算「对手该不该认输」。"""
        s = make_session(self.SCRIPT)
        s.engine.winrate, s.engine.lead = 0.01, -30.0
        s.takeover(5)
        # 对手只应了一手，连续计数就只该加 1；我那一手要是也算了就是 2
        self.assertEqual(s.resign_streak, 1)

    def test_my_own_move_cancels_the_takeover(self):
        s = make_session(self.SCRIPT)
        self.assertEqual(s.takeover(5)["takeover_left"], 4)
        state = s.human_move("K10")
        self.assertEqual(state["takeover_left"], 0)
        with self.assertRaises(IllegalMove):
            s.takeover()

    def test_undo_cancels_the_takeover(self):
        s = make_session(self.SCRIPT)
        s.takeover(5)
        self.assertEqual(s.undo()["takeover_left"], 0)

    def test_only_five_or_ten_moves(self):
        s = make_session(self.SCRIPT)
        for bad in (0, 3, 7, 20):
            with self.assertRaises(IllegalMove):
                s.takeover(bad)

    def test_continuing_without_a_takeover_raises(self):
        s = make_session(self.SCRIPT)
        with self.assertRaises(IllegalMove):
            s.takeover()

    def test_takeover_only_on_your_turn(self):
        s = make_session(self.SCRIPT)
        s.human_move("D4")                      # 轮到电脑了
        with self.assertRaises(IllegalMove):
            s.takeover(5)

    def test_takeover_after_the_game_ends_raises(self):
        s = make_session(self.SCRIPT)
        s.resign()
        with self.assertRaises(IllegalMove):
            s.takeover(5)

    def test_new_game_forgets_the_takeover(self):
        s = make_session(self.SCRIPT)
        s.takeover(5)
        state = s.new_game()
        self.assertEqual(state["takeover_left"], 0)
        self.assertEqual(state["takeover_count"], 0)


class TestHumanColor(unittest.TestCase):
    def test_new_game_can_take_white(self):
        s = make_session(["Q16"])
        state = s.new_game(human_color="W")
        self.assertEqual(state["human_color"], "W")
        self.assertEqual(state["moves"][0]["color"], "B", "电脑执黑先走")
        self.assertEqual(state["to_move"], "W")

    def test_bogus_color_is_ignored(self):
        s = make_session([])
        self.assertEqual(s.new_game(human_color="X")["human_color"], "B")


class TestHistory(unittest.TestCase):
    """对弈记录：下完的盘自动记一笔，没下完的不记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = server.GAMES
        server.GAMES = Path(self.tmp.name)

    def tearDown(self):
        server.GAMES = self._orig
        self.tmp.cleanup()

    def test_unfinished_game_is_not_recorded(self):
        s = make_session(["Q16"])
        play(s, "D4")
        self.assertEqual(server.Session.history()["total"], 0)

    def test_resigning_is_recorded_with_every_field(self):
        s = make_session([])
        s.new_game(level="2k")
        s.resign()
        h = server.Session.history()
        self.assertEqual(h["total"], 1)
        r = h["records"][0]
        self.assertEqual(r["level"], "2k")
        self.assertEqual(r["human_color"], "B")
        self.assertFalse(r["human_won"])              # 你自己认输 = 输
        self.assertEqual(r["result"], "W+R")
        self.assertIsNone(r["human_margin"], "认输的盘没有目数")
        self.assertIsInstance(r["seconds"], int)
        self.assertEqual(r["moves"], 0)

    def test_only_recorded_once(self):
        """认输之后再点一次，不能又记一笔。"""
        s = make_session([])
        s.resign()
        s.resign()
        self.assertEqual(server.Session.history()["total"], 1)

    def test_takeover_is_counted(self):
        s = make_session(["Q16", "D4"])
        s.takeover(5)          # 机器替我走了一手（我 B，对手 W）
        s.resign()
        r = server.Session.history()["records"][0]
        self.assertEqual(r["takeover"], 1)

    def test_undo_and_hint_are_counted(self):
        s = make_session(["Q16", "Q4", "D16"])
        play(s, "D4")
        s.undo()
        s.hint()
        s.resign()
        r = server.Session.history()["records"][0]
        self.assertEqual(r["undos"], 1)
        self.assertEqual(r["hints"], 1)

    def test_new_game_resets_the_counters(self):
        s = make_session(["Q16"])
        play(s, "D4")
        s.undo()
        s.new_game()
        s.resign()
        self.assertEqual(server.Session.history()["records"][0]["undos"], 0)

    def test_margin_is_from_the_human_side(self):
        """人执白时目差要翻过来 —— 不然赢了会显示成输。"""
        wall = ["A1", "A2", "A3", "A4", "A5", "B5", "C5", "D5", "E5", "E1", "E2", "E3", "E4"]
        s = make_session([])
        s.new_game(human_color="W")
        for v in wall:                      # 黑围了个角，白一子没有 -> 黑大胜
            s.game.board[rules.vertex_to_index(v, 19)] = rules.BLACK
        s.game.finished = True
        s._record_result()

        r = server.Session.history()["records"][0]
        self.assertEqual(r["human_color"], "W")
        self.assertLess(r["human_margin"], 0, "黑占优，人执白就是落后")
        self.assertFalse(r["human_won"])

    def test_newest_first(self):
        for level in ("1k", "2k"):
            s = make_session([])
            s.new_game(level=level)
            s.resign()
        self.assertEqual([r["level"] for r in server.Session.history()["records"]],
                         ["2k", "1k"])

    def test_history_file_is_not_mistaken_for_a_saved_game(self):
        """history.jsonl 不能被 list_games() 的 *.json 通配扫进去。"""
        s = make_session([])
        s.resign()
        self.assertEqual(server.Session.list_games(), [])

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        server.GAMES.mkdir(exist_ok=True)
        server.history_path().write_text(
            '{"level": 1}\n这不是 json\n{"level": 2}\n', encoding="utf-8")
        self.assertEqual([r["level"] for r in server.Session.history()["records"]], [2, 1])


class TestPlatform(unittest.TestCase):
    """跨平台那点事。本机是 Windows，所以这里测的是「另一条路也走得通」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = katago.VENDOR
        self._orig_config_dir = katago.CONFIG_DIR
        self.real_config_dir = katago.CONFIG_DIR
        katago.VENDOR = Path(self.tmp.name)
        # 配置有两处可找（vendor 和仓库自带的 configs/），两处都得隔离 ——
        # 不隔离的话真仓库里那份会兜底，「找不到要报错」那条就永远测不出问题。
        katago.CONFIG_DIR = Path(self.tmp.name) / "configs"
        self.fake_katago = Path(self.tmp.name) / "katago"
        self.fake_katago.write_text("", encoding="utf-8")

    def tearDown(self):
        katago.VENDOR = self._orig
        katago.CONFIG_DIR = self._orig_config_dir
        self.tmp.cleanup()

    def _make_mac_folder(self, with_config=True):
        folder = katago.VENDOR / "engines" / "macos"
        folder.mkdir(parents=True, exist_ok=True)
        if with_config:
            (folder / katago.CONFIG_NAME).write_text("", encoding="utf-8")
        return folder

    def test_falls_back_to_path_when_vendor_has_no_binary(self):
        """Mac 上的 katago 是 brew 装的，只在 PATH 上，vendor 里没有。"""
        self._make_mac_folder()
        with mock.patch.object(katago.shutil, "which", return_value=str(self.fake_katago)):
            exe, config = katago.engine_paths("macos")
        self.assertEqual(exe, self.fake_katago)
        self.assertEqual(config.name, katago.CONFIG_NAME)

    def test_vendor_wins_over_path(self):
        """两边都有时用 vendor 里的那份，别被 PATH 上别的版本抢走。"""
        folder = self._make_mac_folder()
        (folder / "katago").write_text("", encoding="utf-8")
        with mock.patch.object(katago.shutil, "which", return_value="/elsewhere/katago"):
            exe, _ = katago.engine_paths("macos")
        self.assertEqual(exe, folder / "katago")

    def test_complains_when_the_config_is_missing(self):
        """两处都没有才报错 —— brew 那个包只给一个二进制，不带 cfg；
        仓库那份要是也丢了，就真没处拿了。"""
        self._make_mac_folder(with_config=False)
        with mock.patch.object(katago.shutil, "which", return_value=str(self.fake_katago)):
            with self.assertRaises(FileNotFoundError):
                katago.engine_paths("macos")

    def test_config_falls_back_to_the_copy_shipped_with_the_source(self):
        """Mac 上没有引擎包，那份 cfg 就不在 vendor 里 —— 得退回 configs/。

        它随源码走，不再靠下载：raw.githubusercontent.com 国内经常连不上，
        而且它以前排在自然音后面，archive.org 一慢就永远轮不到它。"""
        self._make_mac_folder(with_config=False)
        shipped = katago.CONFIG_DIR
        shipped.mkdir(parents=True, exist_ok=True)
        (shipped / katago.CONFIG_NAME).write_text("", encoding="utf-8")
        with mock.patch.object(katago.shutil, "which", return_value=str(self.fake_katago)):
            _exe, config = katago.engine_paths("macos")
        self.assertEqual(config, shipped / katago.CONFIG_NAME)

    def test_vendor_config_wins_over_the_shipped_one(self):
        """Windows 解压引擎包会带出一份同名 cfg，那份跟引擎同版本，优先用。"""
        folder = self._make_mac_folder()
        shipped = katago.CONFIG_DIR
        shipped.mkdir(parents=True, exist_ok=True)
        (shipped / katago.CONFIG_NAME).write_text("", encoding="utf-8")
        with mock.patch.object(katago.shutil, "which", return_value=str(self.fake_katago)):
            _exe, config = katago.engine_paths("macos")
        self.assertEqual(config, folder / katago.CONFIG_NAME)

    def test_the_shipped_config_is_actually_in_the_repo(self):
        """上面那条回退只有在仓库里真有这份文件时才有意义。

        注意用的是 real_config_dir 而不是 katago.CONFIG_DIR —— 后者被
        setUp 指向临时目录了，测的是「有没有这个逻辑」，这条测的是
        「仓库里到底有没有这个文件」。"""
        self.assertTrue(
            (self.real_config_dir / katago.CONFIG_NAME).is_file(),
            f"{self.real_config_dir / katago.CONFIG_NAME} 不在仓库里 —— "
            "Mac 上没处拿这份配置")

    def test_unknown_engine_raises(self):
        with self.assertRaises(FileNotFoundError):
            katago.engine_paths("nope")

    def test_unknown_engine_is_named_in_the_error(self):
        """报错得说清是哪个后端，不然「找不到引擎」这四个字没法查。"""
        with self.assertRaises(FileNotFoundError) as caught:
            katago.engine_paths("nope")
        self.assertIn("nope", str(caught.exception))

    def test_data_dir_stays_in_the_project_when_run_from_source(self):
        with mock.patch.object(server, "IS_FROZEN", False):
            self.assertEqual(server._data_dir(), server.ROOT)

    def test_data_dir_on_windows_goes_to_localappdata(self):
        """打包成 exe 之后不能再往自己旁边写：程序可能被放在只读的地方，
        而且重新打包一次不该把之前下的棋一起冲掉。"""
        with mock.patch.object(server, "IS_FROZEN", True), \
                mock.patch.object(server.platform, "system", return_value="Windows"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            self.assertEqual(server._data_dir(),
                             Path(r"C:\Users\x\AppData\Local") / "Go")

    def test_data_dir_on_mac_goes_to_application_support(self):
        with mock.patch.object(server, "IS_FROZEN", True), \
                mock.patch.object(server.platform, "system", return_value="Darwin"):
            self.assertEqual(server._data_dir().parts[-3:],
                             ("Library", "Application Support", "Go"))

    def test_macos_entry_carries_no_windows_only_overrides(self):
        """DirectML 是 Windows 专属，macos 那项不能带 onnxProvider。"""
        self.assertEqual(katago.ENGINES["macos"]["exe"], "katago")
        self.assertEqual(katago.ENGINES["macos"]["overrides"], {})
        self.assertEqual(katago.ENGINES["directml"]["exe"], "katago.exe")

    def test_setup_never_downloads_a_macos_engine(self):
        """KataGo 官方不发 macOS 预编译包，Mac 上只能 brew 装，所以不下引擎包。
        配置也不再下载 —— 它随源码走，在 configs/ 里。"""
        subdirs = [d[3] for d in setup.REQUIRED + setup.OPTIONAL]
        self.assertNotIn("engines/macos", subdirs)
        self.assertFalse(hasattr(setup, "MAC_DOWNLOADS"),
                         "配置改成随源码发之后，不该再有 macOS 的下载项")
        self.assertEqual([d[3] for d in setup.WINDOWS_DOWNLOADS],
                         ["engines/directml", "engines/eigen"])

    def test_sounds_are_optional_and_in_their_own_batch(self):
        """自然音缺了只是没背景声，不该拦住安装。

        这是防回归：以前必需的和可选的混在一个 DOWNLOADS 里按顺序下，
        archive.org 一慢就是一个 SystemExit 把整个脚本停住 —— 前面下成功的
        白下，排在后面的必需项反而没轮到。Mac 上那个 cfg 就是这么丢的。
        现在两批分开，必需的先跑完。"""
        self.assertEqual(set(setup.SOUNDS), {d[1] for d in setup.OPTIONAL})
        self.assertFalse(set(setup.SOUNDS) & {d[1] for d in setup.REQUIRED},
                         "自然音不能混进必需项")

    def test_setup_downloads_the_two_engines_on_windows(self):
        self.assertEqual([d[3] for d in setup.WINDOWS_DOWNLOADS],
                         ["engines/directml", "engines/eigen"])

    def test_models_are_shared_by_both_platforms(self):
        self.assertEqual(len(setup.MODEL_DOWNLOADS), 2)
        for _url, _name, size, subdir in setup.MODEL_DOWNLOADS:
            self.assertEqual(subdir, "models")
            self.assertIsInstance(size, int, "模型的字节数是钉死的，下完要核对")

    def test_refuses_to_double_bind(self):
        """端口被占时必须报错，不能悄悄绑上去。

        http.server 默认开 SO_REUSEADDR，在 Windows 上它允许第二个进程绑同一个
        端口，然后连接全跑去找先绑的那个 —— 看着就像「改了代码重启了却不生效」。
        这个项目为它查过一次假故障，所以钉一条。
        """
        first = server.Server(("127.0.0.1", 0), server.Handler)
        try:
            port = first.server_address[1]      # 让系统挑个空闲端口，免得跟人撞
            with self.assertRaises(OSError):
                server.Server(("127.0.0.1", port), server.Handler)
        finally:
            first.server_close()

    def test_games_dir_follows_the_env_var(self):
        """打包成 .app 之后存档得挪到 bundle 外面 —— 那里面可能是只读的。"""
        here = str(Path(__file__).resolve().parent)
        env = dict(os.environ, GO_DATA_DIR=str(self.tmp.name))
        out = subprocess.run(
            [sys.executable, "-c", "import server; print(server.GAMES)"],
            capture_output=True, text=True, env=env, cwd=here,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().startswith(str(self.tmp.name)),
                        f"拿到的是 {out.stdout.strip()}")


class TestLevel(unittest.TestCase):
    def test_new_game_applies_level_to_both_sides(self):
        """人机模式只有引擎那边的档位有意义，但两边都设上，免得留下陈旧值 ——
        切到机机模式时黑方要是还揣着上一局的旧档位，那是很难查的一类怪事。"""
        s = make_session([])
        s.new_game(level="2k")
        self.assertEqual(s.levels, {"B": "2k", "W": "2k"})
        self.assertEqual(s.state()["levels"], {"B": "2k", "W": "2k"})

    def test_the_engine_plays_at_the_level_of_its_own_side(self):
        s = make_session(["Q16"])
        s.new_game(level="2k", human_color="B")     # 引擎执白
        s.human_move("D4")
        s.ai_move()
        self.assertEqual(s.engine.genmoves[-1], ("W", "rank_2k"))
        self.assertEqual(s.engine.level, "2k")

    def test_bogus_level_is_refused(self):
        s = make_session([])
        with self.assertRaises(IllegalMove):
            s.new_game(level="10d")


class TestSearchParams(unittest.TestCase):
    """档位 -> 搜索参数的表。

    低段位纯模仿（快，模仿低手本来就准）；高段位必须开搜索 —— 引擎自带的
    cfg 注释明说「高段/职业档位不能指望达到被模仿者的真实强度，因为模型不做
    搜索」。这张表就是速度与强度之间那个旋钮。
    """

    def test_all_eighteen_ranks_exist(self):
        self.assertEqual(
            katago.LEVELS,
            [f"{n}k" for n in range(9, 0, -1)] + [f"{n}d" for n in range(1, 10)],
        )

    def test_kyu_plays_pure_imitation(self):
        """级位是纯模仿：不纠偏、不额外探索、按温度采样 —— 跟改造前一致。"""
        p = katago.search_params("9k")
        self.assertEqual(p["humanSLChosenMovePiklLambda"], 100000000)
        self.assertEqual(p["humanSLRootExploreProbWeightless"], 0.0)

    def test_dan_turns_the_search_on(self):
        """段位要把纠偏打开，否则搜了也白搜。"""
        p = katago.search_params("9d")
        self.assertLess(p["humanSLChosenMovePiklLambda"], 1.0)
        self.assertGreater(p["humanSLRootExploreProbWeightless"], 0.0)

    def test_visits_grow_with_dan(self):
        visits = [katago.search_params(f"{n}d")["maxVisits"] for n in range(1, 10)]
        self.assertEqual(visits, sorted(visits), "档位越高，搜索量不该反而变少")
        self.assertGreater(visits[-1], visits[0], "最高的段位必须比最低的段位搜得多")

    def test_every_level_resolves(self):
        for lv in katago.LEVELS:
            self.assertTrue(katago.search_params(lv), f"{lv} 没给出参数")


class RecordingEngine:
    """只有 cmd 的假引擎，用来核对 set_level 到底发了哪些命令。

    set_profile 直接借真的那一层薄封装 —— 这里要验的是 set_level，
    不该把 set_profile 的行为在测试里再抄一遍。
    """

    def __init__(self):
        self.commands = []

    def cmd(self, command):
        self.commands.append(command)
        return ""

    set_profile = katago.KataGo.set_profile


class TestSetLevel(unittest.TestCase):
    def test_sends_profile_and_search_params_together(self):
        """切档位不等于只换个模仿对象 —— 搜索参数得跟着一起切。

        只切档案的话，9 级会被 9 段那套参数带着跑（或者反过来），
        「分级加搜索」这个设计就落空了。
        """
        rec = RecordingEngine()
        katago.KataGo.set_level(rec, "9d")
        joined = " | ".join(rec.commands)
        self.assertIn("humanSLProfile rank_9d", joined)
        for key, value in katago.search_params("9d").items():
            self.assertIn(f"{key} {value}", joined)

    def test_switching_back_down_restores_the_cheap_params(self):
        """来回切也得回到低档那套，否则问完一手 9 段的棋，9 级再也快不起来。"""
        rec = RecordingEngine()
        katago.KataGo.set_level(rec, "9k")
        joined = " | ".join(rec.commands)
        self.assertIn("humanSLProfile rank_9k", joined)
        self.assertIn("maxVisits 40", joined)
        self.assertIn("humanSLChosenMovePiklLambda 100000000", joined)

    def test_returns_the_level(self):
        rec = RecordingEngine()
        self.assertEqual(katago.KataGo.set_level(rec, "5k"), "5k")


class TestMachinePlay(unittest.TestCase):
    """两个机器对下：黑白各按自己选的档位出招。

    入口仍是 ai_move()，跟人机模式同一个 —— 前端那条路不用分叉。
    """

    def test_each_side_plays_at_its_own_level(self):
        s = make_session(["Q16", "D4"])
        s.new_game(levels={"B": "9k", "W": "9d"}, machine_play=True)
        s.ai_move()
        s.ai_move()
        self.assertEqual(s.engine.genmoves, [("B", "rank_9k"), ("W", "rank_9d")])

    def test_one_call_plays_exactly_one_move(self):
        """一请求一手 —— 前端靠这个把每一步画出来，也才停得下来。"""
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        state = s.ai_move()
        self.assertEqual(len(state["moves"]), 1)
        state = s.ai_move()
        self.assertEqual(len(state["moves"]), 2)

    def test_level_change_takes_effect_on_the_next_move(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.levels["W"] = "1d"
        s.ai_move()
        self.assertEqual(s.engine.genmoves[-1], ("W", "rank_1d"))

    def test_new_game_takes_both_levels_and_the_mode(self):
        s = make_session([])
        s.new_game(levels={"B": "3k", "W": "7d"}, machine_play=True)
        self.assertEqual(s.levels, {"B": "3k", "W": "7d"})
        self.assertTrue(s.machine_play)

    def test_new_game_does_not_play_by_itself(self):
        """开局不自动落子 —— 人得先看见空盘，点了「开始」才走。"""
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        self.assertEqual(s.state()["moves"], [])

    def test_machine_never_resigns(self):
        """自对弈没人答复认输，所以引擎不许提 —— 让它老老实实下完。"""
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.engine.winrate, s.engine.lead = 0.001, -50.0
        for _ in range(6):
            s.ai_move()
        self.assertIsNone(s.resign_proposal)

    def test_human_move_is_refused_in_machine_mode(self):
        s = make_session([])
        s.new_game(machine_play=True)
        with self.assertRaises(IllegalMove):
            s.human_move("D4")

    def test_one_side_can_be_switched_back_to_human(self):
        """机机下到一半想接手，不该被迫开新局。"""
        s = make_session([])
        s.new_game(levels={"B": "9k", "W": "9d"}, machine_play=True)
        s.machine_play = False
        s.human_color = "B"
        self.assertFalse(s.state()["machine_play"])


class TestMoveEvals(unittest.TestCase):
    """每手的形势存下来，复盘时才能画胜率曲线、找出掉分的那一手。

    evals 跟 moves 一一对齐，evals[i] 说的是「第 i 手落下之前」的形势。
    落子前问的，所以是引擎出手那一刻报的那个数 —— 不额外花时间。
    """

    def test_machine_game_records_one_eval_per_move(self):
        s = make_session(["Q16", "D4", "Q4"])
        s.new_game(machine_play=True)
        for _ in range(3):
            s.ai_move()
        self.assertEqual(len(s.state()["evals"]), 3)

    def test_evals_are_all_from_blacks_point_of_view(self):
        """引擎报的是「你问的那个颜色」的胜率（实测：同一局面问黑 0.058、
        问白 0.991），而曲线只能有一条 —— 统一翻成黑方视角，否则会锯齿乱跳。
        """
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.engine.winrate_by_color = {"B": 0.4, "W": 0.4}
        s.ai_move()                      # 黑走，报的就是黑方 40%
        s.ai_move()                      # 白走，报白方 40% → 黑方视角 60%
        evals = s.state()["evals"]
        self.assertAlmostEqual(evals[0]["winrate"], 0.4)
        self.assertAlmostEqual(evals[1]["winrate"], 0.6)

    def test_lead_flips_too(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.engine.lead_by_color = {"B": 5.0, "W": 5.0}
        s.ai_move()
        s.ai_move()
        evals = s.state()["evals"]
        self.assertAlmostEqual(evals[0]["lead"], 5.0)
        self.assertAlmostEqual(evals[1]["lead"], -5.0)

    def test_missing_numbers_stay_none(self):
        """引擎没报数就存 None —— 宁可曲线上断一段，也别编个 0 出来。"""
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        s.engine.winrate = None
        s.ai_move()
        self.assertIsNone(s.state()["evals"][0])

    def test_human_moves_keep_the_list_aligned(self):
        """人机模式下人那一手没有形势可记（引擎当时没在算），留 None 占位 ——
        空过去会让曲线整体错位一手，比断一段还糟。
        """
        s = make_session(["Q16"])
        s.new_game(level="3k")           # 人执黑
        s.human_move("D4")
        s.ai_move()
        evals = s.state()["evals"]
        self.assertEqual(len(evals), 2)
        self.assertIsNone(evals[0])
        self.assertIsNotNone(evals[1])

    def test_undo_drops_the_evals_too(self):
        s = make_session(["Q16", "Q4", "R5", "C6"])
        s.new_game(level="3k")
        play(s, "D4")
        play(s, "D16")
        s.undo()
        state = s.state()
        self.assertEqual(len(state["evals"]), len(state["moves"]))

    def test_new_game_clears_them(self):
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.new_game(machine_play=True)
        self.assertEqual(s.state()["evals"], [])

    def test_survives_a_save_and_load(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.ai_move()
        gid = s.save("测试")["id"]

        s2 = make_session([])
        s2.load(gid)
        self.assertEqual(len(s2.state()["evals"]), 2)


class TestReviewHint(unittest.TestCase):
    """复盘时停在某一手，问引擎「这里该下哪」。

    跟普通支招不是一回事：支招问的是「现在」，复盘问的是「当时」——
    所以得先把引擎的棋盘重放到那一步，问完再拉回来。
    """

    def test_asks_for_the_side_that_was_to_move(self):
        s = make_session(["Q16", "D4", "Q4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.ai_move()                      # 黑 Q16、白 D4
        s.review_hint(0)                 # 第 0 手之前 = 空盘，轮到黑
        self.assertEqual(s.engine.searched, ["B"])
        s.review_hint(1)                 # 第 1 手之前 = 黑已落一子，轮到白
        self.assertEqual(s.engine.searched, ["B", "W"])

    def test_does_not_touch_the_game(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.ai_move()
        before = list(s.state()["moves"])
        s.review_hint(0)
        self.assertEqual(s.state()["moves"], before)

    def test_engine_board_is_put_back_afterwards(self):
        """问完必须把引擎拉回当前局面，否则下一手会走在错的盘上。"""
        s = make_session(["Q16", "D4", "Q4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.ai_move()
        s.review_hint(1)
        live = [(m["color"], m["vertex"]) for m in s.state()["moves"]]
        self.assertEqual(s.engine.played, live)

    def test_asking_past_the_end_is_refused(self):
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        s.ai_move()
        with self.assertRaises(IllegalMove):
            s.review_hint(5)

    def test_at_the_end_it_asks_whoever_is_to_move_now(self):
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        s.ai_move()                      # 黑走了，轮到白
        s.review_hint(1)
        self.assertEqual(s.engine.searched, ["W"])

    def test_eval_is_from_blacks_point_of_view(self):
        """曲线是黑方视角的，复盘问出来的形势也得是同一个视角。"""
        s = make_session(["Q16"])
        s.new_game(machine_play=True)
        s.ai_move()
        self.assertAlmostEqual(s.review_hint(0)["eval"]["winrate"], 0.7)


class TestMachineGamePersistence(unittest.TestCase):
    """机机对局存档、对弈记录，以及老存档的兼容。"""

    def test_history_records_both_levels_and_the_winner(self):
        s = make_session(["Q16", "D4", "pass", "pass"])
        s.new_game(levels={"B": "9k", "W": "9d"}, machine_play=True)
        for _ in range(4):
            s.ai_move()
        r = server.Session.history()["records"][0]
        self.assertTrue(r["machine_play"])
        self.assertEqual(r["levels"], {"B": "9k", "W": "9d"})
        self.assertIsNone(r["human_won"], "机机对局没有「人」赢没赢这回事")
        self.assertTrue(r["result"].startswith(r["winner"]))
        # 目数按黑方视角记（正的=黑领先），跟人机模式那个「人的视角」不一样
        self.assertIsInstance(r["margin"], float)
        self.assertEqual(r["winner"], "B" if r["margin"] > 0 else "W")

    def test_machine_game_is_recorded_only_once(self):
        """history 是整个模块累计的，所以比增量，不比总数。"""
        before = server.Session.history()["total"]
        s = make_session(["pass", "pass"])
        s.new_game(machine_play=True)
        for _ in range(6):          # 两下停一手就收局了，后面几下是空转
            s.ai_move()
        self.assertEqual(server.Session.history()["total"] - before, 1)

    def test_levels_and_mode_survive_a_save_and_load(self):
        s = make_session(["Q16"])
        s.new_game(levels={"B": "9k", "W": "9d"}, machine_play=True)
        s.ai_move()
        gid = s.save("机机")["id"]

        s2 = make_session([])
        state = s2.load(gid)
        self.assertTrue(state["machine_play"])
        self.assertEqual(state["levels"], {"B": "9k", "W": "9d"})

    def test_old_saves_with_an_integer_level_still_open(self):
        """这个功能之前存的棋谱里 level 是个整数，不能让它们打不开。

        games/ 里真有几个这样的存档，是实测过的数据形状。
        """
        server.GAMES.mkdir(exist_ok=True)
        old = server.GAMES / "old-format.json"
        old.write_text(json.dumps({
            "name": "老棋谱", "level": 4, "human_color": "W",
            "moves": [{"n": 1, "color": "B", "vertex": "Q16", "captures": []}],
        }, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(old.unlink)

        s = make_session([])
        state = s.load("old-format")
        self.assertEqual(state["levels"], {"B": "4k", "W": "4k"})
        self.assertFalse(state["machine_play"])
        self.assertEqual(state["evals"], [None], "老存档没有形势，补 None 对齐棋盘")


class TestSwitchingModeMidGame(unittest.TestCase):
    """对局中途换模式：棋必须留着 —— 这是这个功能存在的唯一理由。"""

    def test_the_game_survives_the_switch(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()                                  # 黑走一手，轮到白
        before = [m["vertex"] for m in s.state()["moves"]]
        state = s.switch_mode(False)
        self.assertEqual([m["vertex"] for m in state["moves"]], before)
        self.assertFalse(state["machine_play"])

    def test_the_curve_survives_too(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()
        s.ai_move()
        state = s.switch_mode(False)
        self.assertEqual(len(state["evals"]), len(state["moves"]))

    def test_the_human_takes_whichever_side_is_to_move(self):
        s = make_session(["Q16", "D4"])
        s.new_game(machine_play=True)
        s.ai_move()                                  # 黑刚走完，轮到白
        self.assertEqual(s.state()["to_move"], "W")
        self.assertEqual(s.switch_mode(False)["human_color"], "W")

    def test_switching_to_machine_takes_both_sides(self):
        s = make_session(["Q16", "D4"])
        s.new_game(level="3k", human_color="B")
        s.human_move("D4")
        before = len(s.state()["moves"])
        state = s.switch_mode(True, levels={"B": "9k", "W": "5d"})
        self.assertTrue(state["machine_play"])
        self.assertEqual(state["levels"], {"B": "9k", "W": "5d"})
        self.assertEqual(len(state["moves"]), before, "换模式不该顺手替谁走一手")

    def test_levels_can_be_left_alone(self):
        s = make_session()
        s.new_game(levels={"B": "9k", "W": "5d"}, machine_play=True)
        self.assertEqual(s.switch_mode(False)["levels"], {"B": "9k", "W": "5d"})

    def test_an_explicit_color_is_honoured(self):
        """设置卡里点「我执白」走这条 —— 不是轮谁接谁，是指定。"""
        s = make_session(["Q16"])
        s.new_game(level="3k", human_color="B")
        s.human_move("D4")                           # 轮到白（引擎）
        state = s.switch_mode(False, human_color="W")
        self.assertEqual(state["human_color"], "W")
        self.assertEqual(state["to_move"], "W", "该人走了，引擎不该抢着下")

    def test_taking_the_side_that_is_not_to_move_lets_the_engine_catch_up(self):
        s = make_session(["Q16", "D4"])
        s.new_game(level="3k", human_color="W")      # 引擎执黑先走
        self.assertEqual(s.state()["to_move"], "W")
        state = s.switch_mode(False, human_color="B")   # 改成人执黑
        self.assertEqual(state["human_color"], "B")
        self.assertEqual(len(state["moves"]), 2, "引擎得把白那一手补上")
        self.assertEqual(state["to_move"], "B", "补完该轮到人了")

    def test_a_bogus_level_is_refused(self):
        s = make_session()
        s.new_game(level="3k")
        with self.assertRaises(IllegalMove):
            s.switch_mode(True, levels={"B": "10d", "W": "3k"})

    def test_refuses_once_the_game_is_over(self):
        s = make_session()
        s.new_game(level="3k")
        s.resign()
        with self.assertRaises(IllegalMove):
            s.switch_mode(True)

    def test_a_pending_resign_offer_is_dropped(self):
        """机机模式没人答复认输，切过去时那个提案得清掉，不然是个死提案。"""
        s = make_session()
        s.new_game(level="3k", human_color="B")
        s.resign_proposal = "W"
        self.assertIsNone(s.switch_mode(True)["resign_proposal"])


class TestPlayingOnFromAReviewPly(unittest.TestCase):
    """复盘停在某一手，从这里接着当实战下。"""

    def _four_moves(self):
        s = make_session(["Q16", "R14"])
        s.new_game(level="3k", human_color="B")
        play(s, "D4")            # 2 手
        play(s, "C16")           # 4 手
        return s

    def test_truncates_moves_and_curve_together(self):
        s = self._four_moves()
        state = s.play_from(2)
        self.assertEqual(len(state["moves"]), 2)
        self.assertEqual(len(state["evals"]), 2, "曲线得跟着截，不然跟走子表错位")

    def test_the_human_takes_the_side_that_was_to_move(self):
        s = self._four_moves()
        state = s.play_from(2)
        self.assertFalse(state["machine_play"])
        self.assertEqual(state["human_color"], state["to_move"])

    def test_saves_as_a_new_game_so_the_original_survives(self):
        s = self._four_moves()
        info = s.save("原局")
        s.play_from(2)
        self.assertIsNone(s.state()["game_id"], "接着下的是一盘新棋")
        self.assertNotEqual(s.save("")["id"], info["id"])
        payload = json.loads((server.GAMES / f"{info['id']}.json").read_text("utf-8"))
        self.assertEqual(len(payload["moves"]), 4, "原存档一个字都不该动")

    def test_the_name_says_it_is_a_continuation(self):
        s = self._four_moves()
        s.save("原局")
        s.play_from(2)
        self.assertEqual(s.state()["game_name"], "原局 接续")

    def test_reopens_a_finished_game(self):
        """已经下完的棋也能从中间救活 —— 复盘接管的常见场景。"""
        s = self._four_moves()
        s.resign()
        self.assertTrue(s.state()["resigned"])
        state = s.play_from(2)
        self.assertIsNone(state["resigned"])
        self.assertFalse(state["finished"])
        self.assertEqual(len(state["moves"]), 2)

    def test_the_continued_game_is_recorded_on_its_own(self):
        before = len(server.load_history())
        s = self._four_moves()
        s.resign()                       # 第一笔
        s.play_from(2)
        s.resign()                       # 接续的那盘另记一笔
        self.assertEqual(len(server.load_history()), before + 2)

    def test_can_keep_playing_afterwards(self):
        s = self._four_moves()
        state = s.play_from(2)
        free = next(i for i, c in enumerate(state["board"]) if c == 0)
        state = play(s, rules.index_to_vertex(free, 19))
        self.assertEqual(len(state["moves"]), 4)
        self.assertEqual(len(state["evals"]), 4)

    def test_refuses_the_end_and_beyond(self):
        """停在最后一手之后没有「接着下」可言 —— 那儿就是当前局面。"""
        s = self._four_moves()
        for ply in (-1, 4, 99):
            with self.assertRaises(IllegalMove):
                s.play_from(ply)

    def test_a_fresh_game_has_nothing_to_replay(self):
        s = make_session()
        s.new_game(level="3k")
        with self.assertRaises(IllegalMove):
            s.play_from(0)


class TestNatureFolder(unittest.TestCase):
    """nature/：列清单 + 按名字取文件。

    nature_file() 是全程序唯一「用户给的字符串会变成文件路径」的地方，
    所以穿越那几条是重点，不是凑数。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = server.NATURE
        server.NATURE = Path(self.tmp.name) / "nature"
        self.addCleanup(self._restore)

    def _restore(self):
        server.NATURE = self._orig
        self.tmp.cleanup()

    def _touch(self, name):
        path = server.NATURE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00" * 8)
        return path

    def test_empty_folder_lists_nothing_but_still_reports_where_it_is(self):
        got = server.list_nature()
        self.assertEqual(got["files"], [])
        self.assertEqual(got["dir"], str(server.NATURE))

    def test_creates_the_folder_so_there_is_somewhere_to_drop_files(self):
        self.assertFalse(server.NATURE.exists())
        server.list_nature()
        self.assertTrue(server.NATURE.is_dir())

    def test_lists_audio_and_ignores_everything_else(self):
        self._touch("b.FLAC")
        self._touch("a.mp3")
        self._touch("cover.jpg")
        self._touch("readme.txt")
        self._touch("sub/nested.mp3")                    # 只认一层，不递归
        self.assertEqual(server.list_nature()["files"], ["a.mp3", "b.FLAC"])

    def test_finds_a_real_file(self):
        self._touch("a.mp3")
        self.assertEqual(server.nature_file("a.mp3"), server.NATURE / "a.mp3")

    def test_refuses_to_leave_the_folder(self):
        self._touch("a.mp3")
        for bad in ("../server.py", "..\\server.py", "sub/a.mp3", "sub\\a.mp3",
                    "./a.mp3", "/etc/passwd", "C:/Windows/win.ini", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(server.nature_file(bad))

    def test_survives_a_nul_byte(self):
        """文件名里有 NUL 会让 is_file() 抛 ValueError，不能漏出去。"""
        self.assertIsNone(server.nature_file("a\0.mp3"))

    def test_refuses_a_file_in_the_folder_that_is_not_audio(self):
        self._touch("secret.txt")
        self.assertIsNone(server.nature_file("secret.txt"))

    def test_refuses_a_file_that_is_not_there(self):
        self.assertIsNone(server.nature_file("nope.mp3"))

    def test_follows_the_env_var(self):
        """跟存档一样：打包之后声音得在 bundle 外面，那里面可能是只读的。"""
        here = str(Path(__file__).resolve().parent)
        env = dict(os.environ, GO_DATA_DIR=str(self.tmp.name))
        out = subprocess.run(
            [sys.executable, "-c", "import server; print(server.NATURE)"],
            capture_output=True, text=True, env=env, cwd=here,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().startswith(str(self.tmp.name)),
                        f"拿到的是 {out.stdout.strip()}")


class TestSeedNature(unittest.TestCase):
    """第一次运行时把内置自然音铺进 nature/。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (server.NATURE, server.BUILTIN_SOUNDS)
        root = Path(self.tmp.name)
        server.NATURE = root / "nature"
        server.BUILTIN_SOUNDS = root / "vendor" / "sounds"
        self.addCleanup(self._restore)

    def _restore(self):
        server.NATURE, server.BUILTIN_SOUNDS = self._orig
        self.tmp.cleanup()

    def _builtin(self, name, data=b"ID3fake"):
        server.BUILTIN_SOUNDS.mkdir(parents=True, exist_ok=True)
        (server.BUILTIN_SOUNDS / name).write_bytes(data)

    def test_copies_the_builtins_in(self):
        self._builtin("雨.mp3")
        self._builtin("海浪.mp3")
        server.seed_nature()
        self.assertEqual(sorted(p.name for p in server.NATURE.iterdir()),
                         ["海浪.mp3", "雨.mp3"])
        self.assertEqual((server.NATURE / "雨.mp3").read_bytes(), b"ID3fake")

    def test_leaves_an_existing_folder_alone(self):
        """用户放过了就不要再塞 —— 他可能是故意不要内置那几段的。"""
        server.NATURE.mkdir(parents=True)
        (server.NATURE / "我自己录的.mp3").write_bytes(b"mine")
        self._builtin("雨.mp3")
        server.seed_nature()
        self.assertEqual([p.name for p in server.NATURE.iterdir()],
                         ["我自己录的.mp3"])

    def test_is_a_noop_the_second_time(self):
        self._builtin("雨.mp3")
        server.seed_nature()
        (server.NATURE / "雨.mp3").unlink()      # 用户删了内置的那段
        server.seed_nature()
        self.assertEqual(list(server.NATURE.iterdir()), [])

    def test_does_nothing_when_setup_has_not_run(self):
        """vendor/sounds 不存在（还没跑 setup.py）不该炸，也不该凭空建个空目录。"""
        server.seed_nature()
        self.assertFalse(server.NATURE.exists())

    def test_ignores_non_audio_in_the_builtin_folder(self):
        self._builtin("雨.mp3")
        self._builtin("LICENSE.txt", b"text")
        server.seed_nature()
        self.assertEqual([p.name for p in server.NATURE.iterdir()], ["雨.mp3"])


class TestSoundDownloads(unittest.TestCase):
    """setup.py 里内置自然音那张表。"""

    def test_three_sounds_go_into_vendor_sounds(self):
        self.assertEqual([d[3] for d in setup.SOUND_DOWNLOADS], ["sounds"] * 3)

    def test_all_are_mp3_not_ogg(self):
        """Safari 不支持 Ogg Vorbis，而这个项目有 macOS 版 —— 只能发 MP3。"""
        for _url, name, _size, _sub in setup.SOUND_DOWNLOADS:
            with self.subTest(name=name):
                self.assertTrue(name.endswith(".mp3"), name)

    def test_sizes_are_pinned(self):
        """跟模型一样钉死字节数：下了一半的文件必须能被认出来。"""
        for _url, name, size, _sub in setup.SOUND_DOWNLOADS:
            with self.subTest(name=name):
                self.assertIsInstance(size, int)

    def test_they_ride_along_on_both_platforms(self):
        """自然音不是平台相关的 —— Windows 和 Mac 都要有。"""
        self.assertTrue(set(setup.SOUNDS) <= {d[1] for d in setup.OPTIONAL})


class TestAudioPage(unittest.TestCase):
    """背景音那张卡片的接线。

    这条测试只能静态地看 —— 没有浏览器就没法知道声音对不对。但它挡得住
    最烦人的那类坏法：改了 id 忘了同步，界面上什么都不报，就是按钮没反应。
    """

    def test_every_id_audio_js_reaches_for_exists_in_the_page(self):
        html = (server.WEB / "index.html").read_text(encoding="utf-8")
        js = (server.WEB / "audio.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'\$\("([^"]+)"\)', js))
        self.assertTrue(ids, "一个 id 都没找到，是不是改写法了")
        for i in sorted(ids):
            with self.subTest(id=i):
                self.assertIn(f'id="{i}"', html, f"audio.js 找 {i}，页面上没有")

    def test_the_page_loads_audio_js_after_app_js(self):
        """audio.js 用了 app.js 的 api() 和 toast()，顺序反了就是一片红。"""
        html = (server.WEB / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("/web/app.js"), html.index("/web/audio.js"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
