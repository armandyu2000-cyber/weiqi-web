"use strict";

/* 棋盘渲染与交互。只管「把给定的局面画出来」和「点哪儿了」，
 * 不管对局逻辑 —— 那是 app.js 的事。 */

const COLUMN_LETTERS = "ABCDEFGHJKLMNOPQRST";

function vertexToIndex(vertex, size) {
  const v = String(vertex).trim().toUpperCase();
  if (v === "PASS" || v === "PSS") return -1;
  const x = COLUMN_LETTERS.indexOf(v[0]);
  const row = parseInt(v.slice(1), 10);
  if (x < 0 || x >= size || !(row >= 1 && row <= size)) return -1;
  return (size - row) * size + x;
}

function indexToVertex(index, size) {
  const y = Math.floor(index / size);
  const x = index % size;
  return COLUMN_LETTERS[x] + (size - y);
}

function starPoints(size) {
  const lines = size === 19 ? [3, 9, 15] : size === 13 ? [3, 6, 9] : size === 9 ? [2, 4, 6] : [];
  const out = [];
  // 奇数路的棋盘中心单独一颗，偶数路（13 路）不要中心
  for (const y of lines) {
    for (const x of lines) {
      const middle = y === lines[1] && x === lines[1];
      if (middle && size % 2 === 0) continue;
      out.push([x, y]);
    }
  }
  return out;
}

class BoardView {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.size = options.size || 19;
    this.stones = new Array(this.size * this.size).fill(0);
    this.dead = new Set();       // 结果页标出的死子
    this.ownership = null;       // 每点归属（-1 全黑 .. +1 全白），画地盘用
    this.hint = null;            // 支招的那个点，不落子，只画个记号
    this.lastMove = null;
    this.hover = null;
    this.interactive = true;
    this.onPlay = options.onPlay || (() => {});
    this.geometry = null;

