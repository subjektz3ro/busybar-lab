// Execute the shipped client with controlled responses and a minimal DOM.
// No server, device, browser download, or third-party test package is needed.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function client() {
  const elements = new Map();
  const makeElement = () => ({
    innerHTML: "", textContent: "", hidden: false, dataset: {}, inputs: [],
    segments: [], chips: [], listeners: {},
    classList: { toggle() {}, contains() { return false; } },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelectorAll(selector) {
      return selector === "input" ? this.inputs
        : selector === ".seg" ? this.segments : selector === ".chips" ? this.chips : [];
    },
    contains() { return false; },
    closest() { return makeElement(); },
    replaceChildren(...children) { this.children = children; },
    appendChild() {}, setAttribute() {}, focus() {},
  });
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {
      getElementById: element, body: makeElement(), activeElement: null,
      querySelectorAll: () => [], createElement: makeElement,
    },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    Image: class {}, setInterval() {}, setTimeout() {},
    window: { prompt() { throw new Error("unexpected login"); } },
    fetch: async () => ({ ok: true, json: async () => ({
      apps: [], foreground: null, source: "off", managed: false,
    }) }),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../barkeep/static/app.js"), "utf8"), context);
  await new Promise(setImmediate);  // finish the client's initial polling
  return { context, element, run: (code) => vm.runInContext(code, context) };
}

function pendingResponse() {
  let finish;
  const promise = new Promise((resolve) => { finish = resolve; });
  return { promise, finish: (body) => finish({ ok: true, json: async () => body }) };
}

function config(value) {
  return { keys: [{ name: "SHARED_KEY", description: "setting", value, default: "", type: "text" }] };
}

test("an earlier app's delayed config cannot replace the selected app's form", async () => {
  const ui = await client(), first = pendingResponse(), second = pendingResponse();
  ui.context.fetch = (url) => url.includes("alpha") ? first.promise : second.promise;
  const a = ui.run('selectedApp = "alpha"; loadConfig()');
  const b = ui.run('selectedApp = "beta"; loadConfig()');
  second.finish(config("beta value"));
  await b;
  first.finish(config("alpha value"));
  await a;
  assert.match(ui.element("config-form").innerHTML, /beta value/);
  assert.doesNotMatch(ui.element("config-form").innerHTML, /alpha value/);
  assert.equal(ui.run("configLoadedFor"), "beta");
});

test("an in-flight reload cannot discard a subsequent edit", async () => {
  const ui = await client(), response = pendingResponse();
  ui.context.fetch = () => response.promise;
  ui.run('selectedApp = configLoadedFor = "alpha"');
  ui.element("config-form").innerHTML = "edited form";
  ui.element("config-form").inputs = [{ value: "edited", dataset: { initial: "old" } }];
  const reload = ui.run("loadConfig()");
  ui.element("config-form").listeners.input();
  response.finish(config("old"));
  await reload;
  assert.equal(ui.element("config-form").innerHTML, "edited form");
  assert.equal(ui.run("configDirty"), true);
});

test("returning chips to their initial value does not clear another field's edit", async () => {
  const ui = await client();
  ui.element("config-form").inputs = [{ value: "edited", dataset: { initial: "old" } }];
  const chips = { dataset: { value: "one,two", initial: "one", choices: "one,two" } };
  ui.element("config-form").chips = [chips];
  const pick = {
    dataset: { pick: "two" }, closest: () => chips,
    classList: { toggle() {} }, setAttribute() {},
  };
  await ui.context.document.body.listeners.click({ target: { closest: () => pick } });
  assert.equal(chips.dataset.value, "one");
  assert.equal(ui.run("configDirty"), true);
});

test("a form from a previous app cannot be submitted to the new selection", async () => {
  const ui = await client();
  let requests = 0;
  ui.context.fetch = async () => { requests++; return { ok: true, json: async () => config("saved") }; };
  ui.run('selectedApp = "beta"; configLoadedFor = "alpha"');
  ui.element("config-form").inputs = [{ name: "SHARED_KEY", value: "edited", dataset: { initial: "old" } }];
  await ui.run("saveConfig(false)");
  assert.equal(requests, 0);
});

test("a save response cannot discard edits made while saving", async () => {
  const ui = await client(), response = pendingResponse();
  ui.context.fetch = (url) => url === "/api/state"
    ? Promise.resolve({ ok: true, json: async () => ({ apps: [], foreground: null }) })
    : response.promise;
  ui.run('selectedApp = configLoadedFor = "alpha"; configDirty = true');
  const input = { name: "SHARED_KEY", value: "first edit", dataset: { initial: "old" } };
  ui.element("config-form").inputs = [input];
  const save = ui.run("saveConfig(false)");
  input.value = "second edit";
  ui.element("config-form").listeners.input();
  response.finish(config("first edit"));
  await save;
  assert.equal(ui.run("configDirty"), true);
  assert.equal(input.value, "second edit");
  assert.equal(input.dataset.initial, "first edit");
});

test("reverting a field during a save remains an edit against the saved value", async () => {
  const ui = await client(), response = pendingResponse();
  ui.context.fetch = (url) => url === "/api/state"
    ? Promise.resolve({ ok: true, json: async () => ({ apps: [], foreground: null }) })
    : response.promise;
  ui.run('selectedApp = configLoadedFor = "alpha"; configDirty = true');
  const input = { name: "SHARED_KEY", value: "first edit", dataset: { initial: "old" } };
  ui.element("config-form").inputs = [input];
  const save = ui.run("saveConfig(false)");
  input.value = "old";
  ui.element("config-form").listeners.input();
  response.finish(config("first edit"));
  await save;
  assert.equal(input.value, "old");
  assert.equal(input.dataset.initial, "first edit");
  assert.equal(ui.run("configDirty"), true);
});

for (const kind of ["segments", "chips"]) {
  test(`reverting ${kind} during a save keeps the new baseline`, async () => {
    const ui = await client(), response = pendingResponse();
    ui.context.fetch = (url) => url === "/api/state"
      ? Promise.resolve({ ok: true, json: async () => ({ apps: [], foreground: null }) })
      : response.promise;
    ui.run('selectedApp = configLoadedFor = "alpha"; configDirty = true');
    const control = { dataset: { key: "SHARED_KEY", value: "two", initial: "one" } };
    ui.element("config-form")[kind] = [control];
    const save = ui.run("saveConfig(false)");
    control.dataset.value = "one";
    ui.run("markConfigEdited()");
    response.finish(config("two"));
    await save;
    assert.equal(control.dataset.value, "one");
    assert.equal(control.dataset.initial, "two");
    assert.equal(ui.run("configDirty"), true);
  });
}

test("a saved blank adopts its effective default without losing another field's edit", async () => {
  const ui = await client(), response = pendingResponse();
  ui.context.fetch = (url) => url === "/api/state"
    ? Promise.resolve({ ok: true, json: async () => ({ apps: [], foreground: null }) })
    : response.promise;
  ui.run('selectedApp = configLoadedFor = "alpha"; configDirty = true');
  const cleared = { name: "SHARED_KEY", value: "", dataset: { initial: "override" } };
  const other = { name: "OTHER", value: "old", dataset: { initial: "old" } };
  ui.element("config-form").inputs = [cleared, other];
  const save = ui.run("saveConfig(false)");
  other.value = "new";
  ui.element("config-form").listeners.input();
  response.finish(config("default"));
  await save;
  assert.equal(cleared.value, "default");
  assert.equal(cleared.dataset.initial, "default");
  assert.equal(other.value, "new");
  assert.equal(ui.run("configDirty"), true);
});
