"""rules.py 的检查。标准库 unittest，不装 pytest。

重点是那些错了会静默毁掉一盘棋的地方：提子、自杀、劫、数子。
字符画里 X 是黑、O 是白、. 是空，第一行是最上面一行。
"""

import unittest

from rules import BLACK, EMPTY, WHITE, Game, IllegalMove, from_diagram, vertex_to_index as v2i


class TestCoordinates(unittest.TestCase):
    def test_roundtrip(self):
        from rules import index_to_vertex
        for v in ["A19", "T1", "D4", "Q16", "K10", "A1", "T19"]:
            self.assertEqual(index_to_vertex(v2i(v)), v)

    def test_letter_i_skipped(self):
        # GTP 坐标如果不跳 I，跟引擎对坐标时会整体错位一列
        self.assertRaises(ValueError, v2i, "I5")

    def test_corners(self):
        self.assertEqual(v2i("A19"), 0)      # 左上
        self.assertEqual(v2i("T1"), 360)     # 右下


class TestCapture(unittest.TestCase):
    def test_capture_single_stone(self):
        # 白 B2 只有 C2 一口气
        g = from_diagram("""
            . X .
            X O .
            . X .
        """, size=3, to_move="B")
        rec = g.play("B", "C2")
        self.assertEqual(rec["captures"], ["B2"])
        self.assertEqual(g.board[v2i("B2", 3)], EMPTY)
        self.assertEqual(g.captures["B"], 1)

    def test_capture_group(self):
        # 白 B3+C3 两子共享唯一一口气 D3
        g = from_diagram("""
            X X X .
            X O O .
            X X X .
            . . . .
        """, size=4, to_move="B")
        rec = g.play("B", "D3")
        self.assertEqual(sorted(rec["captures"]), ["B3", "C3"])
        self.assertEqual(g.captures["B"], 2)

    def test_corner_capture(self):
        # 白 A3 在角上，只有 B3 一口气
        g = from_diagram("""
            O . .
            X . .
            . . .
        """, size=3, to_move="B")
        rec = g.play("B", "B3")
        self.assertEqual(rec["captures"], ["A3"])

    def test_capture_beats_suicide(self):
        # 黑下 B2 时自己也没气，但提掉整圈白子后就有气了 —— 必须合法。
        # 这是“自杀”和“提子”最容易搞混的地方：先提子，再判气。
        g = from_diagram("""
            O O O
            O . O
            O O O
        """, size=3, to_move="B")
        rec = g.play("B", "B2")
        self.assertEqual(len(rec["captures"]), 8)
        self.assertEqual(g.board[v2i("B2", 3)], BLACK)


class TestIllegal(unittest.TestCase):
    def test_occupied(self):
        g = from_diagram(". . .\n. X .\n. . .", size=3, to_move="B")
        self.assertRaises(IllegalMove, g.play, "B", "B2")

    def test_suicide_single_stone(self):
        # 白圈把中心围死，但白圈自己有外气，所以黑下中心不提任何子 —— 纯自杀
        g = from_diagram("""
            . . . . .
            . O O O .
            . O . O .
            . O O O .
            . . . . .
        """, size=5, to_move="B")
        self.assertRaises(IllegalMove, g.play, "B", "C3")

    def test_suicide_multi_stone(self):
        # 落子后与周围黑棋连成一片，整片没气
        g = from_diagram("""
            X X X
            X . X
            X X X
        """, size=3, to_move="B")
        self.assertRaises(IllegalMove, g.play, "B", "B2")

    def test_illegal_move_leaves_board_untouched(self):
        g = from_diagram("X X X\nX . X\nX X X", size=3, to_move="B")
        snapshot = g.board[:]
        self.assertRaises(IllegalMove, g.play, "B", "B2")
        self.assertEqual(g.board, snapshot)
        self.assertEqual(g.moves, [])
        self.assertEqual(g.to_move, BLACK)

    def test_wrong_turn(self):
        g = Game(19)
        g.play("B", "D4")
        self.assertRaises(IllegalMove, g.play, "B", "Q16")

    def test_out_of_board(self):
        self.assertRaises(ValueError, v2i, "A20")
        self.assertRaises(ValueError, v2i, "A0")


class TestKo(unittest.TestCase):
    """劫形：白 C3 只剩 C2 一口气，黑下 C2 提，白不能立即回提。

        . . . . .
        . . X . .
        . X O X .
        . O . O .
        . . O . .
         ↑   ↑   ↑
        B2  C2  D2

    反直觉的地方：黑要落的 C2，它另外三个邻点（B2/D2/C1）必须是【白】子。
    如果摆成黑子，那它们就跟 C2 连成一片，而那片黑棋有自己的外气，提完子
    根本不被打吃，白回提也就提不掉 —— 那就不是劫。白子邻点不连气，
    才能让 C2 真的只剩 C3 一口气。
    """

    KO = """
        . . . . .
        . . X . .
        . X O X .
        . O . O .
        . . O . .
    """

    def test_ko_recapture_forbidden(self):
        g = from_diagram(self.KO, size=5, to_move="B")
        g.play("B", "C2")
        self.assertEqual(g.board[v2i("C3", 5)], EMPTY)
        self.assertRaises(IllegalMove, g.play, "W", "C3")

    def test_ko_recapture_ok_after_exchange(self):
        g = from_diagram(self.KO, size=5, to_move="B")
        g.play("B", "C2")
        g.play("W", "E5")      # 找劫材
        g.play("B", "A1")      # 黑应劫
        g.play("W", "C3")      # 现在回提合法
        self.assertEqual(g.board[v2i("C3", 5)], WHITE)
        self.assertEqual(g.board[v2i("C2", 5)], EMPTY)

    def test_undo_then_replay_ko_capture(self):
        # 悔掉提劫那一手再重下，不能因为局面重复被误判
        g = from_diagram(self.KO, size=5, to_move="B")
        g.play("B", "C2")
        g.undo()
        rec = g.play("B", "C2")
        self.assertEqual(rec["captures"], ["C3"])


