"""围棋规则：棋盘、气、提子、自杀、劫、数子。

19 路，中国规则（数子法，贴 7.5 目）。

这个模块是棋盘的唯一权威。KataGo 不是裁判 —— 它的 play 命令不检查轮次
（实测黑连下两手会被接受），final_score 在中国规则下返回的也是神经网络
估算值而不是数子结果。所以“轮到谁”“这手合不合法”“这盘谁赢”全在这里定。

坐标用 GTP 约定：列从 A 开始但跳过 I，行从下往上数。
内部是扁平的下标，y=0 是最上面一行。
"""

EMPTY, BLACK, WHITE = 0, 1, 2
COLOR_NAMES = {BLACK: "B", WHITE: "W"}
SIZE = 19
KOMI = 7.5
COLUMN_LETTERS = "ABCDEFGHJKLMNOPQRST"  # 19 个，没有 I


class IllegalMove(ValueError):
    """不合法的落子。抛之前棋盘不会被改动。"""


def vertex_to_index(vertex, size=SIZE):
    """'D4' -> 扁平下标。坐标不合法抛 IllegalMove。

    抛 IllegalMove 而不是裸 ValueError：调用方（服务端）把它当「用户下错了」
    翻译成 400。抛裸 ValueError 的话会漏进兜底分支，变成 500 —— 用户输错个
    坐标看起来像服务器崩了。
    """
    if not isinstance(vertex, str):
        raise IllegalMove(f"坐标得是字符串：{vertex!r}")
    v = vertex.strip().upper()
    if len(v) < 2:
        raise IllegalMove(f"坐标太短：{vertex!r}")
    letter, digits = v[0], v[1:]
    x = COLUMN_LETTERS[:size].find(letter)
    if x < 0:
        raise IllegalMove(f"列超出棋盘：{vertex!r}")
    if not digits.isdigit():
        raise IllegalMove(f"行号不是数字：{vertex!r}")
    row = int(digits)
    if not 1 <= row <= size:
        raise IllegalMove(f"行超出棋盘：{vertex!r}")
    return (size - row) * size + x


def index_to_vertex(index, size=SIZE):
    y, x = divmod(index, size)
    return f"{COLUMN_LETTERS[x]}{size - y}"


_NEIGHBORS = {}


def _neighbors_for(size):
    if size not in _NEIGHBORS:
        table = []
        for i in range(size * size):
            y, x = divmod(i, size)
            near = []
            if y > 0:
                near.append(i - size)
            if y < size - 1:
                near.append(i + size)
            if x > 0:
                near.append(i - 1)
            if x < size - 1:
                near.append(i + 1)
            table.append(tuple(near))
        _NEIGHBORS[size] = table
    return _NEIGHBORS[size]


def _other(color):
    return WHITE if color == BLACK else BLACK


def _normalize_color(color):
    if color in (BLACK, WHITE):
        return color
    name = str(color).strip().upper()
    if name in ("B", "BLACK"):
        return BLACK
    if name in ("W", "WHITE"):
        return WHITE
    raise ValueError(f"不认识的颜色：{color!r}")


def _group(board, start, neighbors):
    """从 start 出发的同色连通块。返回 (棋子下标集合, 气下标集合)。"""
    color = board[start]
    stones = {start}
    liberties = set()
    stack = [start]
    while stack:
        i = stack.pop()
        for n in neighbors[i]:
            if board[n] == EMPTY:
                liberties.add(n)
            elif board[n] == color and n not in stones:
                stones.add(n)
                stack.append(n)
    return stones, liberties


def is_pass(vertex):
    return isinstance(vertex, str) and vertex.strip().lower() in ("pass", "pss")


