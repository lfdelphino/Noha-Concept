// Shared helpers: DOM builder, icons, pt-BR formatting, API calls, toasts and dialogs.
const SVG = "http://www.w3.org/2000/svg";
const PROPS = new Set(["value", "checked", "selected", "disabled", "hidden", "textContent", "htmlFor", "indeterminate"]);

export function h(tag, props = {}, ...children) {
  const svg = tag === "svg" || tag === "path" || tag === "rect" || tag === "g" || tag === "line" || tag === "text" || tag === "title";
  const el = svg ? document.createElementNS(SVG, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "style") for (const [name, rule] of Object.entries(value)) el.style.setProperty(name.startsWith("--") ? name : name.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`), rule);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function text(el, value) {
  const next = value === null || value === undefined ? "—" : String(value);
  if (el.textContent !== next) el.textContent = next;
}

// 24px stroke icons, drawn for this dashboard.
const ICONS = {
  home: '<path d="M3 10.5 12 4l9 6.5"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
  activity: '<path d="M4 20h16"/><path d="M7 16v-5"/><path d="M12 16V6"/><path d="M17 16V9"/>',
  settings: '<path d="M4 7h9"/><path d="M17 7h3"/><circle cx="15" cy="7" r="2"/><path d="M4 17h3"/><path d="M11 17h9"/><circle cx="9" cy="17" r="2"/>',
  system: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
  play: '<path d="M8 5.5v13l10-6.5z"/>',
  volume: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5z"/><path d="M16 9a4 4 0 0 1 0 6"/><path d="M18.5 6.5a7.5 7.5 0 0 1 0 11"/>',
  mute: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0"/><path d="M5.5 12.5a9.5 9.5 0 0 1 13 0"/><path d="M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r="1" fill="currentColor"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8"/><path d="M16 7l2.5 2.5"/><path d="M14 9l2 2"/>',
  shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.5 4.5L20 16"/><path d="M20 20v-4h-4"/>',
  download: '<path d="M12 4v11"/><path d="M7 10.5l5 5 5-5"/><path d="M5 20h14"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.2" r=".7" fill="currentColor"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="8" r=".7" fill="currentColor"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.5-3.5 3.2-5.5 6.5-5.5s6 2 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 14.8c2 .7 3.2 2.5 3.5 5.2"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
  coin: '<circle cx="12" cy="12" r="9"/><path d="M14.8 9.2c-.5-.9-1.5-1.4-2.8-1.4-1.7 0-2.8.9-2.8 2.1 0 2.9 5.8 1.5 5.8 4.3 0 1.2-1.2 2.1-3 2.1-1.4 0-2.5-.6-3-1.6"/><path d="M12 6v1.8M12 16.3V18"/>',
  radar: '<circle cx="12" cy="12" r="2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4"/><path d="M16.2 16.2a6 6 0 0 0 0-8.4"/><path d="M5 5a10 10 0 0 0 0 14"/><path d="M19 19a10 10 0 0 0 0-14"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0"/><path d="M12 17.5V21"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
  wave: '<path d="M3 12h2"/><path d="M7 8v8"/><path d="M11 5v14"/><path d="M15 9v6"/><path d="M19 7v10"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V4.5h6V7"/><path d="M6.5 7l1 13h9l1-13"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  thermo: '<path d="M10 14.5V5a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0z"/><path d="M12 10v6"/>',
  memory: '<rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 11v2M11 11v2M15 11v2M7 17v2M17 17v2"/>',
  disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r=".9" fill="currentColor"/><circle cx="4.5" cy="12" r=".9" fill="currentColor"/><circle cx="4.5" cy="18" r=".9" fill="currentColor"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  power: '<path d="M12 3v8"/><path d="M6.4 6.5a8 8 0 1 0 11.2 0"/>',
  bolt: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
};

export function icon(name, size = 20) {
  const el = document.createElementNS(SVG, "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("width", size);
  el.setAttribute("height", size);
  el.setAttribute("fill", "none");
  el.setAttribute("stroke", "currentColor");
  el.setAttribute("stroke-width", "1.8");
  el.setAttribute("stroke-linecap", "round");
  el.setAttribute("stroke-linejoin", "round");
  el.setAttribute("aria-hidden", "true");
  el.setAttribute("class", "icon");
  el.innerHTML = ICONS[name] || ICONS.info;
  return el;
}

// pt-BR formatting ------------------------------------------------------------------------------
const integer = new Intl.NumberFormat("pt-BR", {maximumFractionDigits: 0});
const money = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fine = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 4});
export const fmt = {
  number: value => value === null || value === undefined ? "—" : integer.format(value),
  usd: value => value === null || value === undefined ? "—" : `US$ ${money.format(value)}`,
  usdFine: value => value === null || value === undefined ? "—" : `US$ ${(value > 0 && value < 0.01 ? fine : money).format(value)}`,
  percent: value => value === null || value === undefined || !isFinite(value) ? "—" : `${integer.format(value * 100)}%`,
  decimal: (value, digits = 1) => value === null || value === undefined ? "—" : value.toLocaleString("pt-BR", {maximumFractionDigits: digits}),
  duration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 60) return `${Math.round(seconds)} s`;
    if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60), rest = Math.round(seconds % 60);
      return rest && minutes < 10 ? `${minutes} min ${rest} s` : `${minutes} min`;
    }
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return seconds >= 86400 ? `${Math.floor(seconds / 86400)} d ${hours % 24} h` : `${hours} h ${minutes} min`;
  },
  ago(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 45) return "agora mesmo";
    if (seconds < 3600) return `há ${Math.max(1, Math.round(seconds / 60))} min`;
    if (seconds < 86400) return `há ${Math.floor(seconds / 3600)} h`;
    return `há ${Math.floor(seconds / 86400)} d`;
  },
  time: epoch => new Date(epoch * 1000).toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"}),
  datetime: epoch => new Date(epoch * 1000).toLocaleString("pt-BR", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}),
  bytes: value => `${((value || 0) / 1073741824).toLocaleString("pt-BR", {maximumFractionDigits: 1})} GB`,
  today: () => new Date().toLocaleDateString("pt-BR", {weekday: "long", day: "numeric", month: "long"}),
};

export function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Boa noite" : hour < 12 ? "Bom dia" : hour < 18 ? "Boa tarde" : "Boa noite";
}

// API -------------------------------------------------------------------------------------------
export const session = {csrf: ""};
const FALLBACK = {401: "Sua sessão terminou. Entre novamente.", 403: "Acesso negado.", 404: "Recurso não encontrado.",
  429: "Muitas tentativas. Aguarde alguns minutos.", 503: "Serviço iniciando. Aguarde alguns segundos."};

export async function api(path, data) {
  const post = data !== undefined;
  let response;
  try {
    response = await fetch(`/api/${path}`, {method: post ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
      headers: post ? {"Content-Type": "application/json", "X-Noha-Token": session.csrf} : {},
      body: post ? JSON.stringify(data) : undefined});
  } catch {
    throw Object.assign(new Error("Sem conexão com o robô. Confira a rede e tente de novo."), {status: 0});
  }
  let result = {};
  try { result = await response.json(); } catch { /* empty or HTML error body */ }
  if (response.status === 401 && path !== "login") window.dispatchEvent(new CustomEvent("noha:expired"));
  if (response.status === 403 && result.code === "password_change_required") window.dispatchEvent(new CustomEvent("noha:password"));
  if (!response.ok) throw Object.assign(new Error(result.error || FALLBACK[response.status] || `Não foi possível concluir (${response.status}).`), {status: response.status});
  return result;
}

// Feedback --------------------------------------------------------------------------------------
let toasts;
export function toast(message, kind = "ok") {
  if (!toasts) {
    toasts = h("div", {class: "toasts", role: "status", "aria-live": "polite"});
    document.body.append(toasts);
  }
  const item = h("div", {class: `toast toast-${kind}`}, icon(kind === "error" ? "alert" : kind === "info" ? "info" : "check", 18), h("span", {}, message));
  toasts.append(item);
  setTimeout(() => { item.classList.add("leaving"); setTimeout(() => item.remove(), 300); }, kind === "error" ? 6500 : 4000);
}

export function confirmDialog({title, message, confirm = "Confirmar", cancel = "Cancelar", danger = false}) {
  return new Promise(resolve => {
    const dialog = h("dialog", {class: "dialog"},
      h("form", {method: "dialog"},
        h("h2", {}, title), message ? h("p", {class: "muted"}, message) : null,
        h("div", {class: "dialog-actions"},
          h("button", {class: "btn btn-ghost", value: "cancel"}, cancel),
          h("button", {class: `btn ${danger ? "btn-danger" : "btn-primary"}`, value: "ok"}, confirm))));
    dialog.addEventListener("close", () => { resolve(dialog.returnValue === "ok"); dialog.remove(); });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export async function busy(button, task) {
  button.disabled = true;
  button.classList.add("is-busy");
  try { return await task(); } finally { button.disabled = false; button.classList.remove("is-busy"); }
}

export async function copy(value) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
    else {
      const area = h("textarea", {value, style: {position: "fixed", opacity: "0"}});
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error();
    }
    toast("Copiado");
  } catch { toast(value, "info"); }
}

// Small shared widgets --------------------------------------------------------------------------
export function badge(label, kind = "neutral") {
  return h("span", {class: `badge badge-${kind}`}, label);
}

export function switchControl({checked, label, onChange}) {
  const button = h("button", {type: "button", class: "switch", role: "switch", "aria-checked": String(!!checked), "aria-label": label},
    h("span", {class: "switch-knob"}));
  button.addEventListener("click", async () => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(next));
    if (onChange && await onChange(next) === false) button.setAttribute("aria-checked", String(!next));
  });
  return button;
}

export function setSwitch(button, checked) {
  button.setAttribute("aria-checked", String(!!checked));
}

export function meter(value, max, kind = "") {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const fill = h("span", {style: {width: `${ratio * 100}%`}});
  return h("div", {class: `meter ${kind}`, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(max), "aria-valuenow": String(value)}, fill);
}

export function setMeter(el, value, max) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  el.firstChild.style.width = `${ratio * 100}%`;
  el.setAttribute("aria-valuenow", String(value));
  el.classList.toggle("is-warn", ratio >= 0.8 && ratio < 1);
  el.classList.toggle("is-error", ratio >= 1);
}

export function empty(message, iconName = "info") {
  return h("div", {class: "empty"}, icon(iconName, 22), h("p", {}, message));
}
