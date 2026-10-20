// Sistema: diagnostics, updates, remote access, network, hardware, password and events.
import {h, icon, api, fmt, toast, text, replace, busy, meter, setMeter, copy, empty} from "../lib.js";
import {levelIcon} from "../robot.js";

const UPDATE_STATES = {checking: "Verificando", staging: "Preparando a nova versão", waiting: "Aguardando o robô", installing: "Instalando",
  updated: "Atualizada", current: "Em dia", available: "Atualização disponível", rollback: "Versão anterior restaurada",
  deferred: "Adiada", offline: "Sem conexão", disabled: "Atualizações desativadas", installer_required: "Reexecute o instalador",
  error: "Falha"};
const NETWORK_MODES = {online: "Online", configuracao: "Modo de configuração Wi-Fi", iniciando: "Iniciando", buscando: "Buscando redes",
  conectando: "Conectando", reconectando: "Tentando redes salvas", recuperando: "Recuperando", indisponivel: "Indisponível"};
const TUNNEL = {online: ["Ativo", "ok"], reconectando: ["Reconectando", "warn"], configuracao_necessaria: ["Não configurado", "neutral"]};
const SERVICES = [["voice", "Voz"], ["network", "Rede"], ["web", "Painel"], ["admin", "Configurações"], ["ngrok", "Acesso remoto"], ["updates", "Atualizador"]];
const LEVELS = [["info", "Informação"], ["warning", "Avisos"], ["err", "Erros"], ["debug", "Depuração"]];

function section(id, iconName, title, ...children) {
  return h("section", {class: "card", id: `sec-${id}`},
    h("div", {class: "card-head"}, h("h2", {class: "card-title"}, icon(iconName, 20), title)), ...children);
}

function facts(rows) {
  return h("dl", {class: "facts"}, rows.map(([label, value]) => h("div", {}, h("dt", {}, label), h("dd", {}, value ?? "—"))));
}

