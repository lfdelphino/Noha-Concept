"use strict";
const $ = id => document.getElementById(id);
const state = {csrf: "", tab: "overview", days: 1, stream: null, settings: null, lastStatus: null, dirty: new Set(), authenticated: false};
const labels = {conversations: "Conversas", greetings: "Saudações locais", duration_seconds: "Tempo de conversa", input_tokens: "Tokens de entrada", output_tokens: "Tokens de saída", cached_tokens: "Tokens em cache", errors: "Erros", reconnects: "Reconexões", cost_usd: "Custo estimado"};
const names = {online:"Online", configuracao_necessaria:"Configuração necessária", reconectando:"Reconectando", indisponivel:"Indisponível", iniciando:"Iniciando", configuracao:"Configuração Wi-Fi", conversando:"Conversando", cumprimentando:"Cumprimentando", orientando_wifi:"Explicando o Wi-Fi", sensor_desligado:"Saudações desligadas", aquecendo_sensor:"Aquecendo sensor", aguardando_movimento:"Pronta para receber", conectando:"Conectando", gpio_indisponivel:"Sensor indisponível", recuperando_audio_ou_conexao:"Recuperando conexão", audio_indisponivel:"Áudio indisponível", erro_api:"OpenAI recusou a conversa", sem_conexao:"Sem conexão com a OpenAI", chave_ausente:"Chave OpenAI ausente", orcamento_esgotado:"Limite diário atingido", parado:"Parada", pending:"Aguardando conversa", applied:"Aplicada", error:"Falha", checking:"Verificando", staging:"Preparando versão", waiting:"Aguardando o robô", installing:"Instalando", updated:"Atualizada", current:"Em dia", available:"Atualização disponível", rollback:"Versão anterior restaurada", deferred:"Atualização adiada", offline:"Sem conexão", disabled:"Atualizações desativadas", installer_required:"Reexecute o instalador"};
const text = (id, value) => { $(id).textContent = value ?? "—"; };
// Voice states before 1.3.3 carried a hint after ":" (e.g. "erro_api: confira…"); name the prefix.
const nameOf = value => names[value] || names[value?.split(":")[0]] || (value || "Indisponível").replaceAll("_", " ");
const number = value => value === null || value === undefined ? "Indisponível" : new Intl.NumberFormat("pt-BR", {maximumFractionDigits: 0}).format(value);
const duration = seconds => seconds == null ? "—" : seconds < 60 ? `${Math.round(seconds)} s` : seconds < 3600 ? `${Math.floor(seconds / 60)} min` : `${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`;
const usd = value => value == null ? "—" : `US$ ${value.toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const metric = (key, value) => key === "duration_seconds" ? duration(value) : key === "cost_usd" ? usd(value) : number(value);
const bytes = value => `${((value || 0) / 1073741824).toLocaleString("pt-BR", {maximumFractionDigits: 1})} GB`;
function notice(message, error = false) { text("notice", message); $("notice").hidden = !message; $("notice").classList.toggle("error", error); }
function loggedOut(message = "") { state.authenticated = false; state.csrf = ""; state.stream?.close(); state.stream = null; $("shell").hidden = true; $("login").hidden = false; text("login-error", message); }
async function api(path, data) {
  const response = await fetch(`/api/${path}`, {method: data === undefined ? "GET" : "POST", credentials: "same-origin", headers: data === undefined ? {} : {"Content-Type":"application/json", "X-Noha-Token": state.csrf}, body: data === undefined ? undefined : JSON.stringify(data)});
  let result; try { result = await response.json(); } catch { result = {}; }
  if (response.status === 401) loggedOut("Sua sessão terminou. Entre novamente.");
  if (!response.ok) throw new Error(result.error || `Não foi possível concluir (${response.status}).`);
  return result;
}
function setLink(id, value, fallback) { text(id, value || fallback); if (value) $(id).href = value; else $(id).removeAttribute("href"); }
const ago = seconds => seconds < 60 ? `${Math.max(1, Math.round(seconds))} s` : seconds < 3600 ? `${Math.floor(seconds / 60)} min` : `${Math.floor(seconds / 3600)} h`;
const clock = epoch => new Date(epoch * 1000).toLocaleTimeString("pt-BR");
const gates = {aquecendo: "Aquecendo", conversando: "Em conversa", rearmando: "Rearmando", confirmando: "Confirmando presença", pronto: "Pronto para cumprimentar", desligado: "Desligado"};
function motionCard(voice) {
  // Positions use the Pi's clock (heartbeat time), not the browser's.
  const now = voice.updated || Date.now() / 1000, span = 600, since = now - span, history = voice.motion_history || [];
  const missing = voice.gpio_ready === false;
  $("motion-dot").classList.toggle("on", !voice.stale && !missing && !!voice.motion);
  text("motion-state", voice.stale ? "Estado desatualizado" : missing ? "Sensor indisponível" : voice.motion ? "Movimento detectado agora" : "Sem movimento");
  text("motion-detail", missing ? `Confira a ligação no GPIO ${voice.gpio ?? "—"}` : voice.last_motion ? `Último movimento há ${ago(now - voice.last_motion)} · ${clock(voice.last_motion)}` : "Nenhum movimento desde que o serviço iniciou");
  text("gate-badge", voice.stale || !gates[voice.gate] ? "—" : ["aquecendo", "rearmando"].includes(voice.gate) || (voice.gate === "confirmando" && voice.gate_seconds) ? `${gates[voice.gate]} · ${voice.gate_seconds} s` : gates[voice.gate]);
  const visible = history.filter(([, end]) => (end ?? now) >= since), root = $("motion-timeline");
  root.replaceChildren(...visible.map(([start, end]) => {
    const mark = document.createElement("i"), from = Math.max(start, since), to = end ?? now;
    mark.style.left = `${100 * (from - since) / span}%`; mark.style.width = `${100 * (to - from) / span}%`;
    mark.title = `${clock(start)} – ${end ? clock(end) : "agora"} (${ago(to - start)})`;
    return mark;
  }));
  const count = visible.length === 1 ? "1 detecção" : `${visible.length || "Nenhuma"} detecç${visible.length ? "ões" : "ão"}`;
  text("motion-count", count); root.setAttribute("aria-label", `${count} de movimento nos últimos 10 minutos`);
}
function updateStatus(status) {
  state.lastStatus = status;
  const voice = status.voice || {}, system = status.system || {}, tunnel = status.ngrok || {}, updates = status.updates || {};
  text("voice-state", voice.stale ? "Estado de voz desatualizado" : nameOf(voice.state));
  motionCard(voice);
  if (!$("presence").classList.contains("busy")) { const on = status.presence !== false; $("presence").setAttribute("aria-pressed", String(on)); text("presence", on ? "Ligado" : "Desligado"); }
  text("voice-badge", voice.stale ? "Sem sinal" : voice.busy ? "Em conversa" : voice.ready ? "Pronta" : "Atenção");
  text("active-model", [voice.model || status.model, voice.spent_today == null ? "" : `hoje ${usd(voice.spent_today)}${voice.daily_budget ? ` de ${usd(voice.daily_budget)}` : ""}`].filter(Boolean).join(" · "));
  const absent = voice.audio_missing?.length ? `Não encontrado: ${voice.audio_missing.join(", ")}` : "";
  const problems = [absent, voice.gpio_ready === false ? "Sensor de movimento indisponível" : ""].filter(Boolean).join(" · ");
  text("voice-ready", voice.stale ? "Aguardando heartbeat" : voice.detail || (voice.ready ? "Áudio e sensor prontos" : problems || "Confira áudio, sensor e chave"));
  document.querySelector(".hero-card").classList.toggle("speaking", !!voice.busy && !voice.stale);
  text("uptime", `${duration(system.uptime_seconds)} ligada`);
  text("network-state", nameOf(status.network?.mode)); const network = status.network || {};
  text("network-detail", network.wifi ? `Wi-Fi ${network.wifi}` : network.mode === "configuracao" ? `Rede de configuração ${network.ssid}` : network.message);
  text("ngrok-state", nameOf(tunnel.state)); text("hardware", voice.stale ? "Estado desatualizado" : `GPIO ${voice.gpio ?? "—"} · ${voice.audio_ready ? "Áudio OK" : absent || "Verifique áudio"}`);
  setLink("local-url", status.local_url); setLink("public-url", tunnel.url, "Configure o ngrok");
  text("cpu", system.cpu_percent == null ? "—" : `${system.cpu_percent.toLocaleString("pt-BR")} %`); $("cpu-bar").value = system.cpu_percent || 0;
  text("temperature", system.temperature == null ? "—" : `${system.temperature.toLocaleString("pt-BR", {maximumFractionDigits:1})} °C`);
  for (const key of ["memory", "disk"]) { const used = system[`${key}_used`], total = system[`${key}_total`]; text(key, bytes(used)); text(`${key}-detail`, `de ${bytes(total)}`); $(`${key}-bar`).value = total ? 100 * used / total : 0; }
  $("pending-settings").hidden = !status.pending && status.apply?.state !== "error";
  text("pending-settings", status.pending ? "Configuração pendente. Aguardando o fim da conversa atual." : status.apply?.message);
  text("installed-version", updates.installed || voice.version); text("latest-version", updates.latest || "Nenhuma release encontrada");
  text("last-check", updates.checked_at ? new Date(updates.checked_at * 1000).toLocaleString("pt-BR") : "Ainda não verificado");
  text("update-state", nameOf(updates.state)); text("update-detail", updates.message || "O robô aguarda ociosidade antes de instalar.");
  const busy = ["checking", "staging", "waiting", "installing"].includes(updates.state);
  $("check-update").disabled = busy; $("install-update").disabled = busy || !!status.pending;
}
function renderLogs(data) {
  const root = $("log-entries"), nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 60;
  const entries = data?.entries || [];
  if (root.dataset.cursor === entries.at(-1)?.id && entries.length) return;
  root.replaceChildren(); root.dataset.cursor = entries.at(-1)?.id || "";
  if (!entries.length) root.textContent = "Nenhum evento neste filtro.";
  for (const row of entries) { const div = document.createElement("div"), at = document.createElement("time"), message = document.createElement("span"); div.className = "log-line"; at.textContent = row.at ? new Date(Number(row.at) / 1000).toLocaleString("pt-BR") : ""; message.textContent = row.message; div.append(at, message); root.append(div); }
  if (nearBottom) root.scrollTop = root.scrollHeight;
}
function connectStream() {
  state.stream?.close(); state.stream = null;
  if (!state.authenticated || document.hidden) return;
  const params = new URLSearchParams();
  if (state.tab === "system") { params.set("logs", "1"); params.set("service", $("log-service").value); params.set("level", $("log-level").value); }
  const stream = new EventSource(`/api/events?${params}`); state.stream = stream;
  stream.onmessage = event => { const data = JSON.parse(event.data); text("connection", data.available ? "Ao vivo" : "Indisponível"); $("connection").classList.toggle("offline", !data.available); if (data.available) updateStatus(data.status); if (data.logs) renderLogs(data.logs); };
  stream.onerror = async () => { text("connection", "Reconectando"); $("connection").classList.add("offline"); try { await api("session"); } catch {} };
}
async function usage() {
  const data = await api(`usage?days=${state.days}`); const root = $("usage-totals"); root.replaceChildren();
  for (const [key, label] of Object.entries(labels)) { const card = document.createElement("article"), title = document.createElement("h3"), value = document.createElement("p"); card.className = "card metric"; title.textContent = label; value.className = "metric-value"; value.textContent = metric(key, data.totals[key]); card.append(title, value); root.append(card); }
  const models = $("usage-models"); models.replaceChildren();
  if (!data.models.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "As próximas conversas aparecerão aqui."; models.append(empty); }
  for (const model of data.models) { const card = document.createElement("article"), title = document.createElement("h3"); card.className = "card usage-model"; title.textContent = model.model; card.append(title); for (const [key, label] of Object.entries(labels)) { const row = document.createElement("div"), text = document.createElement("span"), value = document.createElement("strong"); row.className = "usage-row"; text.textContent = label; value.textContent = metric(key, model[key]); row.append(text, value); card.append(row); } models.append(card); }
}
function voices(selected) {
  const model = state.settings?.models.find(m => m.id === $("model").value); if (!model) return;
  $("voice").replaceChildren(...model.voices.map(voice => new Option(voice[0].toUpperCase() + voice.slice(1), voice)));
  $("voice").value = model.voices.includes(selected) ? selected : model.voices[0];
  text("model-note", model.deprecated ? "Modelo legado, marcado como descontinuado pela OpenAI." : model.transport === "live" ? "GPT-Live · escuta e fala contínuas." : "Realtime · áudio em tempo real.");
}
function devices(id, kind, selected) {
  const options = state.settings.audio_devices.filter(d => d.class === kind).map(d => new Option(d.description || d.name, d.name));
  // Keep an unplugged saved device visible instead of silently switching to another one.
  if (selected && !options.some(o => o.value === selected)) options.unshift(new Option(`${selected} (desconectado)`, selected));
  $(id).replaceChildren(...options); $(id).value = selected;
}
function echoNote() {
  // Mirrors Audio.same_xvf3800: same card, input and output.
  const card = (name, prefix) => name.startsWith(prefix) ? name.slice(prefix.length).replace(/\.[^.]*$/, "") : null;
  const mic = card($("microphone").value, "alsa_input."), chip = !!mic && mic.includes("XVF3800") && mic === card($("speaker").value, "alsa_output.");
  const mode = $("echo-cancel").value, hardware = mode === "xvf3800" || (mode === "auto" && chip);
  text("echo-note", hardware ? "O XVF3800 cancela o eco do próprio áudio. Use somente com o alto-falante ligado ao array."
    : "PipeWire cancela o eco. Trocar o áudio reinicia o PipeWire.");
}
for (const id of ["microphone", "speaker", "echo-cancel"]) $(id).addEventListener("change", echoNote);
async function settings() {
  const data = await api("settings"); state.settings = data;
  if (!state.dirty.has("voice-form")) {
    $("model").replaceChildren(...data.models.map(model => { const option = new Option(model.label, model.id); option.disabled = model.retired; return option; }));
    $("model").value = data.model; voices(data.voice); $("instructions").value = data.instructions;
    $("gpio").replaceChildren(...Object.entries(data.gpio_pins).map(([gpio, pin]) => new Option(`BCM ${gpio} · pino físico ${pin}`, gpio))); $("gpio").value = data.gpio;
    devices("microphone", "Audio/Source", data.microphone); devices("speaker", "Audio/Sink", data.speaker);
    $("echo-cancel").value = data.echo_cancel; echoNote(); $("daily-budget").value = data.daily_budget_usd;
  }
  if (!state.dirty.has("ngrok-form")) $("ngrok-url").value = data.ngrok_url;
  text("openai-configured", data.openai_key_configured ? "Chave configurada. Deixe vazio para mantê-la." : "Nenhuma chave configurada.");
  text("ngrok-configured", data.ngrok_token_configured ? "Authtoken configurado. Deixe vazio para mantê-lo." : "Informe o authtoken do agente disponível na sua conta ngrok.");
}
async function navigate() {
  const tab = location.hash.slice(1); state.tab = ["overview", "usage", "settings", "system"].includes(tab) ? tab : "overview";
  document.querySelectorAll(".view").forEach(view => view.hidden = view.id !== state.tab);
  document.querySelectorAll(".bottom-nav a").forEach(a => { if (a.hash === `#${state.tab}`) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  if (!state.authenticated) return;
  connectStream();
  try { if (state.tab === "usage") await usage(); if (state.tab === "settings") await settings(); } catch (error) { notice(error.message, true); }
}
async function enter() { state.authenticated = true; $("login-password").value = ""; $("login").hidden = true; $("shell").hidden = false; notice(""); await navigate(); }
$("login-form").addEventListener("submit", async event => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); button.disabled = true; try { const data = await api("login", {password: $("login-password").value}); state.csrf = data.csrf; text("login-error", ""); await enter(); } catch (error) { text("login-error", error.message); } finally { button.disabled = false; } });
$("logout").onclick = async () => { try { await api("logout", {}); } finally { loggedOut(); } };
document.querySelectorAll("form.form-card").forEach(form => { form.addEventListener("input", () => state.dirty.add(form.id)); form.addEventListener("change", () => state.dirty.add(form.id)); });
$("model").onchange = () => voices($("voice").value);
async function save(form, operation, data, after) { const button = form.querySelector("button[type=submit]"), result = form.querySelector(".form-result"); button.disabled = true; result.classList.remove("error"); result.textContent = "Salvando…"; try { const saved = await api(operation, data); result.textContent = saved.message || "Salvo."; state.dirty.delete(form.id); await after?.(saved); } catch (error) { result.textContent = error.message; result.classList.add("error"); } finally { button.disabled = false; } }
$("voice-form").onsubmit = async event => { event.preventDefault(); await save(event.currentTarget, "settings", {model: $("model").value, voice: $("voice").value, gpio: Number($("gpio").value), microphone: $("microphone").value, speaker: $("speaker").value, echo_cancel: $("echo-cancel").value, daily_budget_usd: Number($("daily-budget").value), instructions: $("instructions").value, openai_key: $("openai-key").value}, () => { $("openai-key").value = ""; $("pending-settings").hidden = false; text("pending-settings", "Configuração pendente. Aguardando o fim da conversa atual."); }); };
$("ngrok-form").onsubmit = async event => { event.preventDefault(); await save(event.currentTarget, "ngrok", {url: $("ngrok-url").value, authtoken: $("ngrok-token").value}, saved => { $("ngrok-token").value = ""; setLink("public-url", saved.url); notice(`ngrok reconectando. Endereço: ${saved.url}`); }); };
$("password-form").onsubmit = async event => { event.preventDefault(); if ($("new-password").value !== $("confirm-password").value) { const result = event.currentTarget.querySelector(".form-result"); result.textContent = "As senhas não coincidem."; result.classList.add("error"); return; } await save(event.currentTarget, "password", {current: $("current-password").value, password: $("new-password").value}, () => { $("password-form").reset(); loggedOut("Senha alterada. Entre com a nova senha."); }); };
document.querySelectorAll("[data-days]").forEach(button => button.onclick = async () => { state.days = Number(button.dataset.days); document.querySelectorAll("[data-days]").forEach(b => b.setAttribute("aria-pressed", String(b === button))); try { await usage(); } catch (error) { notice(error.message, true); } });
$("presence").onclick = async () => { const button = $("presence"), on = button.getAttribute("aria-pressed") !== "true"; button.disabled = true; button.classList.add("busy"); try { const result = await api("presence", {enabled: on}); button.setAttribute("aria-pressed", String(result.enabled)); text("presence", result.enabled ? "Ligado" : "Desligado"); notice(result.message); } catch (error) { notice(error.message, true); } finally { button.disabled = false; button.classList.remove("busy"); } };
$("refresh-usage").onclick = () => usage().catch(error => notice(error.message, true));
for (const action of ["check-update", "install-update"]) $(action).onclick = async () => { $(action).disabled = true; try { const result = await api(action, {}); text("update-result", result.message); } catch (error) { text("update-result", error.message); } finally { $(action).disabled = false; } };
for (const id of ["log-service", "log-level"]) $(id).onchange = () => { $("log-entries").dataset.cursor = ""; connectStream(); };
document.querySelectorAll(".copy").forEach(button => button.onclick = async () => { const value = $(button.dataset.copy).getAttribute("href"); if (!value) return; try { if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value); else { const input = document.createElement("textarea"); input.value = value; input.style.position = "fixed"; input.style.opacity = "0"; document.body.append(input); input.select(); const copied = document.execCommand("copy"); input.remove(); if (!copied) throw new Error(); } button.textContent = "Copiado"; setTimeout(() => button.textContent = "Copiar", 1800); } catch { notice(`Endereço: ${value}`); } });
window.addEventListener("hashchange", navigate); document.addEventListener("visibilitychange", () => { connectStream(); if (!document.hidden && state.authenticated && state.tab === "usage") usage().catch(() => {}); });
window.addEventListener("beforeunload", event => { if (state.dirty.size) { event.preventDefault(); event.returnValue = ""; } });
api("session").then(async session => { state.csrf = session.csrf; await enter(); }).catch(() => loggedOut());
