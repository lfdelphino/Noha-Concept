// What the robot is doing, in words and visuals shared by the dashboard and the Pi's status board.
import {h, icon, fmt, replace} from "./lib.js";

// state -> [headline, orb mode, tone]
const STATES = {
  aguardando_movimento: ["Pronta para receber visitantes", "idle", "ok"],
  sensor_desligado: ["Saudações por movimento desligadas", "off", "neutral"],
  aquecendo_sensor: ["Aquecendo o sensor", "warming", "neutral"],
  cumprimentando: ["Cumprimentando um visitante", "speaking", "ok"],
  conectando: ["Abrindo a conversa", "connecting", "ok"],
  conversando: ["Conversando com um visitante", "listening", "ok"],
  orientando_wifi: ["Explicando como configurar o Wi-Fi", "speaking", "warn"],
  recuperando_audio_ou_conexao: ["Recuperando a conexão", "connecting", "warn"],
  gpio_indisponivel: ["Sensor de presença sem leitura", "error", "error"],
  audio_indisponivel: ["Microfone ou alto-falante ausente", "error", "error"],
  erro_api: ["A OpenAI recusou a conversa", "error", "error"],
  sem_conexao: ["Sem conexão com a OpenAI", "error", "error"],
  chave_ausente: ["Falta a chave da OpenAI", "error", "error"],
  orcamento_esgotado: ["Limite diário de custo atingido", "warn", "warn"],
  parado: ["Serviço de voz parado", "error", "error"],
};
const DETAILS = {
  aguardando_movimento: "Noha cumprimenta quando alguém chega perto.",
  sensor_desligado: "O sensor continua ativo, mas Noha não cumprimenta. Ligue em Saudar por movimento.",
  aquecendo_sensor: "O sensor se estabiliza por alguns segundos depois de ligar.",
  cumprimentando: "Saudação gravada, sem custo. A conversa começa se a pessoa responder.",
  conectando: "A pessoa respondeu. Conectando à OpenAI…",
  conversando: "Conversa em andamento.",
  orientando_wifi: "Sem internet: Noha explica pelo alto-falante como configurar o Wi-Fi.",
  recuperando_audio_ou_conexao: "Uma nova tentativa começa em instantes.",
  parado: "O serviço está reiniciando ou foi interrompido.",
};
export const GATES = {aquecendo: "Aquecendo", conversando: "Em atendimento", rearmando: "Rearmando",
  confirmando: "Confirmando presença", pronto: "Pronto", desligado: "Desligado"};

export function describe(status) {
  const voice = status?.voice || {};
  if (!status || voice.stale || !voice.state) {
    return {title: "Sem sinal do robô", detail: "O serviço de voz não responde. Ele pode estar reiniciando.", mode: "error", tone: "error"};
  }
  if (voice.updating) return {title: "Aplicando alterações", detail: "Noha reinicia a voz em alguns segundos.", mode: "connecting", tone: "neutral"};
  let [title, mode, tone] = STATES[voice.state] || [voice.state.replaceAll("_", " "), "idle", "neutral"];
  if (voice.state === "conversando" && voice.speaking) mode = "speaking";
  let detail = voice.detail || DETAILS[voice.state] || "";
  if (voice.state === "aguardando_movimento" && voice.last_motion) {
    detail = `Último movimento ${fmt.ago((voice.updated || Date.now() / 1000) - voice.last_motion)}.`;
  }
  return {title, detail, mode, tone};
}

export function orb() {
  return h("div", {class: "orb", "data-mode": "idle", "aria-hidden": "true"},
    h("i", {class: "ring"}), h("i", {class: "ring"}), h("i", {class: "ring"}),
    h("span", {class: "core"}, h("b"), h("b"), h("b"), h("b"), h("b")));
}

export function setOrb(el, mode) {
  if (el.dataset.mode !== mode) el.dataset.mode = mode;
}

export function gateLabel(voice) {
  if (!voice || voice.stale || !GATES[voice.gate]) return "—";
  return voice.gate_seconds && ["aquecendo", "rearmando", "confirmando"].includes(voice.gate)
    ? `${GATES[voice.gate]} · ${voice.gate_seconds} s` : GATES[voice.gate];
}

// Motion detections of the last 10 minutes, positioned on the Pi's clock.
export function drawTimeline(el, voice, span = 600) {
  const now = voice?.updated || Date.now() / 1000, since = now - span;
  const visible = (voice?.motion_history || []).filter(([, end]) => (end ?? now) >= since);
  replace(el, visible.map(([start, end]) => {
    const from = Math.max(start, since), to = end ?? now;
    return h("i", {style: {left: `${100 * (from - since) / span}%`, width: `${Math.max(0.6, 100 * (to - from) / span)}%`},
      title: `${fmt.time(start)} – ${end ? fmt.time(end) : "agora"}`});
  }));
  const count = visible.length;
  el.setAttribute("aria-label", count ? `${count} ${count === 1 ? "detecção" : "detecções"} nos últimos 10 minutos` : "Nenhuma detecção nos últimos 10 minutos");
  return count;
}

const LEVEL_ICONS = {ok: "check", warn: "alert", error: "alert", info: "info"};
export function levelIcon(level, size = 16) {
  return h("span", {class: `level level-${level}`}, icon(LEVEL_ICONS[level] || "info", size));
}

export function problems(checks) {
  return (checks || []).filter(check => check.level === "error" || check.level === "warn");
}
