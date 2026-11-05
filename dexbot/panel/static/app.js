const token = document.querySelector('meta[name="dexbot-token"]').content;
const names = {
  base_analysis: "База",
  volume: "Объём",
  news: "Новости",
  planner: "План",
  risk_manager: "Риск",
  skeptic: "Скептик",
  team_lead: "Тимлид",
};
const providers = { xai: "xAI", deepseek: "DeepSeek", rules: "правила", scripted: "макет" };

const money = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
};
const plain = (value) => (value === null || value === undefined ? "—" : Number(value).toFixed(2));
const qty = (value) => Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 6 });
const when = (value) => value ? value.replace("T", " ").replace("+00:00", " UTC") : "—";

function pill(text, kind) {
  const node = document.createElement("span");
  node.className = `pill ${kind || ""}`;
  node.textContent = text;
  return node;
}

function drawSeries(canvas, points, color, fill) {
  const ctx = canvas.getContext("2d");
  const width = canvas.clientWidth || 320;
  const height = 220;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  if (!points.length) {
    ctx.fillStyle = "#8e9a8c";
    ctx.font = "14px Segoe UI, sans-serif";
    ctx.fillText("Пока нет точек", 16, 36);
    return;
  }
  const values = points.map((p) => p.y);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pad = 18;
  const coords = points.map((p, i) => {
    const x = pad + (i / Math.max(points.length - 1, 1)) * (width - pad * 2);
    const y = height - pad - ((p.y - min) / span) * (height - pad * 2);
    return [x, y];
  });
  ctx.beginPath();
  coords.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();
  if (fill) {
    ctx.lineTo(coords[coords.length - 1][0], height - pad);
    ctx.lineTo(coords[0][0], height - pad);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  }
}

