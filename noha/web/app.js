// Dashboard shell: sign-in, navigation and the live status stream.
import {h, icon, api, session, toast, replace, busy, text} from "./lib.js";
import {describe, orb} from "./robot.js";
import * as home from "./pages/home.js";
import * as activity from "./pages/activity.js";
import * as settings from "./pages/settings.js";
import * as system from "./pages/system.js";

const PAGES = {
  inicio: {label: "Início", icon: "home", module: home},
  atividade: {label: "Atividade", icon: "activity", module: activity},
  ajustes: {label: "Ajustes", icon: "settings", module: settings},
  sistema: {label: "Sistema", icon: "system", module: system},
};
const root = document.getElementById("root");
const state = {status: null, page: null, view: null, stream: null, streamKey: null, logs: null, available: true, shell: null};
let typedPassword = "";

function logo(extra = "") {
  return h("img", {src: "/logo.png", alt: "Noha", class: `logo ${extra}`});
}

// Sign-in ---------------------------------------------------------------------------------------
async function login(message = "") {
  stop();
  const error = h("p", {class: "form-error", role: "alert"}, message);
  const password = h("input", {id: "password", type: "password", class: "input", autocomplete: "current-password", required: true, maxlength: "256"});
  const hint = h("p", {class: "auth-hint", hidden: true}, icon("info", 16),
    h("span", {}, "Primeiro acesso: a senha é ", h("strong", {}, "admin"), ". Você vai criar uma nova em seguida."));
  const submit = h("button", {class: "btn btn-primary btn-block", type: "submit"}, "Entrar", icon("chevron", 18));
  const form = h("form", {class: "auth-card", novalidate: true},
    h("h1", {}, "Entrar no painel"),
    h("p", {class: "muted"}, "Acompanhe a Noha e ajuste como ela recebe os visitantes."),
    h("label", {class: "label", htmlFor: "password"}, "Senha do painel"), password, error, submit, hint);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!password.value) { text(error, "Digite a senha."); password.focus(); return; }
    await busy(submit, async () => {
      try {
        const result = await api("login", {password: password.value});
        session.csrf = result.csrf;
        typedPassword = password.value;
        if (result.must_change) changePassword(); else shell();
      } catch (failure) {
        text(error, failure.message);
        password.select();
      }
    });
  });
  replace(root, h("div", {class: "auth"},
    h("section", {class: "auth-brand"}, logo("logo-invert"), h("div", {class: "auth-orb"}, orb()),
      h("div", {}, h("p", {class: "eyebrow"}, "NOHA CONCEPT"), h("p", {class: "auth-tagline"}, "Seu anfitrião, por dentro."))),
    h("main", {class: "auth-main"}, form)));
  password.focus();
  try { hint.hidden = !(await api("hello")).default_hint; } catch { /* hint is optional */ }
}

function changePassword() {
  stop();
  const known = typedPassword === "admin";
  const error = h("p", {class: "form-error", role: "alert"});
  const current = h("input", {id: "current", type: "password", class: "input", autocomplete: "current-password", maxlength: "256"});
  const next = h("input", {id: "next", type: "password", class: "input", autocomplete: "new-password", minlength: "6", maxlength: "256"});
  const again = h("input", {id: "again", type: "password", class: "input", autocomplete: "new-password", maxlength: "256"});
  const submit = h("button", {class: "btn btn-primary btn-block", type: "submit"}, "Salvar nova senha");
  const form = h("form", {class: "auth-card", novalidate: true},
    h("span", {class: "auth-icon"}, icon("lock", 22)),
    h("h1", {}, "Crie uma senha"),
    h("p", {class: "muted"}, "A senha padrão é conhecida por todos. Crie uma senha só sua antes de continuar; ela vale também para o acesso pela internet."),
    known ? null : [h("label", {class: "label", htmlFor: "current"}, "Senha atual"), current],
    h("label", {class: "label", htmlFor: "next"}, "Nova senha"), next,
    h("p", {class: "help"}, "Pelo menos 6 caracteres."),
    h("label", {class: "label", htmlFor: "again"}, "Repita a nova senha"), again, error, submit);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (next.value.length < 6) { text(error, "Use pelo menos 6 caracteres."); next.focus(); return; }
    if (next.value !== again.value) { text(error, "As senhas não coincidem."); again.focus(); return; }
    await busy(submit, async () => {
      try {
        await api("password", {current: known ? typedPassword : current.value, password: next.value});
        typedPassword = "";
        login("Senha criada. Entre com a nova senha.");
      } catch (failure) { text(error, failure.message); }
    });
  });
  replace(root, h("div", {class: "auth"},
    h("section", {class: "auth-brand"}, logo("logo-invert"), h("div", {class: "auth-orb"}, orb()),
      h("div", {}, h("p", {class: "eyebrow"}, "PRIMEIRO ACESSO"), h("p", {class: "auth-tagline"}, "Um último passo para proteger a Noha."))),
    h("main", {class: "auth-main"}, form)));
  (known ? next : current).focus();
}

// Shell -----------------------------------------------------------------------------------------
function navItem(id, page, compact) {
  return h("a", {href: `#${id}`, class: compact ? "tab" : "nav-item", "data-page": id}, icon(page.icon, compact ? 22 : 20), h("span", {}, page.label));
}

