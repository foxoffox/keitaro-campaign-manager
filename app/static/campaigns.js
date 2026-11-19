const form = document.querySelector("#campaign-form");
const fields = document.querySelector("#fields");
const create = document.querySelector("#create");
const reload = document.querySelector("#reload");
const result = document.querySelector("#result");
const connection = document.querySelector("#connection");
const openEditor = document.querySelector("#open-editor");
const catalogs = {
  domain_id: "domains",
  group_id: "groups",
  traffic_source_id: "traffic_sources",
  offer_id: "offers",
};
const formFields = ["name", "country", ...Object.keys(catalogs)];
const storageKey = "keitaro-campaign-request";
let lastRequest;
let submitting = false;

try {
  lastRequest = JSON.parse(sessionStorage.getItem(storageKey));
} catch {
  lastRequest = null;
}

function feedback(state, message, focus = false) {
  result.hidden = !message;
  result.dataset.state = state;
  result.textContent = message || "";
  if (focus) result.focus();
}

function updateRoutePreview() {
  const country = form.elements.country.value.trim().toUpperCase();
  const offer = form.elements.offer_id;
  document.querySelector("#geo-preview").textContent =
    /^[A-Z]{2}$/.test(country) ? country : "Выбранное GEO";
  document.querySelector("#offer-preview").textContent =
    offer.value ? offer.selectedOptions[0].textContent : "Выбранный оффер";
}

function formatError(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (!detail?.message) return fallback;
  let message = detail.message;
  if (detail.campaign_id) message += " Кампания №" + detail.campaign_id + ".";
  if (detail.stage && detail.stage !== "validation") {
    message += " Alias: " + detail.alias + ".";
  }
  return message;
}

async function loadCatalogs() {
  const loading = document.querySelector("#catalog-loading");
  const workspace = document.querySelector("#create-workspace");
  loading.hidden = false;
  workspace.hidden = true;
  fields.disabled = create.disabled = true;
  reload.hidden = true;
  connection.dataset.state = "";
  connection.textContent = "Загрузка справочников…";
  feedback("", "");
  try {
    const response = await fetch("/api/keitaro/catalogs");
    const data = await response.json();
    if (!response.ok) throw Error(formatError(data.detail, "Ошибка загрузки справочников."));

    const empty = [];
    for (const [field, catalog] of Object.entries(catalogs)) {
      const select = form.elements[field];
      const items = data[catalog];
      select.replaceChildren(new Option(items.length ? "Выбрать" : "Нет доступных записей", ""));
      select.options[0].disabled = true;
      for (const item of items) select.add(new Option(item.name + " · #" + item.id, item.id));
      if (items.length === 1) select.selectedIndex = 1;
      if (!items.length) empty.push(document.querySelector('label[for="' + field + '"]').textContent);
    }
    connection.dataset.state = "ready";
    connection.textContent = "Справочники Keitaro загружены";
    if (empty.length) {
      feedback("error", "В Keitaro нет доступных записей для полей «" + empty.join("», «") + "».");
      reload.hidden = false;
    } else {
      fields.disabled = create.disabled = false;
    }
  } catch (error) {
    connection.dataset.state = "error";
    connection.textContent = "Не удалось загрузить справочники";
    feedback("error", error.message);
    reload.hidden = false;
  } finally {
    updateRoutePreview();
    loading.hidden = true;
    workspace.hidden = false;
  }
}

function clearError(input) {
  input.removeAttribute("aria-invalid");
  document.getElementById(input.name + "-error").textContent = "";
}

function showValidation(errors) {
  let first;
  for (const error of errors) {
    const name = error.loc?.[1];
    if (!formFields.includes(name)) continue;
    const input = form.elements[name];
    input.setAttribute("aria-invalid", "true");
    document.getElementById(name + "-error").textContent =
      error.msg.replace(/^Value error, /, "");
    first ??= input;
  }
  return first;
}

form.addEventListener("input", (event) => {
  if (formFields.includes(event.target.name)) clearError(event.target);
  if (result.dataset.state === "validation" && !form.querySelector('[aria-invalid="true"]')) {
    feedback("", "");
  }
});
form.elements.country.addEventListener("input", updateRoutePreview);
form.elements.offer_id.addEventListener("change", updateRoutePreview);
reload.addEventListener("click", loadCatalogs);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting || create.disabled) return;
  for (const name of formFields) clearError(form.elements[name]);

  const payload = {
    name: form.elements.name.value.trim(),
    country: form.elements.country.value.trim().toUpperCase(),
  };
  for (const field of Object.keys(catalogs)) payload[field] = Number(form.elements[field].value);
  const signature = JSON.stringify(payload);
  payload.request_id = lastRequest?.signature === signature ? lastRequest.id : crypto.randomUUID();
  lastRequest = {signature, id: payload.request_id};
  try { sessionStorage.setItem(storageKey, JSON.stringify(lastRequest)); } catch {}

  submitting = true;
  openEditor.hidden = true;
  fields.disabled = create.disabled = true;
  create.textContent = "Создаю кампанию…";
  feedback("", "Keitaro сохраняет кампанию и проверяет настройки потоков.");
  let focusTarget = result;
  try {
    const response = await fetch("/api/campaigns", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 422 && Array.isArray(data.detail)) {
        focusTarget = showValidation(data.detail) || result;
        feedback("validation", "Проверь поля формы.");
      } else {
        feedback("error", formatError(data.detail, "Не удалось создать кампанию."));
      }
      return;
    }
    feedback("success", "Кампания №" + data.id + " создана и включена. Alias: " + data.alias + ".");
    openEditor.href = "/editor?campaign=" + encodeURIComponent(data.id);
    openEditor.hidden = false;
    lastRequest = null;
    try { sessionStorage.removeItem(storageKey); } catch {}
    form.elements.name.value = "";
  } catch {
    feedback("error", "Связь прервалась. Повтори отправку с теми же полями, чтобы проверить результат.");
  } finally {
    submitting = false;
    fields.disabled = create.disabled = false;
    create.textContent = "Создать кампанию";
    focusTarget.focus();
  }
});

loadCatalogs();
