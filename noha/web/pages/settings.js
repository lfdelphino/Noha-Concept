// Ajustes: every robot setting, rendered from the server's schema, saved per group.
import {h, icon, api, fmt, toast, text, replace, busy, switchControl, setSwitch, confirmDialog, empty} from "../lib.js";

const GROUP_ICONS = {personalidade: "sparkle", saudacoes: "wave", conversa: "chat", sensor: "radar", audio: "volume", custos: "coin", chave: "key"};
const GROUP_NOTES = {
  audio: "Trocar o microfone ou o alto-falante reinicia o áudio do robô.",
  sensor: "Mudar o pino exige a ligação física correspondente. Acompanhe o efeito no sensor da página Início.",
  chave: "Usada nas conversas e para gravar as saudações. Crie e gerencie chaves em platform.openai.com.",
  saudacoes: "Mudar frases, voz ou estilo grava novas saudações automaticamente quando houver internet.",
};
const INVALID = Symbol("invalid");
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
let unique = 0;

function priceHint(model) {
  const price = model?.price || {};
  if (price.per_minute) return `Cobrado por minuto de conversa: ${fmt.usdFine(price.per_minute)}/min.`;
  if (price.audio_in) return `Áudio por 1 milhão de tokens: ${fmt.usd(price.audio_in)} de entrada, ${fmt.usd(price.audio_out)} de saída.`;
  return "";
}

