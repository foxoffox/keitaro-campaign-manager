const campaign = document.querySelector("#campaign");
const refresh = document.querySelector("#refresh");
const notice = document.querySelector("#notice");
const loading = document.querySelector("#editor-loading");
const editor = document.querySelector("#editor");
const rows = document.querySelector("#offer-rows");
const draftState = document.querySelector("#draft-state");
const cancel = document.querySelector("#cancel-draft");
const publish = document.querySelector("#publish-draft");
const addForm = document.querySelector("#add-offer");
const search = document.querySelector("#offer-search");
const choice = document.querySelector("#offer-choice");
const catalogHint = document.querySelector("#catalog-hint");
const addButton = document.querySelector("#add-button");

const api = "/api/editor/campaigns";
const rowStates = {
  active: "Активен",
  disabled: "Выключен в Keitaro",
  removed: "Удалён из потока",
};
const actionNames = {
  pin: "Закрепить", unpin: "Снять закрепление", remove: "Удалить", restore: "Вернуть",
};
let view = null;
let busy = false;
let needsRefresh = false;

function showNotice(message, state = "") {
  notice.hidden = !message;
  notice.dataset.state = state;
  notice.textContent = message;
}

async function request(url, options) {
  const response = await fetch(url, {cache: "no-store", ...options});
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail;
    const error = new Error(typeof detail === "string" ? detail : "Не удалось выполнить запрос.");
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function setBusy(value) {
  busy = value;
  campaign.disabled = value || campaign.options.length === 0;
  refresh.disabled = value || !campaign.value;
  editor.setAttribute("aria-busy", String(value));
  const locked = value || needsRefresh || view?.draft.publication_pending;
  for (const button of rows.querySelectorAll("button")) {
    button.disabled = locked || view?.draft.upstream_changed || button.dataset.unavailable === "true";
  }
  cancel.disabled = locked;
  publish.disabled = locked || !view?.draft.can_publish;
  updateCatalog();
}

function element(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function rowButton(action, row, unavailable = false) {
  const button = element("button", actionNames[action], "secondary");
  button.type = "button";
  button.dataset.action = action;
  button.dataset.unavailable = String(unavailable);
  button.setAttribute("aria-label", actionNames[action] + " оффер " + row.offer_id);
  if (unavailable) button.title = "Оффер недоступен в справочнике Keitaro";
  button.addEventListener("click", () => edit(action, row.offer_id));
  return button;
}

function renderRows() {
  const catalogIds = new Set(view.catalog.map(item => item.id));
  const draftRows = view.draft.rows;
  rows.replaceChildren();
  for (const row of draftRows) {
    const item = element("article", "", "offer-row");
    item.dataset.id = row.offer_id;
    item.dataset.state = row.state;
    item.setAttribute("role", "listitem");
    const identity = element("div", "", "offer-identity");
    identity.append(element("strong", row.name), element("span", "#" + row.offer_id, "offer-id"));
    const status = element("span", rowStates[row.state], "offer-status");


    const share = element("div", "", "offer-share");
    share.append(element("strong", (row.state === "active" ? row.share : 0) + "%"));
    if (row.state === "active" && row.pinned_share !== null) {
      share.append(element("span", "Закреплена", "pinned-label"));
    }

    const actions = element("div", "", "offer-actions");
    if (row.state === "active") {
      actions.append(rowButton(row.pinned_share === null ? "pin" : "unpin", row));
    }
    if (row.state === "removed") {
      actions.append(rowButton("restore", row, !catalogIds.has(row.offer_id)));
      if (!catalogIds.has(row.offer_id)) actions.append(element("span", "Нет в справочнике", "hint"));
    } else {
      actions.append(rowButton("remove", row));
    }
    item.append(identity, status, share, actions);
    rows.append(item);
  }
  document.querySelector("#empty-offers").hidden = draftRows.length > 0;
}

function updateCatalog() {
  const assigned = new Set(view?.draft.rows.map(row => row.offer_id) ?? []);
  const candidates = view?.catalog.filter(item => !assigned.has(item.id)) ?? [];
  const query = search.value.trim().toLocaleLowerCase("ru");
  const matches = candidates.filter(item => (
    String(item.id).includes(query) || item.name.toLocaleLowerCase("ru").includes(query)
  ));
  const previous = choice.value;
  choice.replaceChildren(new Option("Выбрать оффер", ""));
  for (const item of matches) choice.add(new Option(item.name + " · #" + item.id, item.id));
  if (matches.some(item => String(item.id) === previous)) choice.value = previous;
  else if (matches.length === 1) choice.selectedIndex = 1;
  catalogHint.hidden = !view || matches.length > 0;
  catalogHint.textContent = candidates.length
    ? "По этому запросу офферы не найдены."
    : "Все доступные офферы уже в списке. Удалённые можно вернуть оттуда.";
  const locked = busy || !view || needsRefresh || view.draft.upstream_changed || view.draft.publication_pending;
  search.disabled = locked || candidates.length === 0;
  choice.disabled = locked || matches.length === 0;
  addButton.disabled = choice.disabled || !choice.value;
}

function renderComparison(draft) {
  const published = draft.published_rows;
  const previous = new Map(published.filter(row => row.state === "active").map(row => [row.offer_id, row]));
  const proposed = new Map(draft.rows.filter(row => row.state === "active").map(row => [row.offer_id, row]));
  const ids = new Set([...previous.keys(), ...proposed.keys()]);
  const chart = document.querySelector("#comparison-rows");
  chart.replaceChildren();
  document.querySelector("#draft-key").hidden = !draft.has_draft;
  document.querySelector("#empty-comparison").hidden = ids.size > 0;

  for (const id of ids) {
    const before = previous.get(id);
    const after = proposed.get(id);
    const row = element("div", "", "comparison-row");
    const name = element("span", (after ?? before).name, "comparison-name");
    const bars = element("div", "", "comparison-bars");
    bars.append(shareBar("В Keitaro", before?.share ?? 0));
    if (draft.has_draft) bars.append(shareBar("Черновик", after?.share ?? 0, true));
    row.append(name, bars);
    chart.append(row);
  }
}

function shareBar(label, share, isDraft = false) {
  const bar = element("div", "", isDraft ? "share-bar proposed" : "share-bar");
  const fill = element("i", "");
  fill.style.width = Math.min(100, share) + "%";
  fill.setAttribute("aria-hidden", "true");
  bar.append(fill, element("span", label + " ", "sr-only"), element("b", share + "%"));
  return bar;
}

function renderCharts(active, draft) {
  document.querySelector(".ring").style.setProperty("--total", Math.min(100, draft.total) + "%");
  renderComparison(draft);
  const matrix = document.querySelector("#activity-map");
  matrix.replaceChildren();
  // One cell per assignment for small campaigns, proportional coverage for larger ones.
  const count = Math.min(32, draft.rows.length);
  const filled = draft.rows.length ? Math.round(active.length * count / draft.rows.length) : 0;
  for (let index = 0; index < count; index++) {
    matrix.append(element("i", "", index < filled ? "filled" : ""));
  }
  document.querySelector("#active-caption").textContent = "Всего в списке · " + draft.rows.length;
  const pinnedShare = active.reduce((sum, row) => sum + (row.pinned_share === null ? 0 : row.share), 0);
  document.querySelector("#pin-bar").style.width = pinnedShare + "%";
  document.querySelector("#pinned-share").textContent = pinnedShare + "%";
  const pinnedCount = active.filter(row => row.pinned_share !== null).length;
  document.querySelector("#pin-caption").textContent = "Офферов с закреплённой долей · " + pinnedCount;
}

function renderDraft() {
  const draft = view.draft;
  const active = draft.rows.filter(row => row.state === "active");
  document.querySelector("#active-count").textContent = active.length;
  document.querySelector("#total-share").textContent = draft.total + "%";
  renderCharts(active, draft);
  draftState.textContent = draft.has_draft ? "Черновик сохранён" : "Синхронизировано";
  draftState.dataset.pending = String(draft.has_draft);
  const warning = document.querySelector("#share-warning");
  warning.hidden = !draft.warning;
  warning.textContent = draft.warning ?? "";
  document.querySelector("#conflict").hidden = !draft.upstream_changed;
  document.querySelector("#publication-pending").hidden = !draft.publication_pending;
  cancel.hidden = !draft.has_draft;
  publish.hidden = !draft.has_draft;
  renderRows();
}

async function loadCampaign() {
  if (busy || !campaign.value) return;
  const id = campaign.value;
  history.replaceState(null, "", "/editor?campaign=" + encodeURIComponent(id));
  setBusy(true);
  loading.hidden = false;
  editor.hidden = true;
  showNotice("Читаю потоки и справочник Keitaro…");
  try {
    view = await request(api + "/" + id);
    needsRefresh = false;
    campaign.selectedOptions[0].textContent = view.campaign_name + " · #" + id;
    search.value = "";
    document.querySelector("#offer-stream").textContent = "Поток #" + view.offer_stream_id;
    document.querySelector("#geo-flow").textContent =
      "#" + view.geo_stream_id + (view.countries.length ? " · " + view.countries.join(", ") : "");
    document.querySelector("#fallback-flow").textContent = "#" + view.offer_stream_id;
    document.querySelector("#campaign-state").textContent = "Кампания " +
      (view.campaign_state === "active" ? "включена" : "выключена");
    renderDraft();
    editor.hidden = false;
    showNotice("");
  } catch (error) {
    view = null;
    editor.hidden = true;
    showNotice(error.message, "error");
  } finally {
    loading.hidden = true;
    setBusy(false);
  }
}

async function edit(action, offerId) {
  if (busy || !view || needsRefresh) return;
  const publishing = action === "publish";
  setBusy(true);
  showNotice(publishing ? "Сверяю поток и публикую черновик…" : "");
  let focusTarget;
  try {
    const payload = publishing
      ? {revision: view.draft.revision}
      : {action, revision: view.draft.revision, offer_id: offerId};
    view.draft = await request(api + "/" + campaign.value + (publishing ? "/publish" : "/draft"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (action === "add") search.value = "";
    if (action === "cancel") showNotice("Черновик отменён.");
    if (publishing) showNotice("Опубликовано. Состав и доли подтверждены в Keitaro.", "success");
    renderDraft();
    focusTarget = rows.querySelector('[data-id="' + offerId + '"] button') ?? draftState;
  } catch (error) {
    needsRefresh = error.status === 409 || (publishing && (!error.status || error.status >= 500));
    const message = error.status ? error.message : "Связь прервалась. Обнови состояние из Keitaro.";
    showNotice(message, "error");
    focusTarget = notice;
  } finally {
    setBusy(false);
    focusTarget?.focus();
  }
}

async function start() {
  showNotice("Загружаю список кампаний…");
  try {
    const campaigns = await request(api);
    if (campaigns.length === 0) {
      showNotice("Пока нет кампаний. Создай первую через обёртку.");
      return;
    }
    for (const item of campaigns) {
      campaign.add(new Option(item.name ? item.name + " · #" + item.id : "#" + item.id, item.id));
    }
    const requested = new URLSearchParams(location.search).get("campaign");
    if (campaigns.some(item => String(item.id) === requested)) campaign.value = requested;
    await loadCampaign();
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    loading.hidden = true;
  }
}

campaign.addEventListener("change", loadCampaign);
refresh.addEventListener("click", loadCampaign);
search.addEventListener("input", updateCatalog);
choice.addEventListener("change", () => { addButton.disabled = !choice.value; });
addForm.addEventListener("submit", event => {
  event.preventDefault();
  if (choice.value) edit("add", Number(choice.value));
});
cancel.addEventListener("click", () => edit("cancel"));
publish.addEventListener("click", () => edit("publish"));
start();
