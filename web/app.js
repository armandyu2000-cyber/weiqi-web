"use strict";

/* 页面状态与交互。
 *
 * 前端不重写一遍围棋规则：服务端返回的每一手都带着「被提掉的子」，
 * 所以要看第 N 手的局面，从空盘往前放一遍就行 —— 落子、然后抹掉提子。
 * 前进后退因此都是纯本地计算，不用问服务器。
 */

let state = null;
let viewPly = null;        // null 表示看当前局面，数字表示在看第几手
let currentResult = null;
let busy = false;
let showTerritory = false;  // 地盘底色开关，默认关
let hintVertex = null;      // 支招给的坐标，落下一手就作废
let hintPly = null;         // 这个记号属于第几手；null = 属于「当前局面」
let takeoverCount = 0;      // 选中的接管手数，可选值由服务端给（跟档位一个套路）
let takeoverRunning = false;  // 接管循环在不在跑；点「停止」就是把它置回 false
let machineRunning = false;   // 两个机器对下的循环在不在跑，同上

// 两个模式各有自己的一套档位：人机对局一个选择器，双机对局黑白各一个。
// 服务端只管「现在生效的是哪套」，记住两套是前端的事 —— 切模式时把目标
// 那套推过去，切回来时原样还给它。
let levelPrefs = null;        // { human: "3k", machine: { B: "9k", W: "5d" } }

// 自动循环的「代次」。切模式时 +1，把天上飞着那一手的结果作废 —— 否则它
// 回来会把切模式之前的状态盖回去，界面显示的模式就跟服务器对不上了。
let machineEpoch = 0;
let takeoverEpoch = 0;

// 哪些按钮是「人」才有的操作。两个机器对下时它们没有意义，藏起来。
const HUMAN_ONLY = ["undo", "pass", "hint", "takeover", "takeover-count", "resign"];

const $ = (id) => document.getElementById(id);

const board = new BoardView($("board"), {
  size: 19,
  onPlay: (_index, vertex) => playMove(vertex),
});

// --- 与服务器 ---------------------------------------------------------------

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* 空响应也当出错处理 */ }
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

// --- 局面推算 ---------------------------------------------------------------

function boardAt(moves, ply, size) {
  const stones = new Array(size * size).fill(0);
  for (let i = 0; i < ply; i++) {
    const move = moves[i];
    if (move.vertex === "pass") continue;
    const idx = vertexToIndex(move.vertex, size);
    if (idx < 0) continue;
    stones[idx] = move.color === "B" ? 1 : 2;
    // 提子必须在自己落子之后抹，顺序反了会把刚下的子自己吃掉
    for (const cap of move.captures || []) {
      const ci = vertexToIndex(cap, size);
      if (ci >= 0) stones[ci] = 0;
    }
  }
  return stones;
}

function capturesAt(moves, ply) {
  let black = 0, white = 0;
  for (let i = 0; i < ply; i++) {
    const n = (moves[i].captures || []).length;
    if (moves[i].color === "B") black += n; else white += n;
  }
  return { black, white };
}

// --- 渲染 -------------------------------------------------------------------

