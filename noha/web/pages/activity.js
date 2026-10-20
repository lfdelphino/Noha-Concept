// Atividade: visits, conversations and cost over time. Numbers only: no audio or transcripts are kept.
import {h, icon, api, fmt, text, replace, busy, empty} from "../lib.js";

const PERIODS = [[1, "Hoje"], [7, "7 dias"], [30, "30 dias"]];
export const OUTCOMES = {
  conversou: ["Conversaram", "ok"], sem_resposta: ["Só passaram", "neutral"], wifi: ["Ajuda com o Wi-Fi", "info"],
  falha: ["Falhas", "error"], sem_chave: ["Sem chave OpenAI", "warn"],
};
// One visit in the recent list.
const ROW = {conversou: "Conversou", sem_resposta: "Só passou", wifi: "Ajuda com o Wi-Fi", falha: "Falha", sem_chave: "Sem chave"};
const TOKENS = [["input_tokens", "Tokens de entrada"], ["output_tokens", "Tokens de saída"], ["cached_tokens", "Tokens em cache"]];

function kpi(iconName, label) {
  const value = h("p", {class: "kpi-value"}, "—");
  const sub = h("p", {class: "kpi-sub"}, "");
  return {card: h("article", {class: "card kpi"}, h("div", {class: "kpi-head"}, h("span", {class: "kpi-icon"}, icon(iconName, 18)), h("span", {}, label)), value, sub), value, sub};
}

function chart(series, days, caption) {
  const max = Math.max(1, ...series.map(bar => bar.visits));
  const step = days === 1 ? 3 : days === 7 ? 1 : 5;
  const describeBar = bar => `${days === 1 ? bar.label : bar.label}: ${bar.visits} ${bar.visits === 1 ? "visita" : "visitas"}, ` +
    `${bar.conversations} ${bar.conversations === 1 ? "conversou" : "conversaram"}, ${fmt.usdFine(bar.cost_usd)}`;
  const columns = series.map((bar, index) => h("button", {type: "button", class: "chart-col", "aria-label": describeBar(bar),
    onclick: () => text(caption, describeBar(bar))},
    h("span", {class: "chart-bar visits", style: {"--h": bar.visits / max}}),
    h("span", {class: "chart-bar talks", style: {"--h": bar.conversations / max}}),
    h("span", {class: "chart-label"}, index % step === 0 || index === series.length - 1 ? bar.label : "")));
  return h("div", {class: "chart-wrap"},
    h("div", {class: "chart-scale"}, h("span", {}, fmt.number(max)), h("span", {}, "0")),
    h("div", {class: "chart", style: {"--bars": series.length}}, columns));
}