    canvas.addEventListener("click", (e) => this.handleClick(e));
    canvas.addEventListener("mousemove", (e) => this.handleMove(e));
    canvas.addEventListener("mouseleave", () => {
      if (this.hover !== null) { this.hover = null; this.draw(); }
    });
    window.addEventListener("resize", () => this.draw());
  }

  setPosition(stones, lastMove) {
    this.stones = stones;
    this.lastMove = lastMove ?? null;
    this.dead = new Set();
    this.draw();
  }

  setDead(indices) {
    this.dead = new Set(indices);
    this.draw();
  }

  setOwnership(values) {
    this.ownership = values;
    this.draw();
  }

  setHint(index) {
    this.hint = index;
    this.draw();
  }

  setInteractive(on) {
    this.interactive = on;
    this.canvas.style.cursor = on ? "pointer" : "default";
    if (!on && this.hover !== null) { this.hover = null; this.draw(); }
  }

  // --- 坐标换算 -----------------------------------------------------------

  layout() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = rect.width || 600;
    const h = rect.height || 600;
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
    }
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 边距要留够两位数坐标的宽度（"19" 比 "9" 宽一倍），不然行号会被画到
    // 画布外面割掉。按棋盘尺寸取比例，别用固定像素。
    const margin = Math.max(20, Math.min(w, h) * 0.075);
    const cell = Math.min((w - 2 * margin) / (this.size - 1), (h - 2 * margin) / (this.size - 1));
    const span = cell * (this.size - 1);
    this.geometry = {
      w, h, cell,
      ox: (w - span) / 2,
      oy: (h - span) / 2,
    };
  }

  pointOf(index) {
    const { cell, ox, oy } = this.geometry;
    return [ox + (index % this.size) * cell, oy + Math.floor(index / this.size) * cell];
  }

  indexAt(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const { cell, ox, oy } = this.geometry;
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    const x = Math.round((mx - ox) / cell);
    const y = Math.round((my - oy) / cell);
    if (x < 0 || y < 0 || x >= this.size || y >= this.size) return -1;
    // 离交叉点太远就不算点中，免得误触
    const [px, py] = this.pointOf(y * this.size + x);
    if (Math.hypot(mx - px, my - py) > cell * 0.55) return -1;
    return y * this.size + x;
  }

  // --- 事件 ---------------------------------------------------------------

  handleClick(e) {
    if (!this.interactive) return;
    const index = this.indexAt(e.clientX, e.clientY);
    if (index < 0) return;
    this.onPlay(index, indexToVertex(index, this.size));
  }

  handleMove(e) {
    if (!this.interactive) return;
    const index = this.indexAt(e.clientX, e.clientY);
    const next = index >= 0 && this.stones[index] === 0 ? index : null;
    if (next !== this.hover) { this.hover = next; this.draw(); }
  }

  // --- 绘制 ---------------------------------------------------------------

  draw() {
    this.layout();
    const { w, h, cell, ox, oy } = this.geometry;
    const ctx = this.ctx;
    ctx.clearRect(0, 0, w, h);

    this.drawWood(w, h);
    this.drawGrid(cell, ox, oy);
    this.drawOwnership(cell, ox, oy);      // 压在格线上、落在棋子下面
    this.drawCoordinates(cell, ox, oy, w, h);
    this.drawStones(cell, ox, oy);
  }

  drawWood(w, h) {
    const ctx = this.ctx;
    const grad = ctx.createLinearGradient(0, 0, w, h);
    grad.addColorStop(0, "#efd7a8");
    grad.addColorStop(0.5, "#e6c88f");
    grad.addColorStop(1, "#d8b478");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    // 几道淡淡的木纹，纯装饰
    ctx.save();
    ctx.globalAlpha = 0.05;
    ctx.strokeStyle = "#8a6234";
    ctx.lineWidth = 1;
    for (let i = 1; i < 9; i++) {
      const t = i / 9;
      ctx.beginPath();
      ctx.moveTo(0, h * t);
      ctx.bezierCurveTo(w * 0.3, h * t - 8, w * 0.7, h * t + 8, w, h * t - 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  drawGrid(cell, ox, oy) {
    const ctx = this.ctx;
    const n = this.size;
    const last = cell * (n - 1);
    ctx.strokeStyle = "#4a3520";
    ctx.lineWidth = Math.max(1, cell * 0.035);
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      ctx.moveTo(ox, oy + i * cell);
      ctx.lineTo(ox + last, oy + i * cell);
      ctx.moveTo(ox + i * cell, oy);
      ctx.lineTo(ox + i * cell, oy + last);
    }
    ctx.stroke();

    // 星位
    ctx.fillStyle = "#4a3520";
    for (const [x, y] of starPoints(n)) {
      ctx.beginPath();
      ctx.arc(ox + x * cell, oy + y * cell, Math.max(2, cell * 0.09), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  drawCoordinates(cell, ox, oy, w, h) {
    const ctx = this.ctx;
    const n = this.size;
    ctx.fillStyle = "rgba(74, 53, 32, 0.65)";
    ctx.font = `${Math.max(9, cell * 0.42)}px ui-sans-serif, system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const gap = cell * 0.62;
    for (let i = 0; i < n; i++) {
      const x = ox + i * cell;
      const y = oy + i * cell;
      ctx.fillText(COLUMN_LETTERS[i], x, oy - gap);
      ctx.fillText(COLUMN_LETTERS[i], x, oy + cell * (n - 1) + gap);
      ctx.fillText(String(n - i), ox - gap, y);
      ctx.fillText(String(n - i), ox + cell * (n - 1) + gap, y);
    }
  }

  drawOwnership(cell, ox, oy) {
    if (!this.ownership) return;
    const ctx = this.ctx;
    const half = cell * 0.44;
    const n = this.size;
    for (let i = 0; i < n * n; i++) {
      const v = this.ownership[i] || 0;
      const strength = Math.min(1, Math.abs(v));
      // 中腹未定的地方不上色，否则整盘糊成一片反而看不出谁占哪儿
      if (strength < 0.2) continue;
      // 黑地盘压深蓝、白地盘压浅蓝。用色相区分而不是只靠深浅 ——
      // 木色底本来就浅，纯白块压上去根本看不出来。
      ctx.fillStyle = v < 0 ? "#12305e" : "#8fc4f0";
      ctx.globalAlpha = strength * 0.55;
      ctx.fillRect(
        ox + (i % n) * cell - half,
        oy + Math.floor(i / n) * cell - half,
        half * 2, half * 2
      );
    }
    ctx.globalAlpha = 1;
  }

  drawStones(cell, ox, oy) {
    const ctx = this.ctx;
    const r = cell * 0.47;
    const n = this.size;

    if (this.hover !== null && this.interactive) {
      const [px, py] = this.pointOf(this.hover);
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = "#2b2b2b";
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    for (let i = 0; i < n * n; i++) {
      const color = this.stones[i];
      if (!color) continue;
      const [px, py] = this.pointOf(i);

      ctx.save();
      ctx.shadowColor = "rgba(60, 40, 15, 0.35)";
      ctx.shadowBlur = cell * 0.16;
      ctx.shadowOffsetY = cell * 0.05;

      const grad = ctx.createRadialGradient(
        px - r * 0.35, py - r * 0.4, r * 0.1,
        px, py, r * 1.05
      );
      if (color === 1) {
        grad.addColorStop(0, "#6d6d6d");
        grad.addColorStop(0.45, "#2e2e2e");
        grad.addColorStop(1, "#080808");
      } else {
        grad.addColorStop(0, "#ffffff");
        grad.addColorStop(0.6, "#f2f0ea");
        grad.addColorStop(1, "#cfc9bc");
      }
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      if (this.dead.has(i)) {
        ctx.strokeStyle = color === 1 ? "#ff6b6b" : "#c02828";
        ctx.lineWidth = Math.max(1.5, cell * 0.09);
        const d = r * 0.55;
        ctx.beginPath();
        ctx.moveTo(px - d, py - d);
        ctx.lineTo(px + d, py + d);
        ctx.moveTo(px + d, py - d);
        ctx.lineTo(px - d, py + d);
        ctx.stroke();
      }
    }

    if (this.lastMove !== null && this.lastMove >= 0 && this.stones[this.lastMove]) {
      const [px, py] = this.pointOf(this.lastMove);
      ctx.strokeStyle = this.stones[this.lastMove] === 1 ? "#f5d76e" : "#c0392b";
      ctx.lineWidth = Math.max(1.5, cell * 0.08);
      ctx.beginPath();
      ctx.arc(px, py, r * 0.42, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 支招的记号：虚线圈，画在空点上，不落子
    if (this.hint !== null && this.hint >= 0 && this.hint < n * n) {
      const [px, py] = this.pointOf(this.hint);
      ctx.save();
      ctx.strokeStyle = "#d9a441";
      ctx.lineWidth = Math.max(2, cell * 0.1);
      ctx.setLineDash([cell * 0.18, cell * 0.14]);
      ctx.beginPath();
      ctx.arc(px, py, r * 0.8, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }
  }
}