function render() {
  if (!state) return;
  const moves = state.moves;
  const atEnd = viewPly === null || viewPly >= moves.length;
  const ply = atEnd ? moves.length : viewPly;

  const stones = boardAt(moves, ply, state.size);
  const last = ply > 0 ? moves[ply - 1] : null;
  const lastIndex = last && last.vertex !== "pass" ? vertexToIndex(last.vertex, state.size) : -1;
  board.setPosition(stones, lastIndex);
  // 地盘是「当前局面」的东西，复盘翻到别的手就不该跟着显示
  board.setOwnership(showTerritory && atEnd ? state.ownership : null);
  // 支招记号要跟着它被问出来的那一手走 —— 复盘点「问引擎」时问的是历史局面，
  // 记号画在当前局面上就是骗人的
  const hintHere = hintPly === null ? atEnd : hintPly === ply;
  board.setHint(hintHere ? hintVertex : null);

  if (currentResult && currentResult.dead) {
    board.setDead(currentResult.dead
      .map((v) => vertexToIndex(v, state.size))
      .filter((i) => i >= 0));
  }

  const over = state.finished || Boolean(state.resigned);
  const machine = state.machine_play;
  const myTurn = !over && !machine && state.to_move === state.human_color && atEnd && !busy;
  board.setInteractive(myTurn);

  // 复盘到中间时，轮次按那一手来显示，而不是当前轮次
  const turnColor = ply < moves.length ? moves[ply].color : state.to_move;

  $("row-my-color").hidden = machine;
  $("row-turn").hidden = machine;
  $("row-side-black").hidden = !machine;
  $("row-side-white").hidden = !machine;

  if (machine) {
    // 机机模式：焦点是「谁在跟谁下、现在谁占优」，所以黑白各一行带档位，
    // 正在走的那方高亮 —— 看棋的时候不用回头去瞄设置卡
    for (const side of ["B", "W"]) {
      const el = $(LEVEL_ID[side].side);
      el.innerHTML = "";
      const dot = document.createElement("span");
      dot.className = `dot ${side === "B" ? "b" : "w"}`;
      const name = document.createElement("span");
      name.textContent = SIDE_NAME[side];
      const rank = document.createElement("span");
      rank.className = "rank";
      rank.textContent = levelLabel(state.levels[side]);
      el.append(dot, name, rank);

      const moving = !over && side === turnColor;
      el.classList.toggle("on", moving);
      if (moving) {
        const arrow = document.createElement("span");
        arrow.className = "arrow";
        arrow.textContent = "← 走";
        el.appendChild(arrow);
      }
    }
  } else {
    $("my-color").innerHTML =
      `<span class="dot ${state.human_color === "B" ? "b" : "w"}"></span>`
      + SIDE_NAME[state.human_color];
    $("turn").innerHTML = over
      ? "对局结束"
      : `<span class="dot ${turnColor === "B" ? "b" : "w"}"></span>`
        + SIDE_NAME[turnColor]
        + (turnColor === state.human_color ? "（你）" : "（电脑）");
  }

  const caps = capturesAt(moves, ply);
  $("captures").textContent = `黑 ${caps.black} · 白 ${caps.white}`;

  // 形势读的是 evals —— 跟胜率曲线同一份数：evals[i] 就是「棋盘上摆了 i 手」
  // 那个局面引擎怎么读的，所以按当前手数直接取，复盘翻到哪一手都有数。
  // 摆到最后一手之后 evals 还没这一项，退到前一项（引擎报的也正是那个局面）。
  //
  // 以前这里读的是 state.eval（引擎最近一次报的那个），那东西只描述「当前局面」——
  // 于是复盘一翻手、棋一下完、老棋谱一载入，这一行就永远是「—」。
  const evals = state.evals || [];
  const raw = evals.length ? evals[Math.min(ply, evals.length - 1)] : null;
  const ev = raw ? viewEval(raw) : null;
  let evText = "—";
  if (ev) {
    const pct = Math.round(ev.winrate * 100);
    evText = machine ? `黑 ${pct}%` : `你 ${pct}%`;
    if (ev.lead !== null) {
      evText += machine
        ? (ev.lead >= 0 ? ` · 黑优 ${ev.lead.toFixed(1)} 目`
                        : ` · 白优 ${(-ev.lead).toFixed(1)} 目`)
        : (ev.lead >= 0 ? ` · 领先 ${ev.lead.toFixed(1)} 目`
                        : ` · 落后 ${(-ev.lead).toFixed(1)} 目`);
    }
  }
  $("eval").textContent = evText;

  // ▶ 平时走到当前局面就该是灰的。双机对局里它多一层意思：手动放行，
  // 点一下让机器再落一子 —— 那时候得亮着（见 nextStep）。
  const canStep = state.machine_play && !over && !busy;
  $("ply-now").textContent = ply;
  $("ply-max").textContent = moves.length;
  $("to-start").disabled = ply === 0;
  $("prev").disabled = ply === 0;
  $("next").disabled = atEnd && !canStep;
  $("to-end").disabled = atEnd;

  $("undo").disabled = busy || moves.length === 0;
  $("pass").disabled = !myTurn;
  $("hint").disabled = !myTurn;
  // 接管中这个按钮得能点 —— 点它就是「停下」，所以它不受 busy 管，自己判
  const takingOver = takeoverRunning && state.takeover_left > 0;
  $("takeover-label").textContent = takingOver ? "停止接管" : "接管";
  $("takeover-hint").textContent = takingOver
    ? `还剩 ${state.takeover_left} 手`
    : "让机器替你走几步";
  $("takeover").disabled = takingOver ? false : !myTurn;
  $("resign").disabled = busy || over || moves.length === 0;
  $("territory").textContent = showTerritory ? "隐藏地盘" : "显示地盘";
  $("proposal").classList.toggle("on", Boolean(state.resign_proposal));
  $("review-hint").disabled = busy || moves.length === 0;
  // 停在最后一手之后就没有「接着下」可言 —— 那儿就是当前局面
  $("play-from").disabled = busy || atEnd;

  renderMode();
  drawCurve();
}