class Game:
    """一盘棋。落子会就地改变对象状态。

    悔棋靠整盘快照，不做增量回滚 —— 361 个整数的拷贝便宜到可以忽略，
    而增量回滚是那种会静默错一子的地方。
    """

    def __init__(self, size=SIZE, komi=KOMI):
        self.size = size
        self.komi = komi
        self.neighbors = _neighbors_for(size)
        self.board = [EMPTY] * (size * size)
        self.moves = []                    # 给前端用的走子表
        self.captures = {"B": 0, "W": 0}   # 各方提掉的子数
        self.to_move = BLACK
        self.finished = False
        self._history = []
        self._positions = set()            # 位置超级劫用

    # --- 落子 -------------------------------------------------------------

    def play(self, color, vertex):
        """落子并返回走子记录。不合法则抛 IllegalMove，棋盘不动。"""
        color = _normalize_color(color)
        if self.finished:
            raise IllegalMove("对局已结束，要么复盘要么开新局")
        if color != self.to_move:
            raise IllegalMove(f"现在轮到 {COLOR_NAMES[self.to_move]} 走")

        if is_pass(vertex):
            vertex = "pass"
            new_board = self.board[:]
            captured = []
            placed = False
        else:
            placed = True
            idx = vertex_to_index(vertex, self.size)
            if self.board[idx] != EMPTY:
                raise IllegalMove(f"{vertex} 已经有子了")

            new_board = self.board[:]
            new_board[idx] = color
            opponent = _other(color)

            # 先提对方的死子
            doomed = set()
            for n in self.neighbors[idx]:
                if new_board[n] == opponent:
                    stones, liberties = _group(new_board, n, self.neighbors)
                    if not liberties:
                        doomed |= stones
            for s in doomed:
                new_board[s] = EMPTY

            # 提完再判自己的气。顺序反了会把“提子”误判成“自杀”。
            _, my_liberties = _group(new_board, idx, self.neighbors)
            if not my_liberties:
                raise IllegalMove(f"{vertex} 是自杀手")

            captured = [index_to_vertex(s, self.size) for s in sorted(doomed)]
            vertex = index_to_vertex(idx, self.size)

        # 位置超级劫：局面不许重复。比“单子单气”那套启发式简单，也更贴近
        # 中国规则里禁全同的正式定义，顺带把三劫循环之类一起挡了。
        #
        # 只对真正落子的手生效。停一手不改变局面，拿它去比对的话每一次 pass
        # 都会等于当前局面，于是每一手 pass 都被误判成重复局面 —— 棋就没法结束了。
        if placed:
            self._positions.add(bytes(self.board))
            if bytes(new_board) in self._positions:
                raise IllegalMove("劫：这个局面刚才出现过，不能重复")

        return self._commit(color, vertex, captured, new_board, placed)

    def _commit(self, color, vertex, captured, new_board, placed):
        # 存引用而不是拷贝：self.board 永远是整个替换，从不就地改
        self._history.append((
            self.board, self.to_move, dict(self.captures), self.finished,
            bytes(new_board) if placed else None,
        ))
        self.board = new_board
        self.captures[COLOR_NAMES[color]] += len(captured)
        self.to_move = _other(color)
        if placed:
            self._positions.add(bytes(new_board))

        move = {
            "n": len(self.moves) + 1,
            "color": COLOR_NAMES[color],
            "vertex": vertex,
            "captures": captured,
        }
        self.moves.append(move)

        if len(self.moves) >= 2 and all(m["vertex"] == "pass" for m in self.moves[-2:]):
            self.finished = True
        return move

    def undo(self, times=1):
        """悔棋。退掉一个局面上的所有痕迹，包括劫禁着。"""
        for _ in range(times):
            if not self.moves:
                raise IllegalMove("没有可悔的棋了")
            board, to_move, captures, finished, position = self._history.pop()
            if position is not None:      # 停一手没有产生新局面，别误删已有的
                self._positions.discard(position)
            self.board = board
            self.to_move = to_move
            self.captures = captures
            self.finished = finished
            self.moves.pop()

    # --- 数子 -------------------------------------------------------------

    def score(self, dead=None):
        """中国规则数子。dead 是被判死的棋子坐标（字符串），会被拿掉再数。

        不做死活判断 —— 那是调用方的事（问引擎 final_status_list dead，
        或让人自己点）。
        """
        board = self.board[:]
        for vertex in dead or ():
            board[vertex_to_index(vertex, self.size) if isinstance(vertex, str) else vertex] = EMPTY

        black = board.count(BLACK)
        white = board.count(WHITE)

        seen = set()
        for start in range(len(board)):
            if board[start] != EMPTY or start in seen:
                continue
            region = []
            border = set()
            stack = [start]
            seen.add(start)
            while stack:
                i = stack.pop()
                region.append(i)
                for n in self.neighbors[i]:
                    if board[n] == EMPTY:
                        if n not in seen:
                            seen.add(n)
                            stack.append(n)
                    else:
                        border.add(board[n])
            # 只被一方围住的空点算那方的地；两边都挨着的是单官，谁都不算
            if border == {BLACK}:
                black += len(region)
            elif border == {WHITE}:
                white += len(region)

        margin = black - (white + self.komi)
        winner = "B" if margin > 0 else "W"
        return {
            "black": black,
            "white": white,
            "komi": self.komi,
            "margin": margin,
            "winner": winner,
            "result": f"{winner}+{abs(margin):.1f}",
        }

    # --- 存档 -------------------------------------------------------------

    def to_dict(self):
        return {
            "size": self.size,
            "komi": self.komi,
            "moves": self.moves,
            "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data):
        """重放走子表。棋盘和提子数都是重放出来的，不信存档里的快照。"""
        game = cls(data.get("size", SIZE), data.get("komi", KOMI))
        for move in data.get("moves", []):
            game.play(move["color"], move["vertex"])
        return game

    def state(self):
        return {
            "board": self.board,
            "size": self.size,
            "komi": self.komi,
            "moves": self.moves,
            "captures": self.captures,
            "to_move": COLOR_NAMES[self.to_move],
            "finished": self.finished,
        }


def from_diagram(text, size=SIZE, to_move="B"):
    """用字符画建局面，给测试和调试用。X 黑、O 白、. 空，第一行在最上面。"""
    rows = [r.replace(" ", "") for r in text.strip().splitlines() if r.strip()]
    if len(rows) != size:
        raise ValueError(f"字符画应有 {size} 行，实际 {len(rows)} 行")
    game = Game(size)
    for y, row in enumerate(rows):
        if len(row) != size:
            raise ValueError(f"第 {y + 1} 行应有 {size} 列，实际 {len(row)} 列")
        for x, ch in enumerate(row):
            if ch == "X":
                game.board[y * size + x] = BLACK
            elif ch == "O":
                game.board[y * size + x] = WHITE
            elif ch != ".":
                raise ValueError(f"字符画里出现不认识的符号：{ch!r}")
    game.to_move = _normalize_color(to_move)
    return game