function render(data) {
  const desk = data.desk;
  const port = data.portfolio;
  const pills = document.getElementById("pills");
  pills.replaceChildren(
    pill(desk.running ? "процесс идёт" : "процесс не найден", desk.running ? "ok" : "warn"),
    pill(`режим ${desk.mode}`),
    pill(desk.kill_switch ? "kill switch включён" : "kill switch выключен", desk.kill_switch ? "warn" : "ok"),
    pill(`последний цикл ${when(desk.last_cycle_at)}`),
    pill(`следующий ${when(desk.next_cycle_at)}`),
  );
  document.getElementById("clock").textContent = new Date().toLocaleString("ru-RU");

  const cards = [
    ["Кэш USDT", plain(port.cash), ""],
    ["Эквити", plain(port.equity), ""],
    ["Реализовано", money(port.realized_pnl), port.realized_pnl < 0 ? "down" : "up"],
    ["Нереализовано", money(port.unrealized_pnl), port.unrealized_pnl < 0 ? "down" : "up"],
  ];
  document.getElementById("stats").replaceChildren(...cards.map(([label, value, kind]) => {
    const node = document.createElement("article");
    node.className = "stat";
    const title = document.createElement("h3");
    title.textContent = label;
    const strong = document.createElement("strong");
    strong.className = kind;
    strong.textContent = value;
    node.append(title, strong);
    return node;
  }));

  document.getElementById("equity-note").textContent = data.charts.note || "Снимки из журнала, в конце каждого прохода.";
  const dd = data.charts.max_drawdown;
  document.getElementById("dd-note").textContent = dd === null
    ? "Нужны хотя бы две точки эквити"
    : `Максимум ${(dd * 100).toFixed(2)}% от пика`;

  drawSeries(
    document.getElementById("equity-chart"),
    data.charts.equity.map((p) => ({ y: p.equity })),
    "#d7b56d",
    "rgba(215, 181, 109, 0.16)",
  );
  drawSeries(
    document.getElementById("pnl-chart"),
    data.charts.realized.map((p) => ({ y: p.cumulative })),
    "#8fbf7f",
    "rgba(143, 191, 127, 0.12)",
  );
  drawSeries(
    document.getElementById("dd-chart"),
    data.charts.drawdown.map((p) => ({ y: p.dd * 100 })),
    "#d07a6a",
    "rgba(208, 122, 106, 0.12)",
  );

  const positions = document.getElementById("positions");
  if (!port.positions.length) {
    positions.innerHTML = `<p class="empty">Открытых позиций в журнале нет.</p>`;
  } else {
    const table = document.createElement("table");
    table.innerHTML = "<thead><tr><th>Пара</th><th>Кол-во</th><th>Вход</th><th>Марк</th><th>Стоп</th><th>Тейк</th><th>PnL</th></tr></thead>";
    const body = document.createElement("tbody");
    port.positions.forEach((row) => {
      const tr = document.createElement("tr");
      const cells = [
        row.symbol,
        qty(row.qty),
        plain(row.avg_entry),
        plain(row.last_mark),
        plain(row.stop_loss),
        plain(row.take_profit),
        money(row.unrealized_pnl),
      ];
      cells.forEach((text, index) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (index === cells.length - 1) td.className = row.unrealized_pnl < 0 ? "down" : "up";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    positions.replaceChildren(table);
  }

  const orphans = document.getElementById("orphans");
  if (port.exchange_error) {
    orphans.innerHTML = `<p class="empty">${port.exchange_error}</p>`;
  } else if (!port.exchange_checked) {
    orphans.innerHTML = `<p class="empty">Баланс биржи не запрашивался (бумажный режим или панель без ключей).</p>`;
  } else if (!port.orphans.length) {
    orphans.innerHTML = `<p class="empty">Лишних монет нет: всё ненулевое, кроме стейблов, есть в журнале.</p>`;
  } else {
    const note = document.createElement("p");
    note.className = "empty";
    note.textContent = "Импорт в журнал отсюда не делается: без входа и стопа бот не знает, где выходить.";
    orphans.replaceChildren(...port.orphans.map((row) => {
      const node = document.createElement("div");
      node.className = "orphan";
      const name = document.createElement("b");
      name.textContent = row.asset;
      const amount = document.createElement("span");
      amount.textContent = qty(row.qty);
      node.append(name, amount);
      return node;
    }), note);
  }

  const votes = document.getElementById("votes");
  const llm = data.llm.latest || {};
  const who = (llm.providers || []).map((name) => providers[name] || name).join(", ") || "ещё не было";
  const head = document.createElement("p");
  head.className = "empty";
  head.textContent = `Последний провайдер: ${who}. Вызовов: ${llm.calls || 0}, токены: ${llm.total_tokens || 0}.`;
  if (!data.agents.recent.length) {
    votes.replaceChildren(head, Object.assign(document.createElement("p"), { className: "empty", textContent: "Голосов в журнале пока нет." }));
  } else {
    const list = document.createElement("div");
    data.agents.recent.forEach((row) => {
      const node = document.createElement("div");
      node.className = "vote";
      const name = document.createElement("b");
      name.textContent = names[row.agent] || row.agent;
      const detail = document.createElement("span");
      detail.textContent = `${row.symbol} · ${row.reasoning || ""}`;
      const stance = document.createElement("span");
      stance.textContent = row.stance || "—";
      node.append(name, detail, stance);
      list.appendChild(node);
    });
    votes.replaceChildren(head, list);
  }

  const useful = document.getElementById("useful");
  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>Агент</th><th>n</th><th>hit</th><th>usefulness</th></tr></thead>";
  const body = document.createElement("tbody");
  Object.entries(data.agents.usefulness || {}).forEach(([agent, stat]) => {
    const tr = document.createElement("tr");
    const hit = stat.hit_rate === null ? "—" : `${(stat.hit_rate * 100).toFixed(0)}%`;
    const lift = stat.usefulness === null ? "—" : stat.usefulness.toFixed(2);
    [names[agent] || agent, String(stat.supported), hit, lift].forEach((text) => {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  useful.replaceChildren(table);
}

async function load() {
  const response = await fetch("/api/state");
  render(await response.json());
}

async function post(url) {
  const response = await fetch(url, { method: "POST", headers: { "X-Dexbot-Token": token } });
  const body = await response.json();
  document.getElementById("action-note").textContent = body.detail || "";
  await load();
}

document.getElementById("btn-kill").onclick = () => post("/api/kill");
document.getElementById("btn-resume").onclick = () => post("/api/resume");
document.getElementById("btn-refresh").onclick = () => post("/api/refresh");
document.getElementById("btn-once").onclick = () => {
  if (window.confirm("Запустить один боевой проход? Если служба уже идёт, панель откажется.")) post("/api/run-once");
};

load();
setInterval(load, 15000);
window.addEventListener("resize", load);