// 按模式收起当前模式下没意义的东西。
//
// 原则：别让人对着一个「点了也没用」的控件发懵 —— 机机模式下没有「我」，
// 所以执色选择、悔棋、提示、接管、认输全都该消失，而不是灰着。
function renderMode() {
  const machine = state.machine_play;
  const over = state.finished || Boolean(state.resigned);

  $("colors").hidden = machine;                       // 没有「我」，哪来的执色
  $("ops-title").textContent = machine ? "观战" : "操作";
  for (const id of HUMAN_ONLY) $(id).hidden = machine;

  // 模式／执色按钮：局终了没意义（都要有棋可接着下才成立），但自动循环
  // 跑着的时候必须留着 ——「人随时接手」靠的就是它们（顺手把循环叫停）。
  const locked = over || (busy && !machineRunning && !takeoverRunning);
  for (const id of ["modes", "colors"]) {
    for (const b of $(id).children) b.disabled = locked;
  }

  const el = $("machine");
  el.hidden = !machine;
  if (!machine) return;
  el.disabled = over || (!machineRunning && busy);
  // 暂停是「打断一下」，不是「丢掉这盘」—— 所以按钮是接着走，不是重开
  $("machine-label").textContent = machineRunning
    ? "暂停" : (state.moves.length ? "继续对弈" : "开始对弈");
  $("machine-hint").textContent = machineRunning
    ? "这一手走完就停" : "一路下完，▶ 一手一手";
}

// evals 一律存的是黑方视角（曲线只有一条，两边不统一视角会反着跳成血压计）。
// 机机模式直接读；人机模式得翻成人那一方 —— 人执白时「黑优」就是他落后。
function viewEval(e) {
  if (state.machine_play || state.human_color === "B") return e;
  return { winrate: 1 - e.winrate, lead: e.lead === null ? null : -e.lead };
}

const LEVEL_RE = /^(\d+)([kd])$/;

// 一方的那些元素：档位下拉、占位的「你」、局面卡里那一行
const LEVEL_ID = {
  B: { select: "level-black", you: "you-black", side: "side-black" },
  W: { select: "level-white", you: "you-white", side: "side-white" },
};
const SIDE_NAME = { B: "黑", W: "白" };

// "9k" → 业余 9 级，"9d" → 业余 9 段
function levelLabel(lv) {
  const m = LEVEL_RE.exec(lv || "");
  return m ? `业余 ${m[1]} ${m[2] === "k" ? "级" : "段"}` : lv;
}

function renderModes() {
  const el = $("modes");
  el.innerHTML = "";
  for (const [value, label] of [[false, "人机对局"], [true, "双机对局"]]) {
    const b = document.createElement("button");
    b.textContent = label;
    if (value === state.machine_play) b.classList.add("on");
    // 对局中途也点得动：棋留着，只换谁在下（见 switchMode）。
    // 这就是「人随时接手 / 让机器接手」的入口，没有别的按钮。
    b.onclick = () => switchMode(value);
    el.appendChild(b);
  }
}

function fillLevelSelect(el, chosen) {
  el.innerHTML = "";
  for (const lv of state.level_choices) {
    const opt = document.createElement("option");
    opt.value = lv;
    opt.textContent = levelLabel(lv);
    opt.selected = lv === chosen;
    el.appendChild(opt);
  }
}

// 人机模式：黑白两行都留着（一眼看得出谁是谁），但人自己那一方没有档位可选 ——
// 那个下拉换成灰框写个「你」，别让人对着一个改了也没用的控件发懵。
// 机机模式：两行都是真的下拉。
function renderLevels() {
  const machine = state.machine_play;
  const engineSide = state.human_color === "B" ? "W" : "B";
  for (const side of ["B", "W"]) {
    const mine = !machine && side !== engineSide;
    $(LEVEL_ID[side].select).hidden = mine;
    $(LEVEL_ID[side].you).hidden = !mine;
    fillLevelSelect($(LEVEL_ID[side].select), state.levels[side]);
  }
}

// 目标模式该用哪套档位。还没种下（init 之前）就沿用服务端现状。
function levelsFor(machine) {
  if (!levelPrefs) return state.levels;
  return machine
    ? { ...levelPrefs.machine }
    : { B: levelPrefs.human, W: levelPrefs.human };
}

// 换局／载入之后，拿服务端的现状把两套都填上 —— 之后各自记各自的。
//
// 注意别在 newGame 里调：那时服务端拿着的就是「当前那套」，用它去填另一套
// 会把另一套记住的档位冲掉（双机 9k/5d 切到人机 3k，一开新局两边全变 3k）。
function rememberLevels() {
  const engineSide = state.human_color === "B" ? "W" : "B";
  levelPrefs = { human: state.levels[engineSide], machine: { ...state.levels } };
}