function control(setting, value, ctx, onChange) {
  const id = `f${++unique}`;
  const kind = setting.kind;
  if (kind === "bool") {
    const toggle = switchControl({checked: value, label: setting.label, onChange: () => { onChange(); return true; }});
    return {input: toggle, id, get: () => toggle.getAttribute("aria-checked") === "true", set: v => setSwitch(toggle, v), inline: true};
  }
  if (kind === "int" || kind === "float") {
    const input = h("input", {id, class: "input", type: "number", inputmode: kind === "int" ? "numeric" : "decimal",
      min: setting.min, max: setting.max, step: setting.step || "any", value: String(value ?? "")});
    input.addEventListener("input", onChange);
    const wrap = setting.unit ? h("div", {class: "input-unit"}, input, h("span", {}, setting.unit)) : input;
    return {input: wrap, id, get() {
      const number = kind === "int" ? Number.parseInt(input.value, 10) : Number.parseFloat(input.value.replace(",", "."));
      if (!Number.isFinite(number) || number < setting.min || number > setting.max || (kind === "int" && String(number) !== input.value.trim())) {
        return INVALID;
      }
      return number;
    }, set: v => { input.value = String(v); }, error: `Use um valor de ${String(setting.min).replace(".", ",")} a ${String(setting.max).replace(".", ",")}${setting.unit && setting.unit !== "US$" ? " " + setting.unit : ""}.`};
  }
  if (["choice", "model", "voice", "device", "gpio"].includes(kind)) {
    const select = h("select", {id, class: "input"});
    const fill = (selected) => {
      let options = [];
      if (kind === "choice") options = setting.choices;
      if (kind === "model") options = ctx.models.filter(m => !m.retired || m.id === selected).map(m => [m.id, m.label + (m.deprecated ? " · legado" : "")]);
      if (kind === "voice") {
        const model = ctx.models.find(m => m.id === ctx.draft("voice.model")) || {voices: []};
        options = model.voices.map(v => [v, v[0].toUpperCase() + v.slice(1)]);
        if (!model.voices.includes(selected)) selected = model.voices[0];
      }
      if (kind === "device") {
        options = ctx.devices.filter(d => d.class === setting.source).map(d => [d.name, d.description || d.name]);
        if (selected && !options.some(([name]) => name === selected)) options.unshift([selected, `${selected} (desconectado)`]);
        if (!options.length) options = [["", "Nenhum dispositivo encontrado"]];
      }
      if (kind === "gpio") options = Object.entries(ctx.gpio).map(([gpio, pin]) => [Number(gpio), `GPIO ${gpio} · pino físico ${pin}`]);
      replace(select, options.map(([v, label]) => h("option", {value: String(v)}, label)));
      select.value = String(selected ?? "");
    };
    fill(value);
    select.addEventListener("change", onChange);
    return {input: select, id, refill: () => fill(select.value),
      get: () => kind === "gpio" ? Number(select.value) : select.value, set: v => fill(v)};
  }
  if (kind === "text" || kind === "string") {
    const area = h("textarea", {id, class: `input ${kind === "text" ? "textarea-long" : ""}`, rows: kind === "text" ? "16" : "3", spellcheck: "true"});
    area.value = value ?? "";
    const counter = h("span", {class: "counter"});
    const count = () => {
      const size = kind === "text" ? new TextEncoder().encode(area.value).length : area.value.length;
      text(counter, `${fmt.number(size)} / ${fmt.number(setting.max)} ${kind === "text" ? "bytes" : "caracteres"}`);
      counter.classList.toggle("over", size > setting.max);
    };
    area.addEventListener("input", () => { count(); onChange(); });
    count();
    return {input: h("div", {class: "textarea-wrap"}, area, counter), id, get() {
      const size = kind === "text" ? new TextEncoder().encode(area.value).length : area.value.length;
      if (size > setting.max || (kind === "text" && !area.value.trim())) return INVALID;
      return kind === "string" ? area.value.trim() : area.value;
    }, set: v => { area.value = v ?? ""; count(); }, error: kind === "text" ? "Escreva as instruções da Noha." : `Até ${setting.max} caracteres.`};
  }
  if (kind === "texts") {
    const list = h("ol", {class: "phrase-list"});
    const add = h("button", {type: "button", class: "btn btn-ghost"}, icon("plus", 18), "Adicionar frase");
    const counter = h("span", {class: "counter"});
    const refresh = () => {
      const items = list.children.length;
      add.disabled = items >= setting.max;
      text(counter, `${items} de ${setting.max}`);
      for (const remove of list.querySelectorAll(".phrase-remove")) remove.disabled = items <= setting.min;
    };
    const item = phrase => {
      const area = h("textarea", {class: "input", rows: "2", maxlength: "1000", "aria-label": "Frase de saudação"});
      area.value = phrase;
      area.addEventListener("input", onChange);
      const remove = h("button", {type: "button", class: "btn-icon phrase-remove", "aria-label": "Remover frase",
        onclick: () => { li.remove(); refresh(); onChange(); }}, icon("trash", 18));
      const li = h("li", {class: "phrase"}, area, remove);
      return li;
    };
    const set = phrases => { replace(list, (phrases || []).map(item)); refresh(); };
    add.addEventListener("click", () => { list.append(item("")); refresh(); onChange(); list.lastChild.querySelector("textarea").focus(); });
    set(value);
    return {input: h("div", {class: "phrases"}, list, h("div", {class: "phrases-foot"}, add, counter)), id, set, get() {
      const phrases = [...list.querySelectorAll("textarea")].map(area => area.value.trim()).filter(Boolean);
      return phrases.length >= setting.min && phrases.length <= setting.max ? phrases : INVALID;
    }, error: "Escreva pelo menos uma frase."};
  }
  if (kind === "secret") {
    const input = h("input", {id, class: "input", type: "password", autocomplete: "new-password", spellcheck: "false", maxlength: "512",
      placeholder: ctx.keyConfigured ? "Chave salva · deixe vazio para manter" : "sk-…"});
    const show = h("button", {type: "button", class: "btn-icon", "aria-label": "Mostrar chave",
      onclick: () => { input.type = input.type === "password" ? "text" : "password"; }}, icon("eye", 18));
    input.addEventListener("input", onChange);
    return {input: h("div", {class: "input-action"}, input, show), id, secret: true,
      get: () => input.value.trim(), set: () => { input.value = ""; }};
  }
  return {input: h("p", {}, String(value)), id, get: () => value, set() {}};
}

