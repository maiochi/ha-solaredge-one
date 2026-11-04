/**
 * SolarEdge Energy Card: casa isométrica animada com os painéis do telhado
 * coloridos pela potência de cada otimizador.
 *
 * Uso mínimo (descobre as entidades da integração solaredge_one sozinho):
 *   type: custom:solaredge-energy-card
 *
 * Opções: title, power, state, energy_today, energy_month, energy_total,
 * array_power, optimizers (lista), max_power, panel_max_power.
 */

const DOMAIN = "solaredge_one";
const CARD_VERSION = "0.3.0";

// --- Geometria da casa (projeção oblíqua, viewBox 0 0 400 290) -------------
const A = [220, 262]; // canto frontal inferior esquerdo da fachada
const B = [320, 237]; // canto frontal inferior direito
const H = 70; // altura das paredes
const D = [-130, -55]; // vetor de profundidade (para trás e para a esquerda)
const APEX = [270, 132]; // cumeeira na fachada

const add = (p, q) => [p[0] + q[0], p[1] + q[1]];
const sub = (p, q) => [p[0] - q[0], p[1] - q[1]];
const mul = (p, k) => [p[0] * k, p[1] * k];
const pts = (arr) => arr.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");

const A2 = add(A, [0, -H]);
const B2 = add(B, [0, -H]);
const roof = (u, v) => add(add(A2, mul(D, u)), mul(sub(APEX, A2), v));
const side = (u, t) => add(add(A, mul(D, u)), [0, -H * t]);
const front = (s, t) => add(add(A, mul(sub(B, A), s)), [0, -H * t]);
const quad = (f, a0, a1, b0, b1) => [f(a0, b0), f(a1, b0), f(a1, b1), f(a0, b1)];

const PANEL_COLD = [27, 42, 68];
const PANEL_HOT = [56, 189, 248];