function renderTakeoverCount() {
  const el = $("takeover-count");
  el.innerHTML = "";
  for (const n of state.takeover_choices) {
    const b = document.createElement("button");
    b.textContent = `${n} 手`;
    if (n === takeoverCount) b.classList.add("on");
    // 接管手数只在「开始」那一刻读一次，跑起来之后点它没意义
    b.onclick = () => {
      if (busy) return;
      takeoverCount = n;
      renderTakeoverCount();
    };
    el.appendChild(b);
  }
}

function renderColors() {
  const el = $("colors");
  el.innerHTML = "";
  for (const [value, label] of [["B", "我执黑"], ["W", "我执白"]]) {
    const b = document.createElement("button");
    b.textContent = label;
    if (value === state.human_color) b.classList.add("on");
    // 同样是保留棋局：换执色只是「从现在起这方归我」，棋盘不受影响。
    // 挑到不是轮次的那一方时，服务端会让引擎先补一手（见 switch_mode）。
    b.onclick = () => switchMode(false, value);
    el.appendChild(b);
  }
}

// --- 提示 -------------------------------------------------------------------

let toastTimer = null;
function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", Boolean(isError));
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), isError ? 4200 : 2200);
}

function setBusy(on, text) {
  busy = on;
  $("busy-text").textContent = text || "让俺想想";
  $("busy").classList.toggle("on", on);
}

// --- 结果 -------------------------------------------------------------------

async function refreshResult() {
  await refreshHistory();     // 记录只在对局结束时变，跟着这里刷一次最省事
  if (!state.finished && !state.resigned) {
    currentResult = null;
    $("result").classList.remove("on");
    return;
  }
  try {
    currentResult = await api("/api/result");
  } catch (err) {
    currentResult = null;
    return;
  }
  const el = $("result");
  const r = currentResult;
  if (!r || !r.finished) { el.classList.remove("on"); return; }
  el.classList.add("on");

  let detail;
  if (r.resigned) {
    detail = `${r.resigned === "B" ? "黑方" : "白方"}认输`;
  } else {
    detail = `黑 ${r.black} 子 · 白 ${r.white} 子（白含贴目 ${r.komi}）`;
    if (r.dead && r.dead.length) detail += `　已判死 ${r.dead.length} 子`;
  }
  const strong = document.createElement("b");
  strong.textContent = r.result;
  el.textContent = "对局结束　";
  el.appendChild(strong);
  const span = document.createElement("span");
  span.className = "banner-detail";
  span.textContent = detail;
  el.appendChild(span);
}

// --- 动作 -------------------------------------------------------------------