export function mount(root) {
  let days = 1;
  const segmented = h("div", {class: "segmented", role: "group", "aria-label": "Período"},
    PERIODS.map(([value, label]) => h("button", {type: "button", "aria-pressed": String(value === days), dataset: {days: value},
      onclick: () => { days = value; for (const b of segmented.children) b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days)); load(); }}, label)));
  const refresh = h("button", {class: "btn btn-ghost", type: "button"}, icon("refresh", 18), "Atualizar");
  refresh.addEventListener("click", () => busy(refresh, load));

  const visits = kpi("users", "Visitas");
  const talks = kpi("chat", "Conversaram");
  const cost = kpi("coin", "Custo");
  const time = kpi("clock", "Tempo conversando");
  const chartTitle = h("h3", {class: "card-title"}, icon("activity", 18), "Visitas");
  const chartBody = h("div", {class: "chart-host"});
  const caption = h("p", {class: "help chart-caption"}, "Toque em uma barra para ver os detalhes.");
  const outcomeBar = h("div", {class: "stack"});
  const outcomeList = h("ul", {class: "legend"});
  const visitList = h("ul", {class: "rows"});
  const models = h("div", {class: "models"});
  const error = h("p", {class: "form-error", role: "alert"});

  replace(root,
    h("header", {class: "page-head"},
      h("div", {}, h("p", {class: "eyebrow"}, "ATIVIDADE"), h("h1", {}, "Como foram as visitas"),
        h("p", {class: "muted"}, "Contamos só números: nenhuma gravação ou transcrição é guardada.")),
      h("div", {class: "page-actions"}, segmented, refresh)),
    error,
    h("div", {class: "grid"},
      h("div", {class: "span-12 kpis"}, visits.card, talks.card, cost.card, time.card),
      h("section", {class: "card span-8"},
        h("div", {class: "card-head"}, chartTitle,
          h("div", {class: "chart-legend"}, h("span", {class: "key visits"}, "Visitas"), h("span", {class: "key talks"}, "Conversaram"))),
        chartBody, caption),
      h("section", {class: "card span-4"},
        h("div", {class: "card-head"}, h("h3", {class: "card-title"}, icon("users", 18), "Como terminaram")), outcomeBar, outcomeList),
      h("section", {class: "card span-7"},
        h("div", {class: "card-head"}, h("h3", {class: "card-title"}, icon("list", 18), "Visitas recentes")), visitList),
      h("section", {class: "card span-5"},
        h("div", {class: "card-head"}, h("h3", {class: "card-title"}, icon("sparkle", 18), "Por modelo")), models,
        h("p", {class: "help"}, "Custo estimado pelos preços publicados da OpenAI. Histórico de 90 dias."))));

  async function load() {
    try {
      const data = await api(`usage?days=${days}`);
      text(error, "");
      render(data);
    } catch (failure) { text(error, failure.message); }
  }

  function render(data) {
    const totals = data.totals, outcomes = data.outcomes || {};
    const talked = outcomes.conversou || 0;
    text(visits.value, fmt.number(totals.visits));
    text(visits.sub, days === 1 ? "desde a meia-noite" : `nos últimos ${days} dias`);
    text(talks.value, fmt.number(talked));
    text(talks.sub, totals.visits ? `${fmt.percent(talked / totals.visits)} das visitas` : "—");
    text(cost.value, fmt.usd(totals.cost_usd));
    text(cost.sub, talked ? `${fmt.usdFine(totals.cost_usd / talked)} por conversa` : "—");
    text(time.value, fmt.duration(totals.duration_seconds));
    text(time.sub, talked ? `média de ${fmt.duration(totals.duration_seconds / talked)}` : "—");
    text(chartTitle.lastChild, days === 1 ? "Visitas por hora" : "Visitas por dia");
    replace(chartBody, totals.visits ? chart(data.series, days, caption) : empty("Nenhuma visita neste período.", "users"));

    const total = Object.values(outcomes).reduce((sum, n) => sum + n, 0);
    replace(outcomeBar, total ? Object.entries(OUTCOMES).filter(([key]) => outcomes[key]).map(([key, [label, tone]]) =>
      h("span", {class: `stack-part tone-${tone}`, style: {width: `${100 * outcomes[key] / total}%`}, title: `${label}: ${outcomes[key]}`})) : []);
    outcomeBar.hidden = !total;
    replace(outcomeList, Object.entries(OUTCOMES).map(([key, [label, tone]]) =>
      h("li", {}, h("span", {class: `swatch tone-${tone}`}), h("span", {}, label), h("strong", {}, fmt.number(outcomes[key] || 0)))));

    replace(visitList, data.visits.length ? data.visits.slice(0, 25).map(visit => {
      const [, tone] = OUTCOMES[visit.outcome] || [visit.outcome, "neutral"];
      const label = ROW[visit.outcome] || visit.outcome;
      const length = visit.ended - visit.started;
      return h("li", {class: "row"},
        h("span", {class: "row-time"}, days === 1 ? fmt.time(visit.started) : fmt.datetime(visit.started)),
        h("span", {class: `badge badge-${tone}`}, label),
        h("span", {class: "row-meta"}, [fmt.duration(length), visit.replies ? `${visit.replies} ${visit.replies === 1 ? "resposta" : "respostas"}` : null,
          visit.cost_usd ? fmt.usdFine(visit.cost_usd) : "sem custo"].filter(Boolean).join(" · ")));
    }) : [h("li", {}, empty("As próximas visitas aparecem aqui.", "users"))]);

    // Only what each model reports: the speech model (saudações) has no conversations or tokens.
    replace(models, data.models.length ? data.models.map(model => h("div", {class: "model"},
      h("h4", {}, model.model),
      h("dl", {class: "facts"}, [model.conversations ? ["Conversas", fmt.number(model.conversations)] : null,
        model.greetings ? ["Saudações tocadas", fmt.number(model.greetings)] : null,
        ...TOKENS.filter(([key]) => model[key] !== null && model[key] !== undefined).map(([key, label]) => [label, fmt.number(model[key])]),
        ["Custo", fmt.usdFine(model.cost_usd)]].filter(Boolean).map(([label, value]) => h("div", {}, h("dt", {}, label), h("dd", {}, value))))))
      : empty("Nenhum uso registrado neste período.", "sparkle"));
  }

  load();
  const timer = setInterval(() => { if (!document.hidden && days === 1) load(); }, 30000);
  return {destroy: () => clearInterval(timer)};
}
