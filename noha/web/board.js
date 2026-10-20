// Status board for the Pi's own display: read-only, large type, always live. Settings live on phones.
import {h, icon, fmt, text, replace, meter, setMeter, api} from "./lib.js";
import {describe, orb, setOrb, gateLabel, drawTimeline, levelIcon} from "./robot.js";

const ESSENTIAL = ["voz", "internet", "chave", "audio", "sensor", "saudacoes", "custo"];
const root = document.getElementById("root");

function stat(iconName, label) {
  const value = h("p", {class: "board-value"}, "—"), sub = h("p", {class: "board-sub"}, "");
  return {el: h("article", {class: "board-stat"}, h("p", {class: "board-label"}, icon(iconName, 22), label), value, sub), value, sub};
}

const bigOrb = orb();
const title = h("h1", {class: "board-title"}, "Conectando…");
const detail = h("p", {class: "board-detail"}, "");
const clock = h("time", {class: "board-clock"}, "");
const live = h("span", {class: "board-live"}, h("span", {class: "dot"}), "Ao vivo");
const visits = stat("users", "Visitas hoje"), talks = stat("chat", "Conversas"), cost = stat("coin", "Custo hoje");
const budget = meter(0, 1);
cost.el.append(budget);
const timeline = h("div", {class: "timeline timeline-large", role: "img"});
const motion = h("p", {class: "board-sub"}, "");
const gate = h("span", {class: "badge badge-neutral"}, "—");
const checks = h("ul", {class: "board-checks"});
const address = h("strong", {}, "http://noha.local");
const banner = h("p", {class: "board-alert", hidden: true});

replace(root, h("div", {class: "board"},
  h("header", {class: "board-head"}, h("img", {src: "/logo.png", alt: "Noha", class: "logo logo-invert"}),
    h("span", {class: "board-name"}, "Painel do robô"), h("span", {class: "board-spacer"}), live, clock),
  banner,
  h("section", {class: "board-state"}, bigOrb, h("div", {}, title, detail)),
  h("section", {class: "board-stats"}, visits.el, talks.el, cost.el),
  h("section", {class: "board-panel"}, h("div", {class: "board-panel-head"}, h("p", {class: "board-label"}, icon("radar", 22), "Sensor de presença · 10 min"), gate),
    timeline, motion),
  checks,
  h("footer", {class: "board-foot"}, icon("settings", 20), h("span", {}, "Ajustes pelo celular ou computador: "), address)));

function tick() {
  text(clock, new Date().toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"}));
}
tick();
setInterval(tick, 10000);

function render(status, available) {
  live.classList.toggle("off", !available);
  text(live.lastChild, available ? "Ao vivo" : "Sem resposta");
  if (!status) return;
  const view = describe(status);
  text(title, view.title);
  text(detail, view.detail);
  setOrb(bigOrb, view.mode);
  root.firstChild.dataset.tone = view.tone;
  const today = status.today || {};
  text(visits.value, fmt.number(today.visits ?? 0));
  text(visits.sub, today.visits ? `${fmt.number(today.conversations)} conversaram` : "Ninguém ainda");
  text(talks.value, fmt.number(today.conversations ?? 0));
  text(talks.sub, today.conversations ? `${fmt.duration(today.duration_seconds || 0)} no total` : "—");
  const limit = status.budget?.limit || 0;
  text(cost.value, fmt.usd(today.cost_usd ?? 0));
  text(cost.sub, limit ? `de ${fmt.usd(limit)} por dia` : "sem limite diário");
  budget.hidden = !limit;
  setMeter(budget, today.cost_usd || 0, limit || 1);
  const voice = status.voice || {};
  drawTimeline(timeline, voice);
  text(gate, gateLabel(voice));
  text(motion, voice.stale ? "Sem sinal do robô" : voice.motion ? "Movimento agora" :
    voice.last_motion ? `Último movimento ${fmt.ago((voice.updated || 0) - voice.last_motion)}` : "Nenhum movimento desde que o robô ligou");
  const byId = Object.fromEntries((status.checks || []).map(check => [check.id, check]));
  replace(checks, ESSENTIAL.filter(id => byId[id]).map(id => {
    const check = byId[id];
    return h("li", {class: "board-check", dataset: {level: check.level}, title: check.detail}, levelIcon(check.level, 20), h("span", {}, check.label));
  }));
  const worst = (status.checks || []).find(check => check.level === "error");
  banner.hidden = !worst;
  if (worst) replace(banner, icon("alert", 22), h("span", {}, h("strong", {}, `${worst.label}: `), worst.detail));
  if (status.local_url) text(address, status.local_url);
}

let stream;
function connect() {
  stream?.close();
  stream = new EventSource("/api/events");
  stream.onmessage = event => {
    const data = JSON.parse(event.data);
    render(data.status, data.available);
  };
  stream.onerror = async () => {
    render(null, false);
    try { await api("session"); } catch (failure) {
      if (failure.status === 401) {
        stream.close();
        text(title, "Este painel abre sozinho na tela do robô");
        text(detail, "Para acompanhar e ajustar a Noha, entre pelo celular ou computador.");
        setOrb(bigOrb, "off");
      }
    }
  };
}
connect();