class TestUndo(unittest.TestCase):
    def test_undo_restores_everything(self):
        g = from_diagram("""
            X X X .
            X O O .
            X X X .
            . . . .
        """, size=4, to_move="B")
        before = g.board[:]
        g.play("B", "D3")
        self.assertEqual(g.captures["B"], 2)
        g.undo()
        self.assertEqual(g.board, before)
        self.assertEqual(g.captures["B"], 0)
        self.assertEqual(g.moves, [])
        self.assertEqual(g.to_move, BLACK)

    def test_undo_empty_raises(self):
        self.assertRaises(IllegalMove, Game(19).undo)

    def test_undo_two(self):
        g = Game(19)
        g.play("B", "D4")
        g.play("W", "Q16")
        g.undo(2)
        self.assertEqual(g.moves, [])
        self.assertEqual(g.to_move, BLACK)

    def test_replaying_undone_move_is_allowed(self):
        # 悔棋后又下同一手，不能因为“局面重复”被误判成劫
        g = Game(19)
        g.play("B", "D4")
        g.undo()
        rec = g.play("B", "D4")
        self.assertEqual(rec["vertex"], "D4")


class TestPassAndFinish(unittest.TestCase):
    def test_two_passes_finish(self):
        g = Game(19)
        g.play("B", "pass")
        self.assertFalse(g.finished)
        g.play("W", "pass")
        self.assertTrue(g.finished)

    def test_playing_after_finish_raises(self):
        g = Game(19)
        g.play("B", "pass")
        g.play("W", "pass")
        self.assertRaises(IllegalMove, g.play, "B", "D4")

    def test_undo_reopens_game(self):
        g = Game(19)
        g.play("B", "pass")
        g.play("W", "pass")
        g.undo()                       # 悔掉白的停一手，轮次也回到白
        self.assertFalse(g.finished)
        self.assertEqual(g.to_move, WHITE)
        g.play("W", "D4")              # 能接着下


class TestScoring(unittest.TestCase):
    def test_empty_board_is_komi_for_white(self):
        s = Game(19).score()
        self.assertEqual((s["black"], s["white"]), (0, 0))
        self.assertEqual(s["result"], "W+7.5")

    def test_split_board(self):
        # B 列全黑、C 列全白。A 列归黑，D 列归白。
        # 黑 4 子 + 4 空 = 8；白 4 子 + 4 空 = 8。贴目定胜负。
        g = Game(4)
        for row in "1234":
            g.board[v2i("B" + row, 4)] = BLACK
            g.board[v2i("C" + row, 4)] = WHITE
        s = g.score()
        self.assertEqual((s["black"], s["white"]), (8, 8))
        self.assertEqual(s["result"], "W+7.5")

    def test_neutral_points_count_for_nobody(self):
        # 黑只占 A 列、白只占 D 列，中间 B/C 两列两边都挨着 —— 单官，谁都不算
        g = Game(4)
        for row in "1234":
            g.board[v2i("A" + row, 4)] = BLACK
            g.board[v2i("D" + row, 4)] = WHITE
        s = g.score()
        self.assertEqual((s["black"], s["white"]), (4, 4))

    def test_dead_stone_removal_changes_result(self):
        # 白 A1 是死子。拿掉它，整个 A 列才归黑。
        g = Game(4)
        for row in "1234":
            g.board[v2i("B" + row, 4)] = BLACK
            g.board[v2i("C" + row, 4)] = WHITE
        g.board[v2i("A1", 4)] = WHITE

        # 不拿死子：白 A1 一子 + C 列 4 子 + D 列 4 空 = 9。
        # A2..A4 同时挨着黑(B列)和白(A1)，是单官，谁都不算。
        with_dead = g.score()
        self.assertEqual((with_dead["black"], with_dead["white"]), (4, 9))
        self.assertEqual(with_dead["result"], "W+12.5")

        # 拿掉死子：整个 A 列 4 空归黑，黑变成 4 子 + 4 空 = 8；
        # 白 A1 没了，剩 C 列 4 子 + D 列 4 空 = 8。
        cleaned = g.score(dead=["A1"])
        self.assertEqual((cleaned["black"], cleaned["white"]), (8, 8))
        # 4x4 上 7.5 目贴目会压过一切，这里只验算法不看合理性
        self.assertEqual(cleaned["result"], "W+7.5")


class TestSerialization(unittest.TestCase):
    def test_replay_reproduces_board(self):
        g = Game(19)
        for v in ["D4", "Q16", "Q4", "D16", "pass", "R14"]:
            g.play(g.to_move, v)
        clone = Game.from_dict(g.to_dict())
        self.assertEqual(clone.board, g.board)
        self.assertEqual(clone.to_move, g.to_move)
        self.assertEqual(clone.captures, g.captures)
        self.assertEqual(len(clone.moves), len(g.moves))
        self.assertEqual(clone.finished, g.finished)


if __name__ == "__main__":
    unittest.main(verbosity=2)