function shell() {
  const content = h("main", {class: "content", id: "content", tabindex: "-1"});
  const pill = h("a", {href: "#inicio", class: "status-pill", "aria-label": "Estado do robô"}, h("span", {class: "dot"}), h("span", {class: "status-text"}, "Conectando…"));
  const sidePill = h("div", {class: "side-status"}, h("span", {class: "dot"}), h("div", {}, h("strong", {class: "status-text"}, "Conectando…"), h("span", {class: "side-version"}, "")));
  const logout = async () => {
    if (!await guard()) return;
    try { await api("logout", {}); } catch { /* already gone */ }
    session.csrf = "";
    login();
  };
  state.shell = {content, pills: [pill, sidePill]};
  replace(root, h("div", {class: "shell"},
    h("aside", {class: "sidebar"},
      h("a", {href: "#inicio", class: "sidebar-logo", "aria-label": "Noha, início"}, logo()),
      h("nav", {class: "nav", "aria-label": "Principal"}, Object.entries(PAGES).map(([id, page]) => navItem(id, page, false))),
      h("div", {class: "sidebar-foot"}, sidePill,
        h("button", {class: "btn btn-ghost btn-block nav-logout", type: "button", onclick: logout}, icon("logout", 18), "Sair"))),
    h("header", {class: "topbar"},
      h("a", {href: "#inicio", class: "topbar-logo", "aria-label": "Noha, início"}, logo()),
      pill,
      h("button", {class: "btn-icon", type: "button", "aria-label": "Sair", onclick: logout}, icon("logout", 20))),
    content,
    h("nav", {class: "tabbar", "aria-label": "Principal"}, Object.entries(PAGES).map(([id, page]) => navItem(id, page, true)))));
  route();
  connect();
}

function updateChrome(status) {
  if (!state.shell) return;
  const {title, tone} = describe(status);
  for (const pill of state.shell.pills) {
    pill.dataset.tone = state.available ? tone : "error";
    text(pill.querySelector(".status-text"), state.available ? title : "Painel sem resposta");
  }
  const version = state.shell.pills[1].querySelector(".side-version");
  text(version, status?.voice?.version ? `Versão ${status.voice.version}` : "");
}

// Router ----------------------------------------------------------------------------------------
async function guard() {
  return !state.view?.dirty || await state.view.dirty();
}

let current = location.hash;
async function route() {
  const [pageId, section] = location.hash.slice(1).split("/");
  const id = PAGES[pageId] ? pageId : "inicio";
  if (state.page === id && state.view) {
    current = location.hash;
    state.view.show?.(section);
    return;
  }
  if (state.view && !await guard()) {
    history.replaceState(null, "", current);  // stay, keep the unsaved form
    return;
  }
  current = location.hash;
  state.view?.destroy?.();
  state.page = id;
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === id) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  const container = h("div", {class: "page"});
  replace(state.shell.content, container);
  document.title = `${PAGES[id].label} · Noha`;
  state.view = PAGES[id].module.mount(container, {
    status: () => state.status,
    navigate: hash => { location.hash = hash; },
    logs: params => { state.logs = params; connect(); },
    refresh: () => connect(true),
  });
  if (state.status) state.view.update?.(state.status, {});
  state.view.show?.(section);
  window.scrollTo(0, 0);
  state.shell.content.focus({preventScroll: true});
}

// Live status -----------------------------------------------------------------------------------
function connect(force = false) {
  const params = new URLSearchParams();
  if (state.logs) { params.set("logs", "1"); params.set("service", state.logs.service); params.set("level", state.logs.level); }
  const key = params.toString();
  if (!force && state.stream && state.streamKey === key && state.stream.readyState !== EventSource.CLOSED) return;
  state.stream?.close();
  state.streamKey = key;
  if (document.hidden || !state.shell) { state.stream = null; return; }
  const stream = new EventSource(`/api/events?${key}`);
  state.stream = stream;
  stream.onmessage = event => {
    const data = JSON.parse(event.data);
    state.available = data.available;
    if (data.available) state.status = data.status;
    updateChrome(state.status);
    state.view?.update?.(state.status, data);
  };
  stream.onerror = async () => {
    state.available = false;
    updateChrome(state.status);
    try { await api("session"); } catch { /* noha:expired handles sign-out */ }
  };
}

function stop() {
  state.stream?.close();
  const view = state.view;
  state.stream = state.view = state.page = state.shell = state.logs = null;  // before destroy: no reconnect
  view?.destroy?.();
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden) connect(); else { state.stream?.close(); state.stream = null; } });
window.addEventListener("noha:expired", () => { if (state.shell) login("Sua sessão terminou. Entre novamente."); });
window.addEventListener("noha:password", () => changePassword());
window.addEventListener("beforeunload", event => { if (state.view?.isDirty?.()) { event.preventDefault(); event.returnValue = ""; } });

(async () => {
  try {
    const current = await api("session");
    session.csrf = current.csrf;
    if (current.scope === "board") location.replace("/board");
    else if (current.must_change) changePassword();
    else shell();
  } catch (failure) {
    if (failure.status === 401 || failure.status === 403) login();
    else login(failure.message);
  }
})();