export function mount(root, ctx) {
  let data = null, group = "personalidade", fields = new Map(), baseline = {}, pendingSeen = false;
  const nav = h("nav", {class: "subnav", "aria-label": "Grupos de ajustes"});
  const panel = h("div", {class: "settings-panel"});
  const banner = h("div", {class: "banner", hidden: true});
  replace(root,
    h("header", {class: "page-head"}, h("div", {}, h("p", {class: "eyebrow"}, "AJUSTES"), h("h1", {}, "Do jeito da casa"),
      h("p", {class: "muted"}, "Noha aplica as alterações quando estiver livre. A voz reinicia em alguns segundos."))),
    banner,
    h("div", {class: "settings-layout"}, nav, panel));
  replace(panel, h("div", {class: "card"}, h("div", {class: "skeleton"}), h("div", {class: "skeleton short"})));

  const merged = () => ({...data.values, ...Object.fromEntries(Object.entries(data.pending).filter(([key]) => key !== "openai_key"))});
  const dirtyFields = () => [...fields].filter(([key, field]) => field.secret ? field.get() !== "" : !same(field.get(), baseline[key]));

  function drawNav() {
    replace(nav, data.schema.groups.map(g => h("a", {href: `#ajustes/${g.id}`, class: "subnav-item", "aria-current": g.id === group ? "page" : null},
      icon(GROUP_ICONS[g.id] || "settings", 18), h("span", {}, g.label),
      Object.keys(data.pending).some(key => settingFor(key)?.group === g.id) ? h("span", {class: "dot dot-warn", title: "Alterações pendentes"}) : null)));
  }

  const settingFor = key => data.schema.settings.find(s => s.key === key);

  function drawBanner(status) {
    const pending = Object.keys(data?.pending || {});
    const apply = status?.apply || data?.apply || {};
    if (pending.length) {
      const labels = pending.map(key => settingFor(key)?.label || key).join(", ");
      const discard = h("button", {class: "btn btn-ghost btn-small", type: "button"}, "Descartar");
      discard.addEventListener("click", async () => {
        if (!await confirmDialog({title: "Descartar alterações pendentes?", message: labels, confirm: "Descartar", danger: true})) return;
        await busy(discard, async () => {
          try { toast((await api("discard", {})).message); await load(); } catch (failure) { toast(failure.message, "error"); }
        });
      });
      banner.className = "banner banner-warn";
      replace(banner, icon("clock", 18), h("div", {}, h("strong", {}, "Aguardando aplicação"),
        h("span", {}, `${labels}. Noha aplica quando estiver livre.`)), discard);
      banner.hidden = false;
    } else if (apply.state === "error") {
      banner.className = "banner banner-error";
      replace(banner, icon("alert", 18), h("div", {}, h("strong", {}, "As últimas alterações não funcionaram"), h("span", {}, apply.message || "A configuração anterior foi restaurada.")));
      banner.hidden = false;
    } else if (apply.state === "applied" && apply.at && Date.now() / 1000 - apply.at < 600) {
      banner.className = "banner banner-ok";
      replace(banner, icon("check", 18), h("div", {}, h("strong", {}, "Alterações aplicadas"), h("span", {}, `às ${fmt.time(apply.at)}.`)));
      banner.hidden = false;
    } else banner.hidden = true;
  }

  function drawGroup() {
    fields = new Map();
    baseline = merged();
    const info = data.schema.groups.find(g => g.id === group) || data.schema.groups[0];
    group = info.id;
    const status = h("p", {class: "form-status", role: "status"});
    const save = h("button", {class: "btn btn-primary", type: "submit", disabled: true}, "Salvar alterações");
    const undo = h("button", {class: "btn btn-ghost", type: "button", disabled: true}, "Desfazer");
    const changed = () => {
      const dirty = dirtyFields().length > 0;
      save.disabled = !dirty;
      undo.disabled = !dirty;
      text(status, dirty ? "Alterações não salvas" : "");
      fields.get("voice.voice")?.refill?.();
    };
    const fieldCtx = {models: data.models, devices: data.devices, gpio: data.gpio_pins, keyConfigured: data.openai_key_configured,
      draft: key => fields.get(key)?.get() ?? baseline[key]};
    const body = [], advanced = [];
    for (const setting of data.schema.settings.filter(s => s.group === group)) {
      const field = control(setting, setting.kind === "secret" ? "" : baseline[setting.key], fieldCtx, changed);
      const error = h("p", {class: "field-error", role: "alert"});
      field.error_el = error;
      fields.set(setting.key, field);
      const pending = setting.key in data.pending ? h("span", {class: "badge badge-warn"}, "pendente") : null;
      const help = [setting.help, setting.kind === "model" ? priceHint(data.models.find(m => m.id === baseline[setting.key])) : "",
        setting.kind === "secret" ? (data.openai_key_configured ? "Uma chave está salva." : "Nenhuma chave salva: Noha não conversa.") : ""].filter(Boolean).join(" ");
      const label = h("label", {class: "label", htmlFor: field.id}, setting.label, pending);
      const block = field.inline
        ? h("div", {class: "field field-inline"}, h("div", {}, label, help ? h("p", {class: "help"}, help) : null), field.input)
        : h("div", {class: "field"}, label, field.input, help ? h("p", {class: "help"}, help) : null, error);
      (setting.advanced ? advanced : body).push(block);
    }
    const form = h("form", {class: "card settings-card", novalidate: true},
      h("div", {class: "card-head"}, h("div", {}, h("h2", {class: "card-title"}, icon(GROUP_ICONS[group] || "settings", 20), info.label),
        h("p", {class: "help"}, info.help))),
      GROUP_NOTES[group] ? h("p", {class: "note"}, icon("info", 16), GROUP_NOTES[group]) : null,
      body,
      advanced.length ? h("details", {class: "advanced"}, h("summary", {}, icon("down", 16), "Ajustes avançados"), advanced) : null,
      h("div", {class: "form-actions"}, status, undo, save));
    undo.addEventListener("click", () => { for (const [key, field] of fields) field.set(baseline[key]); changed(); });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const changes = {};
      let invalid = null;
      for (const [key, field] of fields) {
        const value = field.get();
        text(field.error_el, "");
        if (value === INVALID) { text(field.error_el, field.error || "Valor inválido."); invalid = invalid || field; continue; }
        if (field.secret ? value !== "" : !same(value, baseline[key])) changes[key] = value;
      }
      if (invalid) { text(status, "Corrija os campos destacados."); return; }
      if (!Object.keys(changes).length) return;
      await busy(save, async () => {
        try {
          const result = await api("settings", {changes});
          data.pending = result.pending;
          toast(result.message);
          drawNav();
          drawBanner();
          drawGroup();
        } catch (failure) { text(status, failure.message); toast(failure.message, "error"); }
      });
    });
    replace(panel, form);
    drawNav();
  }

  async function load() {
    try {
      data = await api("settings");
      pendingSeen = Object.keys(data.pending).length > 0;
      drawBanner();
      drawGroup();
    } catch (failure) {
      replace(panel, h("div", {class: "card"}, empty(failure.message, "alert")));
    }
  }

  async function leave() {
    if (!data || !dirtyFields().length) return true;
    return confirmDialog({title: "Descartar alterações não salvas?", message: "As mudanças deste grupo ainda não foram salvas.",
      confirm: "Descartar", danger: true});
  }

  load();
  return {
    async show(section) {
      const next = section || group;
      if (!data) { group = next; return; }
      if (next === group) return;
      if (!await leave()) { history.replaceState(null, "", `#ajustes/${group}`); return; }
      group = next;
      drawGroup();
      window.scrollTo({top: 0, behavior: "smooth"});
    },
    update(status) {
      if (!data) return;
      // Reload once the robot applied what was pending, unless the open form has unsaved edits.
      if (pendingSeen && !status.pending && !dirtyFields().length) { pendingSeen = false; load(); return; }
      pendingSeen = pendingSeen || status.pending;
      drawBanner(status);
    },
    dirty: leave,
    isDirty: () => !!data && dirtyFields().length > 0,
  };
}