export function mount(root, ctx) {
  // Diagnostics
  const checkList = h("ul", {class: "checks checks-full"});

  // Updates
  const updateFacts = h("div");
  const updateMessage = h("p", {class: "help"});
  const check = h("button", {class: "btn btn-secondary", type: "button"}, icon("refresh", 18), "Verificar agora");
  const install = h("button", {class: "btn btn-primary", type: "button"}, icon("download", 18), "Instalar atualização");
  for (const [button, operation] of [[check, "check-update"], [install, "install-update"]]) {
    button.addEventListener("click", () => busy(button, async () => {
      try { toast((await api(operation, {})).message); } catch (failure) { toast(failure.message, "error"); }
    }));
  }

  // Remote access
  const tunnelState = h("span", {class: "badge badge-neutral"}, "—");
  const tunnelLink = h("div", {class: "address"});
  const url = h("input", {id: "ngrok-url", class: "input", type: "url", inputmode: "url", placeholder: "https://seu-endereco.ngrok-free.app", autocomplete: "off"});
  const token = h("input", {id: "ngrok-token", class: "input", type: "password", autocomplete: "new-password", maxlength: "512", spellcheck: "false"});
  const tokenHelp = h("p", {class: "help"}, "Authtoken do agente, na sua conta ngrok.");
  const ngrokError = h("p", {class: "form-error", role: "alert"});
  const ngrokSave = h("button", {class: "btn btn-primary", type: "submit"}, "Salvar e reconectar");
  const ngrokForm = h("form", {class: "stack-form", novalidate: true},
    h("label", {class: "label", htmlFor: "ngrok-url"}, "Endereço HTTPS da sua conta"), url,
    h("label", {class: "label", htmlFor: "ngrok-token"}, "Authtoken do agente"), token, tokenHelp, ngrokError,
    h("p", {class: "help"}, "Salvar reconecta o acesso pela internet; o painel aberto por ele pode cair por alguns segundos."),
    h("div", {class: "form-actions"}, ngrokSave));
  ngrokForm.addEventListener("submit", async event => {
    event.preventDefault();
    await busy(ngrokSave, async () => {
      try {
        const result = await api("ngrok", {url: url.value, authtoken: token.value});
        token.value = "";
        text(ngrokError, "");
        toast(result.message);
      } catch (failure) { text(ngrokError, failure.message); }
    });
  });
  api("settings").then(settings => {
    url.value = settings.ngrok_url || "";
    token.placeholder = settings.ngrok_token_configured ? "Authtoken salvo · deixe vazio para manter" : "";
  }).catch(() => {});

  // Network and hardware
  const networkFacts = h("div");
  const gauges = h("div", {class: "gauges"});
  const gauge = (iconName, label) => {
    const value = h("p", {class: "gauge-value"}, "—"), sub = h("p", {class: "help"}, ""), bar = meter(0, 100);
    gauges.append(h("article", {class: "gauge"}, h("div", {class: "kpi-head"}, h("span", {class: "kpi-icon"}, icon(iconName, 18)), h("span", {}, label)), value, bar, sub));
    return {value, sub, bar};
  };
  const cpu = gauge("system", "Processador"), temperature = gauge("thermo", "Temperatura");
  const memory = gauge("memory", "Memória"), disk = gauge("disk", "Armazenamento");

  // Password
  const current = h("input", {id: "pw-current", class: "input", type: "password", autocomplete: "current-password", maxlength: "256"});
  const next = h("input", {id: "pw-next", class: "input", type: "password", autocomplete: "new-password", maxlength: "256"});
  const again = h("input", {id: "pw-again", class: "input", type: "password", autocomplete: "new-password", maxlength: "256"});
  const passwordError = h("p", {class: "form-error", role: "alert"});
  const passwordSave = h("button", {class: "btn btn-primary", type: "submit"}, "Alterar senha");
  const passwordForm = h("form", {class: "stack-form", novalidate: true},
    h("label", {class: "label", htmlFor: "pw-current"}, "Senha atual"), current,
    h("label", {class: "label", htmlFor: "pw-next"}, "Nova senha"), next, h("p", {class: "help"}, "Pelo menos 6 caracteres."),
    h("label", {class: "label", htmlFor: "pw-again"}, "Repita a nova senha"), again, passwordError,
    h("p", {class: "help"}, "Todos os acessos abertos são encerrados. No Pi, sem o painel: sudo noha dashboard-password."),
    h("div", {class: "form-actions"}, passwordSave));
  passwordForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (next.value.length < 6) { text(passwordError, "Use pelo menos 6 caracteres."); return; }
    if (next.value !== again.value) { text(passwordError, "As senhas não coincidem."); return; }
    await busy(passwordSave, async () => {
      try {
        await api("password", {current: current.value, password: next.value});
        toast("Senha alterada. Entre com a nova senha.");
        setTimeout(() => location.reload(), 1500);
      } catch (failure) { text(passwordError, failure.message); }
    });
  });

  // Events
  const service = h("select", {class: "input", id: "log-service"}, SERVICES.map(([value, label]) => h("option", {value}, label)));
  const level = h("select", {class: "input", id: "log-level"}, LEVELS.map(([value, label]) => h("option", {value}, label)));
  const logs = h("div", {class: "logs", tabindex: "0", "aria-label": "Eventos recentes"}, h("p", {class: "help"}, "Carregando eventos…"));
  const follow = () => { logs.dataset.cursor = ""; ctx.logs({service: service.value, level: level.value}); };
  service.addEventListener("change", follow);
  level.addEventListener("change", follow);

  replace(root,
    h("header", {class: "page-head"}, h("div", {}, h("p", {class: "eyebrow"}, "SISTEMA"), h("h1", {}, "Por dentro da Noha"),
      h("p", {class: "muted"}, "Saúde do robô, atualizações, acesso e eventos."))),
    h("div", {class: "grid"},
      h("div", {class: "span-7"}, section("diagnostico", "shield", "Diagnóstico", checkList)),
      h("div", {class: "span-5"}, section("atualizacoes", "download", "Atualizações", updateFacts, updateMessage,
        h("div", {class: "button-row"}, check, install))),
      h("div", {class: "span-12"}, section("hardware", "system", "Raspberry Pi", gauges)),
      h("div", {class: "span-6"}, section("acesso", "globe", "Acesso pela internet",
        h("div", {class: "inline-status"}, tunnelState, tunnelLink), ngrokForm)),
      h("div", {class: "span-6"}, section("rede", "wifi", "Rede", networkFacts),
        section("seguranca", "lock", "Senha do painel", passwordForm)),
      h("div", {class: "span-12"}, section("eventos", "list", "Eventos",
        h("div", {class: "filters"}, h("div", {}, h("label", {class: "label", htmlFor: "log-service"}, "Serviço"), service),
          h("div", {}, h("label", {class: "label", htmlFor: "log-level"}, "Nível mínimo"), level)),
        logs, h("p", {class: "help"}, "Até 100 eventos das últimas 24 horas. Chaves e senhas aparecem ocultas.")))));
  follow();

  function renderLogs(data) {
    const entries = data?.entries || [];
    const last = entries.at(-1)?.id || "";
    if (logs.dataset.cursor === last && entries.length) return;
    const nearBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 60;
    logs.dataset.cursor = last;
    replace(logs, entries.length ? entries.map(entry => h("div", {class: `log ${Number(entry.priority) <= 3 ? "log-error" : Number(entry.priority) === 4 ? "log-warn" : ""}`},
      h("time", {}, entry.at ? new Date(Number(entry.at) / 1000).toLocaleString("pt-BR") : ""), h("span", {}, entry.message)))
      : empty("Nenhum evento neste filtro.", "list"));
    if (nearBottom || !logs.dataset.scrolled) { logs.scrollTop = logs.scrollHeight; logs.dataset.scrolled = "1"; }
  }

  return {
    show(id) {
      const target = id && document.getElementById(`sec-${id}`);
      if (!target) return;
      target.scrollIntoView({behavior: "smooth", block: "start"});
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1600);
    },
    update(status, payload) {
      if (!status) return;
      replace(checkList, (status.checks || []).map(item => {
        const body = [levelIcon(item.level), h("span", {class: "check-text"}, h("strong", {}, item.label), h("span", {}, item.detail))];
        return h("li", {}, item.action && !item.action.startsWith("sistema") ? h("a", {href: `#${item.action}`, class: "check-row"}, body, icon("chevron", 16))
          : h("div", {class: "check-row"}, body));
      }));

      const updates = status.updates || {}, voice = status.voice || {};
      replace(updateFacts, facts([["Instalada", updates.installed || voice.version], ["Disponível", updates.latest ? updates.latest.replace(/^v/, "") : "Nenhuma publicada"],
        ["Última verificação", updates.checked_at ? fmt.datetime(updates.checked_at) : "Ainda não verificado"],
        ["Estado", UPDATE_STATES[updates.state] || "—"]]));
      text(updateMessage, updates.message || "O robô procura versões novas a cada hora e instala sozinho.");
      const running = ["checking", "staging", "waiting", "installing"].includes(updates.state);
      check.disabled = running || check.classList.contains("is-busy");
      install.disabled = running || status.pending || updates.state !== "available" || install.classList.contains("is-busy");

      const [tunnelLabel, tunnelTone] = TUNNEL[status.ngrok?.state] || ["—", "neutral"];
      tunnelState.className = `badge badge-${tunnelTone}`;
      text(tunnelState, tunnelLabel);
      const address = status.ngrok?.url;
      replace(tunnelLink, address ? [h("a", {href: address, target: "_blank", rel: "noopener"}, address),
        h("button", {class: "btn-icon", type: "button", "aria-label": "Copiar endereço", onclick: () => copy(address)}, icon("copy", 18))] : []);

      const network = status.network || {};
      replace(networkFacts, facts([["Estado", NETWORK_MODES[network.mode] || "—"], ["Wi-Fi", network.wifi || (network.mode === "configuracao" ? `Hotspot ${network.ssid}` : "—")],
        ["Na rede local", h("span", {class: "address"}, h("a", {href: status.local_url}, status.local_url),
          h("button", {class: "btn-icon", type: "button", "aria-label": "Copiar endereço local", onclick: () => copy(status.local_url)}, icon("copy", 18)))],
        ["Rede de configuração", network.ssid || "—"]]));

      const system = status.system || {};
      text(cpu.value, system.cpu_percent == null ? "—" : `${fmt.decimal(system.cpu_percent)}%`);
      setMeter(cpu.bar, system.cpu_percent || 0, 100);
      text(cpu.sub, `Ligado há ${fmt.duration(system.uptime_seconds)} · ${system.hostname || ""}`);
      text(temperature.value, system.temperature == null ? "—" : `${fmt.decimal(system.temperature)} °C`);
      setMeter(temperature.bar, system.temperature || 0, 85);
      text(temperature.sub, system.temperature >= 80 ? "Quente: confira a ventilação" : "Limite seguro: 85 °C");
      for (const [item, key] of [[memory, "memory"], [disk, "disk"]]) {
        const used = system[`${key}_used`] || 0, total = system[`${key}_total`] || 0;
        text(item.value, total ? `${fmt.percent(used / total)}` : "—");
        setMeter(item.bar, used, total || 1);
        text(item.sub, `${fmt.bytes(used)} de ${fmt.bytes(total)}`);
      }
      if (payload?.logs) renderLogs(payload.logs);
    },
    destroy() { ctx.logs(null); },
  };
}