async function playMove(vertex) {
  if (busy) return;
  try {
    // 拆成两次请求：先把人的这一手落下来画出去，再单独等引擎。
    // 合成一次的话，人得盯着没变化的棋盘等引擎想完，才看见自己刚下的子。
    state = await api("/api/move", { vertex });
    viewPly = null;
    hintVertex = null;      // 子一落，上一手的支招就作废了
    hintPly = null;
    render();

    if (!state.finished && !state.resigned) {   // 自己停一手收的官，不用再问引擎
      setBusy(true, "让俺想想");
      state = await api("/api/ai_move", {});
      viewPly = null;
    }
    await refreshResult();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// 改档位不用重开一局，下一手就生效。改的是当前模式那一套，另一套原样留着。
async function setLevel(color, level) {
  try {
    const machine = state.machine_play;
    state = machine
      ? await api("/api/levels", { [color]: level })    // 机机：黑白各设各的
      : await api("/api/level", { level });             // 人机：只设对手那一方
    if (levelPrefs) {
      if (machine) levelPrefs.machine[color] = level;
      else levelPrefs.human = level;
    }
    renderLevels();
    render();
    toast(machine
      ? `${color === "B" ? "黑" : "白"}方：${levelLabel(level)}`
      : `对手棋力：${levelLabel(level)}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function undo() {
  if (busy) return;
  try {
    state = await api("/api/undo", {});
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

async function resign() {
  if (!confirm("确定认输？")) return;
  try {
    state = await api("/api/resign", {});
    await refreshResult();
    render();
  } catch (err) {
    toast(err.message, true);
  }
}

// 重开一局。换模式／换执色不走这儿了 —— 那些留着棋，见 switchMode。
async function newGame() {
  if (busy) return;
  const inProgress = state.moves.length > 0 && !state.finished && !state.resigned;
  if (inProgress && !confirm("当前这盘还没下完，确定开新局？")) return;
  setBusy(true, "准备新局");
  try {
    state = await api("/api/new", {
      human_color: state.human_color,
      machine_play: state.machine_play,
      levels: levelsFor(state.machine_play),   // 用这个模式记住的那套档位
    });
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
    renderModes();
    renderLevels();
    renderColors();
    render();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// 对局中途换模式／换执色 —— 棋留着，不重开一局。这是这个功能存在的唯一理由。
//
// 双机 → 人机：人接手「轮到的那一方」（轮谁就接谁），对手按人机对局那套档位走。
// 人机 → 双机：两台机器把两边都接过去，各按双机对局那套里自己那方走。
// 切到双机后机器就自己下起来 ——「接手」是接着下，不是摆个姿势等人再点一下。
async function switchMode(target, color) {
  if (busy && !machineRunning && !takeoverRunning) return;
  // 点到已经亮着的那一个、执色也没变，就不用跑这一趟
  if (target === state.machine_play && (!color || color === state.human_color)) return;
  if (state.finished || state.resigned) { toast("这盘已经结束了，开新局吧", true); return; }

  // 先叫停正在跑的自动循环。它每手一次请求，回来时代次已经对不上，会自己作废。
  machineRunning = false;
  takeoverRunning = false;
  machineEpoch += 1;
  takeoverEpoch += 1;

  setBusy(true, target ? "机器接手" : "我来接手");
  try {
    state = await api("/api/mode", {
      machine_play: target,
      human_color: color || null,     // 不传就是「轮谁就接谁」
      levels: levelsFor(target),
    });
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
    renderModes();
    renderLevels();
    renderColors();
    render();
    toast(target ? "机器接手 · 双机对局"
                 : `你接手，执${state.human_color === "B" ? "黑" : "白"}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
  // 看实际结果，不看 target：万一上面那趟没成，state 还是旧模式，
  // 这时候开循环就是在人机模式里空转（见 startMachine 的循环条件）。
  if (state.machine_play && !state.finished && !state.resigned) await startMachine();
}

async function askHint() {
  if (busy) return;
  setBusy(true, "让俺想想");
  try {
    const r = await api("/api/hint", {});
    hintVertex = vertexToIndex(r.vertex, state.size);
    hintPly = null;                 // 问的是当前局面
    // 支招也可能是停一手或认输，那就没点可标，退成一句话
    const where = hintVertex >= 0 ? r.vertex : (r.vertex === "pass" ? "停一手" : r.vertex);
    const pct = r.eval ? `　下这里之后你约 ${Math.round(r.eval.winrate * 100)}% 胜率` : "";
    toast(`电脑建议：${where}${pct}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// 复盘时停在某一手，问引擎「这里该下哪」。
// 服务端要把棋盘重放回那一步再问，所以比普通支招慢；问完它自己拉回来。
async function askReviewHint() {
  if (busy || !state.moves.length) return;
  const ply = viewPly === null ? state.moves.length : viewPly;
  setBusy(true, "让俺想想");
  try {
    const r = await api("/api/hint", { ply });
    hintVertex = vertexToIndex(r.vertex, state.size);
    hintPly = ply >= state.moves.length ? null : ply;
    const where = hintVertex >= 0 ? r.vertex : (r.vertex === "pass" ? "停一手" : r.vertex);
    const pct = r.eval ? `　当时黑约 ${Math.round(r.eval.winrate * 100)}%` : "";
    toast(`第 ${ply} 手这里，${r.color === "B" ? "黑" : "白"}方引擎想下：${where}${pct}`, false);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// 复盘停在某一手，从这里接着当实战下：这一手之后的棋丢掉（只在内存里丢，
// 原存档文件不动），人接手该走的那一方，对手按人机对局那套档位走。
async function playFrom() {
  if (busy) return;
  const ply = viewPly === null ? state.moves.length : viewPly;
  if (ply >= state.moves.length) return;
  const drop = state.moves.length - ply;
  if (!confirm(`从第 ${ply} 手接着下？后面的 ${drop} 手会丢掉（原棋谱文件不动）。`)) return;
  setBusy(true, "接着下");
  try {
    state = await api("/api/play_from", { ply, levels: levelsFor(false) });
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
    renderModes();
    renderLevels();
    renderColors();
    render();
    toast(`从第 ${ply} 手接着下，你执${state.human_color === "B" ? "黑" : "白"}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// 两个机器对下：一手一请求，跟接管那条路一个套路 —— 每手都画得出来，
// 也随时停得下来。一个请求闷头跑完一盘的话，界面得晾上半小时。
async function startMachine() {
  if (busy || machineRunning) return;
  const epoch = ++machineEpoch;
  machineRunning = true;
  setBusy(true, "双机对局中");
  try {
    // state.machine_play 是必须的：人机模式下 /api/ai_move 走到轮人就一手不走，
    // 少了这一条循环会空转着猛敲接口。
    while (machineRunning && state.machine_play
           && !state.finished && !state.resigned) {
      const next = await api("/api/ai_move", {});
      // 期间被切走了：这一手是按切之前的状态算的，拿它盖回去模式就错位了
      if (epoch !== machineEpoch) return;
      state = next;
      viewPly = null;
      hintVertex = null;
      hintPly = null;
      render();
    }
    await refreshResult();
  } catch (err) {
    toast(err.message, true);
  } finally {
    machineRunning = false;
    setBusy(false);
    render();
  }
}

function stopMachine() {
  machineRunning = false;              // 循环走到下一次判断就收手
  $("machine").disabled = true;        // 当前这手回来之前别再点
  toast("这一手走完就停");
}

// 手动放行：一键一手。双机模式下 /api/ai_move 本来就是「走一手」，
// 所以这儿只是不走循环 —— 想看清每一手就一下一下点，想省事就按「开始对弈」。
async function stepMachine() {
  if (busy || machineRunning) return;
  if (!state.machine_play || state.finished || state.resigned) return;
  setBusy(true, "让俺想想");
  try {
    state = await api("/api/ai_move", {});
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// ▶ / →：有下一手就翻过去看；已经站在当前局面了，就让机器再落一子。
// 「翻谱」和「放行」合成一个键，不用记两套；人机模式轮不到机器，它就只翻谱。
function nextStep() {
  const ply = viewPly === null ? state.moves.length : viewPly;
  if (ply < state.moves.length) { setPly(ply + 1); return; }
  stepMachine();
}

// 机器接管：一手一请求，跟落子那条路一样。所以每手都画得出来，
// 人也看得见它替你下的什么，中途还停得下来 —— 一个请求闷头跑十手的话
// 界面得晾好几十秒。
async function startTakeover() {
  if (busy || takeoverRunning) return;
  const epoch = ++takeoverEpoch;
  takeoverRunning = true;
  setBusy(true, "机器替你走");
  try {
    const first = await api("/api/takeover", { moves: takeoverCount });
    if (epoch !== takeoverEpoch) return;   // 期间被切走了，结果作废
    state = first;
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    render();
    while (takeoverRunning && state.takeover_left > 0
           && !state.finished && !state.resigned && !state.resign_proposal) {
      const next = await api("/api/takeover", {});
      if (epoch !== takeoverEpoch) return;
      state = next;
      viewPly = null;
      render();
    }
    await refreshResult();
  } catch (err) {
    toast(err.message, true);
  } finally {
    takeoverRunning = false;
    setBusy(false);
    render();
  }
}

function stopTakeover() {
  takeoverRunning = false;               // 循环走到下一次判断就收手
  $("takeover").disabled = true;         // 当前这手回来之前别再点
  toast("这一手走完就停");
}

// 引擎提了认输，人的答复。接受和继续下走的是同一套流程，只有接口不同。
async function answerResign(path) {
  if (busy) return;
  setBusy(true, "让俺想想");
  try {
    state = await api(path, {});
    viewPly = null;
    await refreshResult();
    render();
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

async function quitApp() {
  if (!confirm("退出程序？后台服务会关掉，要再下得重新双击应用。")) return;
  try { await api("/api/quit", {}); } catch (_) { /* 服务正在关，断连是正常的 */ }
  state = null;                  // 之后的 render() 会直接返回，不再碰已死的服务
  document.body.innerHTML = "";
  const el = document.createElement("div");
  el.className = "quit-screen";
  el.textContent = "程序已退出，这个页面可以关掉了。";
  document.body.appendChild(el);
}

async function saveGame() {
  if (!state.moves.length) { toast("还没下，没什么可存的", true); return; }
  const name = prompt("给这盘棋起个名字：", state.game_name || "");
  if (name === null) return;
  try {
    const r = await api("/api/save", { name });
    state.game_name = r.name;
    toast(`已保存：${r.name}`);
    await refreshSaves();
  } catch (err) {
    toast(err.message, true);
  }
}

async function refreshHistory() {
  let data = { total: 0, records: [] };
  try { data = await api("/api/history"); } catch (_) { /* 记录拿不到不影响下棋 */ }
  $("history-total").textContent = `共 ${data.total} 盘`;
  const el = $("history");
  el.innerHTML = "";
  if (!data.records.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "还没有下过";
    el.appendChild(d);
    return;
  }
  for (const r of data.records) el.appendChild(historyRow(r));
}

function durationText(seconds) {
  if (!seconds) return "不到 1 分";
  const m = Math.round(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分`;
}

// 记录卡很窄，"5k" → 「5级」就够了
function shortLevel(lv) {
  const m = LEVEL_RE.exec(lv || "");
  return m ? `${m[1]}${m[2] === "k" ? "级" : "段"}` : (lv || "—");
}

function historyRow(r) {
  const item = document.createElement("div");
  item.className = "hist";

  const top = document.createElement("div");
  top.className = "hist-top";
  const who = document.createElement("span");
  const res = document.createElement("span");

  if (r.machine_play) {
    // 两个机器下的：写双方的档位，胜负按黑方那个目差读
    const lv = r.levels || {};
    who.textContent = `${shortLevel(lv.B)}黑 vs ${shortLevel(lv.W)}白`;
    res.className = "win";
    res.textContent = `${r.winner === "B" ? "黑" : "白"}胜 ${Math.abs(r.margin).toFixed(1)} 目`;
  } else {
    who.textContent = `${shortLevel(r.level)} · 你执${r.human_color === "B" ? "黑" : "白"}`;
    res.className = r.human_won ? "win" : "lose";
    // 认输收场的盘没有目数，只有胜负
    res.textContent = r.human_margin === null
      ? (r.human_won ? "对方认输" : "你认输")
      : `${r.human_won ? "胜" : "负"} ${Math.abs(r.human_margin).toFixed(1)} 目`;
  }
  top.append(who, res);

  const sub = document.createElement("div");
  sub.className = "hist-sub";
  let subText = `${r.ended.slice(5)} · ${durationText(r.seconds)} · ${r.moves} 手`;
  // 机机对局没有悔棋/提示/接管可言，别印一排零出来
  if (!r.machine_play) {
    subText += ` · 悔 ${r.undos} 次 · 提示 ${r.hints} 次`;
    // 老记录里没有这个字段，没接管过就不占地方
    if (r.takeover) subText += ` · 机器代打 ${r.takeover} 手`;
  }
  sub.textContent = subText;
  item.append(top, sub);
  return item;
}

async function refreshSaves() {
  let list = [];
  try { list = await api("/api/games"); } catch (_) { /* 列表拿不到不影响下棋 */ }
  const el = $("saves");
  el.innerHTML = "";
  if (!list.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "还没有保存过";
    el.appendChild(d);
    return;
  }
  for (const g of list) {
    const item = document.createElement("div");
    item.className = "save-item";
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = g.name;                 // 用 textContent，棋谱名里带尖括号也不会出事
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${g.machine_play ? "机机 · " : ""}${g.moves} 手`;
    item.append(nm, meta);
    item.onclick = () => loadGame(g.id);
    el.appendChild(item);
  }
}

async function loadGame(id) {
  const inProgress = state.moves.length > 0 && !state.finished && !state.resigned;
  if (inProgress && !confirm("当前这盘还没下完，确定载入别的棋谱？")) return;
  setBusy(true, "载入棋谱");
  try {
    state = await api("/api/load", { id });
    rememberLevels();          // 这份棋谱自己带着档位，两套都按它重新种
    viewPly = null;
    hintVertex = null;
    hintPly = null;
    await refreshResult();
    renderModes();
    renderLevels();
    renderColors();
    render();
    toast(`已载入：${state.game_name}`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    setBusy(false);
    render();
  }
}

// --- 复盘控制 ---------------------------------------------------------------

function setPly(ply) {
  if (!state.moves.length) return;
  const clamped = Math.max(0, Math.min(ply, state.moves.length));
  viewPly = clamped >= state.moves.length ? null : clamped;
  render();
}

// --- 胜率曲线 ---------------------------------------------------------------

const CURVE_PAD = 5;

// evals 跟走子表一一对齐，evals[i] 是「第 i 手落下之前」的胜率（黑方视角）。
// 所以横轴第 i 格就是第 i 手，点一下就跳到那一手。
function curveX(i, n, w) {
  return n <= 1 ? w / 2 : CURVE_PAD + (w - CURVE_PAD * 2) * (i / (n - 1));
}

function curvePlyAt(clientX, n, w) {
  const t = (clientX - CURVE_PAD) / Math.max(1, w - CURVE_PAD * 2);
  return Math.round(t * (n - 1));
}

function drawCurve() {
  const cv = $("curve");
  const wrap = cv.parentElement;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  if (w <= 0 || h <= 0) return;

  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const evals = state.evals || [];
  const n = evals.length;
  if (!n) return;

  const plotH = h - CURVE_PAD * 2;
  const mid = CURVE_PAD + plotH / 2;
  const yAt = (wr) => CURVE_PAD + plotH * (1 - wr);      // 胜率高 = 靠上

  // 连续有数的那几手分成一段一段画 —— 人机模式下人那几手是 None，
  // 跨着空档连线会把一段没发生过的走势画出来
  const runs = [];
  let run = [];
  for (let i = 0; i < n; i++) {
    const e = evals[i];
    if (e) run.push({ x: curveX(i, n, w), y: yAt(e.winrate) });
    else if (run.length) { runs.push(run); run = []; }
  }
  if (run.length) runs.push(run);
  if (!runs.length) return;

  // 中线 = 五五开
  g.strokeStyle = "rgba(236, 231, 223, 0.20)";
  g.setLineDash([3, 3]);
  g.beginPath();
  g.moveTo(CURVE_PAD, mid);
  g.lineTo(w - CURVE_PAD, mid);
  g.stroke();
  g.setLineDash([]);

  // 曲线跟中线之间填色：上面是黑优、下面是白优。用裁剪精确分色，
  // 不用逐段判方向 —— 小段在五五开附近来回跳时逐段判会花。
  for (const seg of runs) {
    if (seg.length < 2) continue;
    const band = new Path2D();
    band.moveTo(seg[0].x, seg[0].y);
    for (let i = 1; i < seg.length; i++) band.lineTo(seg[i].x, seg[i].y);
    band.lineTo(seg[seg.length - 1].x, mid);
    band.lineTo(seg[0].x, mid);
    band.closePath();

    g.save();
    g.clip(band);
    g.fillStyle = "rgba(76, 132, 196, 0.34)";
    g.fillRect(0, 0, w, mid);
    g.fillStyle = "rgba(236, 231, 223, 0.20)";
    g.fillRect(0, mid, w, h - mid);
    g.restore();
  }

  g.strokeStyle = "#d9a441";
  g.lineWidth = 1.6;
  g.lineJoin = "round";
  for (const seg of runs) {
    g.beginPath();
    g.moveTo(seg[0].x, seg[0].y);
    for (let i = 1; i < seg.length; i++) g.lineTo(seg[i].x, seg[i].y);
    g.stroke();
  }

  // 当前看的是哪一手
  const cur = viewPly === null ? n - 1 : Math.min(viewPly, n - 1);
  const cx = curveX(cur, n, w);
  g.strokeStyle = "rgba(217, 164, 65, 0.55)";
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(cx, 0);
  g.lineTo(cx, h);
  g.stroke();
}

// --- 绑定 -------------------------------------------------------------------

$("to-start").onclick = () => setPly(0);
$("prev").onclick = () => setPly((viewPly === null ? state.moves.length : viewPly) - 1);
$("next").onclick = nextStep;
$("to-end").onclick = () => setPly(state.moves.length);
$("undo").onclick = undo;
$("pass").onclick = () => playMove("pass");
$("hint").onclick = askHint;
$("takeover").onclick = () => (takeoverRunning ? stopTakeover() : startTakeover());
$("machine").onclick = () => (machineRunning ? stopMachine() : startMachine());
$("review-hint").onclick = askReviewHint;
$("play-from").onclick = playFrom;
$("level-black").onchange = (e) => setLevel("B", e.target.value);
$("level-white").onchange = (e) => setLevel("W", e.target.value);
$("curve").onclick = (e) => {
  const n = (state.evals || []).length;
  if (!n) return;
  const box = e.target.getBoundingClientRect();
  setPly(curvePlyAt(e.clientX - box.left, n, box.width));
};
window.addEventListener("resize", () => { if (state) drawCurve(); });
$("territory").onclick = () => { showTerritory = !showTerritory; render(); };
$("accept-resign").onclick = () => answerResign("/api/accept_resign");
$("decline-resign").onclick = () => answerResign("/api/decline_resign");
$("resign").onclick = resign;
$("new").onclick = () => newGame();
$("quit").onclick = quitApp;
$("save").onclick = saveGame;

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  const cur = viewPly === null ? state.moves.length : viewPly;
  if (e.key === "ArrowLeft") { e.preventDefault(); setPly(cur - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); nextStep(); }
  else if (e.key === "Home") { e.preventDefault(); setPly(0); }
  else if (e.key === "End") { e.preventDefault(); setPly(state.moves.length); }
});

// --- 启动 -------------------------------------------------------------------

(async function init() {
  try {
    state = await api("/api/state");
  } catch (err) {
    toast(`连不上服务：${err.message}`, true);
    return;
  }
  takeoverCount = state.takeover_choices[0];
  rememberLevels();
  renderModes();
  renderLevels();
  renderTakeoverCount();
  renderColors();
  await refreshResult();
  render();
  await refreshSaves();
})();
