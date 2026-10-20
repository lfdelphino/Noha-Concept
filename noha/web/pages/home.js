// Início: is Noha OK right now, and the controls used every day.
import {h, icon, api, fmt, toast, text, replace, busy, switchControl, setSwitch, meter, setMeter, greeting} from "../lib.js";
import {describe, orb, setOrb, gateLabel, drawTimeline, levelIcon, problems} from "../robot.js";

function kpi(iconName, label) {
  const value = h("p", {class: "kpi-value"}, "—");
  const sub = h("p", {class: "kpi-sub"}, "");
  const extra = h("div", {class: "kpi-extra"});
  const card = h("article", {class: "card kpi"}, h("div", {class: "kpi-head"}, h("span", {class: "kpi-icon"}, icon(iconName, 18)), h("span", {}, label)), value, sub, extra);
  return {card, value, sub, extra};
}

function sparkline(values) {
  const max = Math.max(1, ...values);
  return h("div", {class: "sparkline", "aria-hidden": "true"}, values.map(v => h("i", {style: {height: `${Math.max(6, 100 * v / max)}%`}, class: v ? "" : "zero"})));
}

export function mount(root, ctx) {
  // Status hero
  const heroOrb = orb();
  const title = h("h2", {class: "hero-title"}, "Conectando ao robô…");
  const detail = h("p", {class: "hero-detail"}, "");
  const meta = h("dl", {class: "hero-meta"});
  const hero = h("section", {class: "hero", "aria-live": "polite"},
    h("div", {class: "hero-body"}, h("p", {class: "eyebrow"}, "AGORA"), title, detail), heroOrb, meta);

  // Everyday controls
  const presence = switchControl({checked: true, label: "Saudar por movimento", onChange: async enabled => {
    try {
      const result = await api("presence", {enabled});
      toast(result.message);
      return true;
    } catch (failure) { toast(failure.message, "error"); return false; }
  }});
  const testButton = h("button", {class: "btn btn-secondary", type: "button"}, icon("play", 18), "Testar saudação");
  testButton.addEventListener("click", () => busy(testButton, async () => {
    try { toast((await api("trigger", {})).message); } catch (failure) { toast(failure.message, "error"); }
  }));
  const volumeValue = h("output", {class: "volume-value"}, "—");
  const volume = h("input", {type: "range", min: "0", max: "100", step: "5", class: "range", disabled: true, "aria-label": "Volume do alto-falante"});
  const volumeIcon = h("span", {class: "volume-icon"}, icon("volume", 20));
  let volumeTimer;
  volume.addEventListener("input", () => {
    text(volumeValue, `${volume.value}%`);
    replace(volumeIcon, icon(Number(volume.value) ? "volume" : "mute", 20));
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(async () => {
      try { await api("volume", {level: Number(volume.value)}); } catch (failure) { toast(failure.message, "error"); }
    }, 300);
  });
  api("volume").then(result => {
    volume.disabled = false;
    volume.value = result.muted ? 0 : result.level;
    text(volumeValue, `${volume.value}%`);
  }).catch(() => text(volumeValue, "Indisponível"));
  const controls = h("section", {class: "card controls"},
    h("h3", {class: "card-title"}, "Controles"),
    h("div", {class: "control-row"},
      h("div", {class: "control-text"}, h("strong", {}, "Saudar por movimento"),
        h("span", {class: "help"}, "Desligado, o sensor continua visível, mas Noha não cumprimenta.")), presence),
    h("div", {class: "control-row"},
      h("div", {class: "control-text"}, h("strong", {}, "Testar agora"),
        h("span", {class: "help"}, "Noha cumprimenta como se alguém tivesse chegado.")), testButton),
    h("div", {class: "control-row control-volume"},
      h("div", {class: "control-text"}, h("strong", {}, "Volume"), h("span", {class: "help"}, "Alto-falante da Noha.")),
      h("div", {class: "volume"}, volumeIcon, volume, volumeValue)));

  // Today
  const visits = kpi("users", "Visitas hoje");
  const talks = kpi("chat", "Conversas");
  const cost = kpi("coin", "Custo hoje");
  const time = kpi("clock", "Tempo conversando");
  const budget = meter(0, 1);
  cost.extra.append(budget);

  // Sensor
  const dot = h("span", {class: "live-dot"});
  const motionState = h("p", {class: "sensor-state"}, "—");
  const motionDetail = h("p", {class: "help"}, "");
  const gate = h("span", {class: "badge badge-neutral"}, "—");
  const timeline = h("div", {class: "timeline", role: "img"});
  const motionCount = h("span", {}, "");
  const sensor = h("section", {class: "card sensor"},
    h("div", {class: "card-head"}, h("h3", {class: "card-title"}, icon("radar", 18), "Sensor de presença"), gate),
    h("div", {class: "sensor-now"}, dot, h("div", {}, motionState, motionDetail)),
    timeline, h("div", {class: "timeline-axis"}, h("span", {}, "10 min atrás"), motionCount, h("span", {}, "agora")),
    h("a", {class: "link", href: "#ajustes/sensor"}, "Ajustar sensibilidade", icon("chevron", 16)));

  // Diagnostics
  const summary = h("span", {class: "badge badge-neutral"}, "—");
  const list = h("ul", {class: "checks"});
  const diagnostics = h("section", {class: "card"},
    h("div", {class: "card-head"}, h("h3", {class: "card-title"}, icon("shield", 18), "Diagnóstico"), summary), list,
    h("a", {class: "link", href: "#sistema/diagnostico"}, "Ver detalhes", icon("chevron", 16)));

  replace(root,
    h("header", {class: "page-head"}, h("div", {}, h("p", {class: "eyebrow"}, fmt.today()), h("h1", {}, `${greeting()}!`))),
    h("div", {class: "grid"},
      h("div", {class: "span-8"}, hero), h("div", {class: "span-4"}, controls),
      h("div", {class: "span-12 kpis"}, visits.card, talks.card, cost.card, time.card),
      h("div", {class: "span-7"}, sensor), h("div", {class: "span-5"}, diagnostics)));

  return {
    update(status) {
      const voice = status?.voice || {};
      const view = describe(status);
      text(title, view.title);
      text(detail, view.detail);
      setOrb(heroOrb, view.mode);
      hero.dataset.tone = view.tone;
      replace(meta, [["Modelo", status?.model], ["Voz", status?.voice_name ? status.voice_name[0].toUpperCase() + status.voice_name.slice(1) : null],
        ["Versão", voice.version], ["Ligada há", fmt.duration(status?.system?.uptime_seconds)]]
        .filter(([, value]) => value).map(([label, value]) => h("div", {}, h("dt", {}, label), h("dd", {}, value))));

      setSwitch(presence, status?.presence !== false);
      testButton.disabled = testButton.classList.contains("is-busy") || !!voice.stale || !!voice.busy || !!voice.updating;

      const today = status?.today || {};
      text(visits.value, fmt.number(today.visits ?? 0));
      text(visits.sub, today.visits ? `${fmt.number(today.conversations)} conversaram` : "Ninguém ainda");
      replace(visits.extra, sparkline(today.hourly || []));
      text(talks.value, fmt.number(today.conversations ?? 0));
      text(talks.sub, today.visits ? `${fmt.percent(today.conversations / today.visits)} das visitas` : "—");
      const limit = status?.budget?.limit || 0;
      text(cost.value, fmt.usd(today.cost_usd ?? 0));
      text(cost.sub, limit ? `de ${fmt.usd(limit)} por dia` : "Sem limite diário");
      budget.hidden = !limit;
      setMeter(budget, today.cost_usd || 0, limit || 1);
      text(time.value, fmt.duration(today.duration_seconds || 0));
      text(time.sub, today.conversations ? `média de ${fmt.duration((today.duration_seconds || 0) / today.conversations)}` : "—");

      const fresh = !voice.stale;
      dot.classList.toggle("on", fresh && !!voice.motion);
      text(motionState, !fresh ? "Sem sinal do robô" : voice.gpio_ready === false ? "Sensor sem leitura" : voice.motion ? "Movimento agora" : "Sem movimento");
      text(motionDetail, voice.last_motion ? `Último movimento ${fmt.ago((voice.updated || 0) - voice.last_motion)} · ${fmt.time(voice.last_motion)}` : "Nenhum movimento desde que o robô ligou");
      text(gate, gateLabel(voice));
      const count = drawTimeline(timeline, voice);
      text(motionCount, count ? `${count} ${count === 1 ? "detecção" : "detecções"}` : "nenhuma detecção");

      const checks = status?.checks || [];
      const issues = problems(checks);
      const errors = issues.filter(check => check.level === "error").length;
      summary.className = `badge badge-${errors ? "error" : issues.length ? "warn" : "ok"}`;
      text(summary, errors ? `${errors} ${errors === 1 ? "problema" : "problemas"}` : issues.length ? `${issues.length} ${issues.length === 1 ? "aviso" : "avisos"}` : "Tudo certo");
      replace(list, checks.slice(0, 7).map(check => {
        const body = [levelIcon(check.level), h("span", {class: "check-text"}, h("strong", {}, check.label), h("span", {}, check.detail))];
        return h("li", {}, check.action ? h("a", {href: `#${check.action}`, class: "check-row"}, body, icon("chevron", 16)) : h("div", {class: "check-row"}, body));
      }));
    },
  };
}