function mix(c0, c1, k) {
  const c = c0.map((v, i) => Math.round(v + (c1[i] - v) * k));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

class SolarEdgeEnergyCard extends HTMLElement {
  setConfig(config) {
    this._config = Object.assign({ title: "Energia", panel_max_power: 440 }, config);
    this._built = false;
  }

  static getStubConfig() {
    return {};
  }

  getCardSize() {
    return 7;
  }

  set hass(hass) {
    this._hass = hass;
    try {
      if (!this._built) {
        this._entities = this._resolve(hass);
        this._build();
        this._built = true;
      }
      this._update();
    } catch (err) {
      // Mostra o erro no próprio card em vez do "Erro de configuração" genérico
      console.error("solaredge-energy-card", err);
      this._built = false;
      this.innerHTML = `<ha-card><div style="padding:16px;color:#f87171;font-family:monospace;white-space:pre-wrap">SolarEdge card v${CARD_VERSION}\n${String((err && err.stack) || err).slice(0, 600)}</div></ha-card>`;
    }
  }

  // Descobre as entidades da integração pelo registro (translation_key)
  _resolve(hass) {
    const cfg = this._config;
    const devices = hass.devices || {};
    const ents = Object.values(hass.entities || {}).filter((e) => e.platform === DOMAIN);
    const model = (e) => (devices[e.device_id] || {}).model;
    const site = (key) =>
      (ents.find((e) => e.translation_key === key && model(e) === "Site") || {}).entity_id;
    const byDevice = (key) =>
      Object.fromEntries(
        ents.filter((e) => e.translation_key === key).map((e) => [e.device_id, e.entity_id])
      );

    const temps = byDevice("temperature");
    const daily = byDevice("daily_energy");
    let optimizers;
    if (cfg.optimizers) {
      optimizers = cfg.optimizers.map((entity) => ({ power: entity }));
    } else {
      optimizers = ents
        .filter((e) => e.translation_key === "power")
        .map((e) => ({
          power: e.entity_id,
          name: (devices[e.device_id] || {}).name || e.entity_id,
          temperature: temps[e.device_id],
          daily: daily[e.device_id],
        }))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    }

    return {
      power: cfg.power || site("pv_power"),
      state: cfg.state || site("production_state"),
      today: cfg.energy_today || site("daily_energy"),
      month: cfg.energy_month || site("monthly_energy"),
      total: cfg.energy_total || site("lifetime_energy"),
      array: cfg.array_power || site("array_power"),
      optimizers,
    };
  }

  _build() {
    const n = Math.max(this._entities.optimizers.length, 1);
    const rows = n > 8 ? 2 : 1;
    const cols = Math.ceil(n / rows);
    const panels = this._entities.optimizers
      .map((_, i) => {
        const r = Math.floor(i / cols);
        const c = i % cols;
        const du = 0.9 / cols;
        const dv = 0.8 / rows;
        const q = quad(
          roof,
          0.05 + c * du + 0.012,
          0.05 + (c + 1) * du - 0.012,
          0.1 + r * dv + 0.035,
          0.1 + (r + 1) * dv - 0.035
        );
        return `<polygon class="panel" data-i="${i}" points="${pts(q)}"><title></title></polygon>`;
      })
      .join("");

    const roofPlane = [A2, APEX, add(APEX, D), add(A2, D)];
    const roofBack = [B2, APEX, add(APEX, D), add(B2, D)];
    const flowRoof = [roof(0.1, 0.5), roof(0.1, 0), side(0.1, 0.62)];
    const flowGround = [side(0.1, 0.36), side(0.1, 0), A, B, add(B, mul(sub(B, A), 0.62))];
    const path = (p) => "M" + p.map((q) => q.map((v) => v.toFixed(1)).join(" ")).join(" L");

    const stars = [
      [40, 30], [80, 18], [130, 40], [175, 14], [300, 26], [250, 52], [375, 70], [330, 92],
    ]
      .map(([x, y], i) => `<circle class="star" cx="${x}" cy="${y}" r="1.2" style="animation-delay:${i * 0.4}s"/>`)
      .join("");

    this.innerHTML = `
      <ha-card>
        <style>${STYLE}</style>
        <div class="wrap">
          <div class="header">
            <div class="title">${this._config.title}</div>
            <div class="chip-state"><span class="dot"></span><span class="state-text">—</span></div>
          </div>
          <div class="stats">
            <div class="stat" data-entity="today"><div class="v">—</div><div class="l">Hoje</div><div class="line"></div></div>
            <div class="stat" data-entity="month"><div class="v">—</div><div class="l">Mês</div><div class="line"></div></div>
            <div class="stat" data-entity="total"><div class="v">—</div><div class="l">Total</div><div class="line"></div></div>
          </div>
          <svg viewBox="0 0 400 290" class="scene">
            <defs>
              <radialGradient id="se-sun"><stop offset="0" stop-color="#fde68a"/><stop offset=".45" stop-color="#fbbf24" stop-opacity=".9"/><stop offset="1" stop-color="#f59e0b" stop-opacity="0"/></radialGradient>
              <radialGradient id="se-ground" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#1e293b"/><stop offset="1" stop-color="#0b1020" stop-opacity="0"/></radialGradient>
              <linearGradient id="se-window" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fde68a"/><stop offset="1" stop-color="#f59e0b"/></linearGradient>
              <linearGradient id="se-window-day" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e0f2fe"/><stop offset=".55" stop-color="#7dd3fc"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient>
              <radialGradient id="se-ground-day" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#64748b" stop-opacity=".45"/><stop offset="1" stop-color="#64748b" stop-opacity="0"/></radialGradient>
              <linearGradient id="se-shine" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".35"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
              <filter id="se-glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
              <clipPath id="se-roof-clip"><polygon points="${pts(roofPlane)}"/></clipPath>
            </defs>

            <g class="night">${stars}<path class="moon" d="M50 86 a16 16 0 1 0 14 24 a13 13 0 1 1 -14 -24z"/></g>
            <g class="day">
              <circle class="sun-glow" cx="50" cy="100" r="34" fill="url(#se-sun)"/>
              <g class="sun-rays">${Array.from({ length: 12 }, (_, i) => `<line x1="50" y1="74" x2="50" y2="80" transform="rotate(${i * 30} 50 100)"/>`).join("")}</g>
              <circle cx="50" cy="100" r="12" fill="#fbbf24"/>
            </g>

            <ellipse class="ground-shadow" cx="215" cy="262" rx="190" ry="30"/>
            <polygon class="roof-back" points="${pts(roofBack)}"/>
            <polygon class="wall-side" points="${pts([A, A2, add(A2, D), add(A, D)])}"/>
            <polygon class="wall-front" points="${pts([A, B, B2, APEX, A2])}"/>

            <polygon class="window" points="${pts(quad(side, 0.32, 0.47, 0.34, 0.76))}"/>
            <polygon class="window" points="${pts(quad(side, 0.6, 0.75, 0.34, 0.76))}"/>
            <polygon class="window big" points="${pts(quad(front, 0.55, 0.86, 0.3, 0.82))}"/>
            <polygon class="door" points="${pts(quad(front, 0.14, 0.36, 0, 0.62))}"/>

            <polygon class="roof" points="${pts(roofPlane)}"/>
            <g class="panels">${panels}</g>
            <g clip-path="url(#se-roof-clip)"><rect class="shine" x="40" y="60" width="60" height="140" fill="url(#se-shine)"/></g>

            <path class="flow-base" d="${path(flowRoof)}"/>
            <path class="flow" d="${path(flowRoof)}"/>
            <polygon class="inverter" points="${pts(quad(side, 0.065, 0.135, 0.36, 0.62))}"/>
            <circle class="led" cx="${side(0.1, 0.56)[0]}" cy="${side(0.1, 0.56)[1]}" r="1.6"/>
            <path class="flow-base ground" d="${path(flowGround)}"/>
            <path class="flow ground" d="${path(flowGround)}"/>
          </svg>
          <div class="chips">
            <div class="chip" data-entity="power">
              <div class="icon sun-icon"></div>
              <div><div class="ct">Solar</div><div class="cv power-text">—</div></div>
            </div>
            <div class="chip" data-entity="array">
              <div class="icon grid-icon"></div>
              <div><div class="ct">Painéis</div><div class="cv array-text">—</div></div>
            </div>
          </div>
        </div>
      </ha-card>`;

    this.querySelectorAll("[data-entity]").forEach((el) =>
      el.addEventListener("click", () => this._moreInfo(this._entities[el.dataset.entity]))
    );
    this.querySelectorAll(".panel").forEach((el) =>
      el.addEventListener("click", () =>
        this._moreInfo(this._entities.optimizers[Number(el.dataset.i)].power)
      )
    );
  }

  _num(entityId) {
    const st = entityId && this._hass.states[entityId];
    const v = st ? parseFloat(st.state) : NaN;
    return Number.isFinite(v) ? v : null;
  }

  _fmtEnergy(entityId) {
    const st = entityId && this._hass.states[entityId];
    const v = this._num(entityId);
    if (v === null) return "—";
    let unit = st.attributes.unit_of_measurement || "kWh";
    let value = v;
    if (unit === "Wh") {
      value = v / 1000;
      unit = "kWh";
    }
    if (unit === "kWh" && value >= 1000) {
      value /= 1000;
      unit = "MWh";
    }
    const digits = unit === "MWh" ? 2 : value >= 100 ? 0 : 1;
    return `${value.toLocaleString(this._locale(), { maximumFractionDigits: digits })} ${unit}`;
  }

  _fmtPower(w) {
    if (w === null) return "—";
    if (w >= 1000) {
      return `${(w / 1000).toLocaleString(this._locale(), { maximumFractionDigits: 2 })} kW`;
    }
    return `${Math.round(w).toLocaleString(this._locale())} W`;
  }

  _locale() {
    return (this._hass.locale && this._hass.locale.language) || this._hass.language || "pt-BR";
  }

  _update() {
    if (!this._built || !this._hass) return;
    const e = this._entities;
    const power = this._num(e.power);
    const maxPower =
      this._config.max_power ||
      (e.power && this._hass.states[e.power] && this._hass.states[e.power].attributes.max_ac_power) ||
      5000;
    const ratio = power ? Math.min(power / maxPower, 1) : 0;
    const sun = this._hass.states["sun.sun"];
    const isDay = sun ? sun.state === "above_horizon" : ratio > 0;

    const card = this.querySelector(".wrap");
    this.querySelector("ha-card").classList.toggle("is-day", isDay);
    card.classList.toggle("producing", ratio > 0);
    card.style.setProperty("--flow-dur", `${(2.6 - 2.1 * ratio).toFixed(2)}s`);
    card.style.setProperty("--glow", (0.25 + 0.75 * ratio).toFixed(2));

    for (const key of ["today", "month", "total"]) {
      this.querySelector(`[data-entity="${key}"] .v`).textContent = this._fmtEnergy(e[key]);
    }

    const stateObj = e.state && this._hass.states[e.state];
    const labels = { producing: "Produzindo", idle: "Parado", not_communicating: "Sem comunicação" };
    const stateText = stateObj ? labels[stateObj.state] || stateObj.state : ratio > 0 ? "Produzindo" : "Parado";
    this.querySelector(".state-text").textContent = stateText;

    this.querySelector(".power-text").textContent =
      `${this._fmtPower(power)} · ${Math.round(ratio * 100)}%`;

    const panelMax = this._config.panel_max_power;
    const watts = e.optimizers.map((o) => this._num(o.power));
    const valid = watts.filter((w) => w !== null);
    const reported = this._num(e.array);
    const arrayPower =
      reported !== null ? reported : valid.length ? valid.reduce((s, w) => s + w, 0) : null;
    const avg = valid.length ? valid.reduce((s, w) => s + w, 0) / valid.length : null;
    this.querySelector(".array-text").textContent =
      `${this._fmtPower(arrayPower)}${avg !== null ? ` · méd. ${Math.round(avg)} W` : ""}`;

    this.querySelectorAll(".panel").forEach((el) => {
      const i = Number(el.dataset.i);
      const o = e.optimizers[i];
      const w = watts[i];
      const k = w === null ? 0 : Math.min(w / panelMax, 1);
      el.style.fill = mix(PANEL_COLD, PANEL_HOT, Math.sqrt(k));
      el.style.filter = k > 0.15 ? "url(#se-glow)" : "none";
      const temp = this._num(o.temperature);
      const today = o.daily ? this._fmtEnergy(o.daily) : null;
      el.querySelector("title").textContent = [
        o.name || o.power,
        `${this._fmtPower(w)}`,
        temp !== null ? `máx. ${temp} °C` : null,
        today ? `hoje ${today}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
    });
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { bubbles: true, composed: true, detail: { entityId } })
    );
  }
}

const STYLE = `
  /* Tema noturno (padrão); .is-day troca as variáveis para o tema claro */
  ha-card { overflow: hidden; border-radius: 24px; border: none; position: relative;
    --se-text: #e5e7eb; --se-muted: #94a3b8; --se-line: #94a3b8;
    --se-chip: rgba(255,255,255,.07); --se-chip-hover: rgba(255,255,255,.12);
    --se-roof-back: #1f2937; --se-wall-side: #1e2535; --se-wall-front: #283143;
    --se-roof: #111827; --se-roof-stroke: #374151; --se-door: #111827;
    --se-window: url(#se-window); --se-ground: url(#se-ground); --se-flow-base: #334155;
    --se-panel-stroke: #0b1220;
    background: radial-gradient(120% 90% at 70% 0%, #1e293b 0%, #0f172a 45%, #070b16 100%); }
  ha-card.is-day {
    --se-text: #0f172a; --se-muted: #475569; --se-line: #64748b;
    --se-chip: rgba(255,255,255,.6); --se-chip-hover: rgba(255,255,255,.85);
    --se-roof-back: #94a3b8; --se-wall-side: #cbd5e1; --se-wall-front: #e2e8f0;
    --se-roof: #334155; --se-roof-stroke: #475569; --se-door: #64748b;
    --se-window: url(#se-window-day); --se-ground: url(#se-ground-day); --se-flow-base: #94a3b8;
    --se-panel-stroke: #1e293b; }
  /* Céu diurno numa camada própria para a troca de tema ser suave */
  ha-card::before { content: ""; position: absolute; inset: 0; opacity: 0; transition: opacity 1.5s;
    background: linear-gradient(180deg, #7dd3fc 0%, #bae6fd 40%, #e0f2fe 75%, #f8fafc 100%); }
  ha-card.is-day::before { opacity: 1; }
  .wrap { color: var(--se-text); padding: 20px 20px 16px; position: relative; z-index: 1;
    transition: color 1.5s; --flow-dur: 2s; --glow: .3;
    font-family: var(--paper-font-body1_-_font-family, inherit); }
  .header { display: flex; align-items: center; justify-content: space-between; }
  .title { font-size: 28px; font-weight: 700; letter-spacing: -.5px; }
  .chip-state { display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 12px;
    background: var(--se-chip); font-size: 13px; transition: background 1.5s; }
  .chip-state .dot { width: 8px; height: 8px; border-radius: 50%; background: #64748b; }
  .producing .chip-state .dot { background: #22c55e; box-shadow: 0 0 8px #22c55e; animation: pulse 2s infinite; }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 18px; }
  .stat { cursor: pointer; position: relative; padding-left: 10px; }
  .stat .v { font-size: 18px; font-weight: 700; white-space: nowrap; }
  .stat .l { font-size: 12px; color: var(--se-muted); transition: color 1.5s; }
  .stat .line { position: absolute; left: 0; top: 4px; width: 1px; height: 70px;
    background: linear-gradient(var(--se-line), transparent); opacity: .5; }
  .scene { width: 100%; display: block; margin-top: -24px; }
  .scene polygon, .scene ellipse, .scene path { transition: fill 1.5s, stroke 1.5s; }
  .ground-shadow { fill: var(--se-ground); }
  .roof-back { fill: var(--se-roof-back); }
  .wall-side { fill: var(--se-wall-side); }
  .wall-front { fill: var(--se-wall-front); }
  .roof { fill: var(--se-roof); stroke: var(--se-roof-stroke); stroke-width: .6; }
  .door { fill: var(--se-door); stroke: var(--se-roof-stroke); stroke-width: .6; }
  .window { fill: var(--se-window); opacity: calc(.15 + var(--glow) * .85); }
  .is-day .window { opacity: .9; }
  .window.big { filter: url(#se-glow); }
  .is-day .window.big { filter: none; }
  .panel { stroke: var(--se-panel-stroke); stroke-width: .5; cursor: pointer; transition: fill 1.2s ease; }
  .panel:hover { stroke: #e0f2fe; stroke-width: 1; }
  .shine { opacity: 0; }
  .producing .shine { opacity: 1; animation: shine 6s ease-in-out infinite; }
  .inverter { fill: #f1f5f9; stroke: #64748b; stroke-width: .5; }
  .led { fill: #64748b; }
  .producing .led { fill: #22c55e; filter: url(#se-glow); animation: pulse 1.6s infinite; }
  .flow-base { fill: none; stroke: var(--se-flow-base); stroke-width: 1.4; stroke-linejoin: round; }
  .flow { fill: none; stroke: #fbbf24; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round;
    stroke-dasharray: 3 9; opacity: 0; filter: url(#se-glow); }
  .is-day .flow { stroke: #f59e0b; }
  .flow.ground { stroke: #38bdf8; }
  .is-day .flow.ground { stroke: #0284c7; }
  .producing .flow { opacity: 1; animation: flow var(--flow-dur) linear infinite; }
  .day, .night { transition: opacity 1.5s; }
  .day { opacity: 0; } .is-day .day { opacity: 1; } .is-day .night { opacity: 0; }
  .sun-glow { transform-origin: 50px 100px; animation: breathe 4s ease-in-out infinite; }
  .sun-rays line { stroke: #f59e0b; stroke-width: 2; stroke-linecap: round; }
  .sun-rays { transform-origin: 50px 100px; animation: spin 30s linear infinite; }
  .moon { fill: #e2e8f0; opacity: .9; }
  .star { fill: #e2e8f0; animation: twinkle 3s ease-in-out infinite; }
  .chips { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 4px; }
  .chip { display: flex; align-items: center; gap: 10px; padding: 12px; border-radius: 14px;
    background: var(--se-chip); cursor: pointer; transition: background 1.5s; }
  .chip:hover { background: var(--se-chip-hover); }
  .ct { font-size: 14px; font-weight: 600; }
  .cv { font-size: 12px; color: var(--se-muted); transition: color 1.5s; }
  .icon { width: 22px; height: 22px; flex: none; border-radius: 50%; }
  .sun-icon { background: radial-gradient(circle, #fbbf24 40%, rgba(251,191,36,.25) 41%, transparent 70%); }
  .producing .sun-icon { animation: breathe 3s ease-in-out infinite; }
  .grid-icon { border-radius: 4px; background:
    linear-gradient(90deg, transparent 45%, #0f172a 45% 55%, transparent 55%),
    linear-gradient(transparent 45%, #0f172a 45% 55%, transparent 55%), #38bdf8; }
  @keyframes flow { to { stroke-dashoffset: -24; } }
  @keyframes pulse { 50% { opacity: .35; } }
  @keyframes breathe { 50% { transform: scale(1.12); } }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes twinkle { 50% { opacity: .2; } }
  @keyframes shine { 0% { transform: translateX(-80px); } 60%, 100% { transform: translateX(260px); } }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
`;

if (!customElements.get("solaredge-energy-card")) {
  customElements.define("solaredge-energy-card", SolarEdgeEnergyCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "solaredge-energy-card",
    name: "SolarEdge Energy",
    description: "Casa animada com produção em tempo real e potência por painel.",
    preview: true,
  });
  console.info(`%c SOLAREDGE-ENERGY-CARD %c v${CARD_VERSION} `, "background:#f59e0b;color:#000", "");
}
