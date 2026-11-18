import { readFolder, updateEntry, verifyPdf } from "./personal.js";
import { canLoadSummary, loadSummary } from "./summary.js";

const byId = (id) => document.getElementById(id);
const state = { articles: [], topics: [], topic: "", limit: 20 };
const folder = new Map();
const pendingBookmarks = new Set();
const results = byId("results");
const dialog = byId("article-dialog");
let summaryRequest;
let indexRequest;
let loadedVersion;
const dateFormat = new Intl.DateTimeFormat("en-AU", {
  day: "numeric", month: "short", year: "numeric", timeZone: "Australia/Sydney",
});
const refreshTimeFormat = new Intl.DateTimeFormat("en-AU", {
  day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
  timeZone: "Australia/Sydney", timeZoneName: "short",
});
const countFormat = new Intl.NumberFormat("en-AU");

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function externalLink(label, url) {
  const link = element("a", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function formattedDate(date) {
  return dateFormat.format(new Date(date));
}

function normalize(value) {
  return value.normalize("NFKD").replace(/\p{Diacritic}/gu, "").toLowerCase();
}

function topicLabel(topic) {
  return topic || "All evidence";
}

function setTopic(topic) {
  state.topic = topic;
  state.limit = 20;
  byId("topic").value = topic;
  render();
}

function buildTopics() {
  const counts = new Map();
  for (const article of state.articles) {
    for (const topic of article.topics) counts.set(topic, (counts.get(topic) || 0) + 1);
  }
  const select = byId("topic");
  select.replaceChildren(new Option(`All clinical topics (${countFormat.format(state.articles.length)})`, ""));
  for (const topic of state.topics) {
    const count = counts.get(topic);
    if (count) select.append(new Option(`${topic} (${countFormat.format(count)})`, topic));
  }
  if (!counts.has(state.topic)) state.topic = "";
  select.value = state.topic;
}

function articleKey(article) {
  return (article.metadata?.doi || article.doi || article.id).toLowerCase();
}

function bookmarkLabel(button, article) {
  const saved = Boolean(folder.get(articleKey(article))?.bookmarked);
  button.textContent = saved ? "Bookmarked" : "Bookmark";
  button.setAttribute("aria-pressed", String(saved));
}

function action(label, callback) {
  const button = element("button", label);
  button.type = "button";
  button.addEventListener("click", callback);
  return button;
}

function remember(entry, id) {
  if (entry) folder.set(id, entry);
  else folder.delete(id);
  renderFolder();
}

function bookmarkButton(article, status) {
  const id = articleKey(article);
  const button = action("", async () => {
    if (pendingBookmarks.has(id)) return;
    pendingBookmarks.add(id);
    try {
      const entry = await updateEntry(id, (saved) => {
        const bookmarked = !saved?.bookmarked;
        if (!bookmarked && !saved?.pdf) return null;
        return { ...saved, id, article, bookmarked, savedAt: Date.now() };
      });
      remember(entry, id);
      for (const control of document.querySelectorAll("[data-bookmark]")) {
        if (control.dataset.bookmark === id) bookmarkLabel(control, article);
      }
      status.textContent = entry?.bookmarked ? "Bookmark added to My folder." : "Bookmark removed.";
    } catch {
      status.textContent = "Couldn’t save the bookmark in this browser.";
    } finally {
      pendingBookmarks.delete(id);
    }
  });
  button.dataset.bookmark = id;
  bookmarkLabel(button, article);
  return button;
}

async function savePdf(file, article) {
  const pdf = await verifyPdf(file);
  const id = article ? articleKey(article) : crypto.randomUUID();
  const entry = await updateEntry(id, (saved) => ({
    ...saved, id, article, pdf, filename: file.name || "Article.pdf", savedAt: Date.now(),
  }));
  remember(entry, id);
  return entry;
}

function fileInput(label, handler) {
  const wrapper = element("label", label, "import-label");
  const input = element("input");
  input.type = "file";
  input.accept = ".pdf,application/pdf";
  input.addEventListener("change", async () => {
    const [file] = input.files;
    if (file) await handler(file);
    input.value = "";
  });
  wrapper.append(input);
  return wrapper;
}

function downloadPdf(entry) {
  const url = URL.createObjectURL(entry.pdf);
  const link = element("a");
  link.href = url;
  link.download = entry.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function renderFolder() {
  const container = byId("folder-results");
  container.replaceChildren();
  if (!folder.size) {
    container.append(element("p", "Bookmark an article or add a PDF to start your folder.", "empty"));
    return;
  }
  for (const entry of [...folder.values()].sort((a, b) => b.savedAt - a.savedAt)) {
    const card = element("article", "", "article folder-entry");
    const title = entry.article?.metadata?.title || entry.article?.title || entry.filename;
    card.append(element("h3", title));
    const labels = [entry.bookmarked && "Bookmark", entry.pdf && "PDF saved on this device"].filter(Boolean);
    card.append(element("p", labels.join(" · "), "article-source"));
    const controls = element("div", "", "article-actions");
    if (entry.article) controls.append(action("Article details", () => showArticle(entry.article)));
    if (entry.pdf) controls.append(action("Download PDF", () => downloadPdf(entry)));
    const remove = (label, update, message) => action(label, async () => {
      try {
        remember(await updateEntry(entry.id, update), entry.id);
        for (const control of document.querySelectorAll("[data-bookmark]")) {
          if (entry.article && control.dataset.bookmark === entry.id) bookmarkLabel(control, entry.article);
        }
        byId("folder-status").textContent = message;
      } catch {
        byId("folder-status").textContent = "Couldn’t remove the saved item.";
      }
    });
    if (entry.bookmarked) controls.append(remove("Remove bookmark", (saved) =>
      saved?.pdf ? { ...saved, bookmarked: false } : null, "Bookmark removed."));
    if (entry.pdf) controls.append(remove("Remove PDF", (saved) => {
      if (!saved?.bookmarked) return null;
      const { pdf, filename, ...bookmark } = saved;
      return bookmark;
    }, "PDF removed from this folder."));
    card.append(controls);
    container.append(card);
  }
}

async function refreshFolder() {
  try {
    const entries = await readFolder();
    folder.clear();
    for (const entry of entries) folder.set(entry.id, entry);
    for (const button of document.querySelectorAll("[data-bookmark]")) {
      const article = state.articles.find((row) => articleKey(row) === button.dataset.bookmark);
      if (article) bookmarkLabel(button, article);
    }
    renderFolder();
  } catch {
    byId("folder-status").textContent = "Local storage is unavailable in this browser.";
  }
}

async function showSummary(article, section) {
  summaryRequest?.abort();
  const request = new AbortController();
  summaryRequest = request;
  const status = element("p", "Loading abstract highlights…", "summary-source");
  status.setAttribute("role", "status");
  section.append(status);
  try {
    const sections = await loadSummary(article, AbortSignal.any([request.signal, AbortSignal.timeout(12000)]));
    if (request.signal.aborted) return;
    if (!sections.length) {
      status.textContent = "Summary unavailable here. Check the original article or abstract above.";
      return;
    }
    status.textContent = "Selected excerpts from the authors’ abstract, retrieved from Europe PMC.";
    for (const [label, text] of sections) {
      if (label) section.append(element("h4", label));
      section.append(element("p", text));
    }
  } catch {
    if (!request.signal.aborted) status.textContent = "Couldn’t load the summary. Reopen the article to retry or use the abstract link above.";
  }
}

function showArticle(article) {
  const detail = byId("article-detail");
  const info = article.metadata || {};
  const title = element("h2", info.title || article.title);
  title.id = "dialog-title";
  detail.replaceChildren(title, element("p", article.citation, "citation"));
  const tags = element("div", "", "tags");
  for (const topic of article.topics) tags.append(element("span", topic, "tag"));
  detail.append(tags);
  const actions = element("div", "", "detail-actions");
  actions.append(externalLink("Original article ↗", article.url));
  if (info.pdf) actions.append(externalLink("Open-access PDF ↗", info.pdf));
  else if (info.fullText) actions.append(externalLink("Open full text ↗", info.fullText));
  if (info.abstractUrl) actions.append(externalLink("Read abstract ↗", info.abstractUrl));
  const status = element("p", "", "article-status");
  status.setAttribute("role", "status");
  actions.append(bookmarkButton(article, status));
  const importPdf = fileInput("Attach PDF", async (file) => {
    try {
      saveLocal.hidden = true;
      await savePdf(file, article);
      status.textContent = "PDF saved in My folder.";
    } catch {
      status.textContent = "Couldn’t save the file. Choose a PDF and check available storage.";
    } finally {
      saveLocal.hidden = !info.pdf || Boolean(folder.get(articleKey(article))?.pdf);
    }
  });
  const saveLocal = action("Save PDF locally", async () => {
    saveLocal.disabled = true;
    status.textContent = "Getting the PDF…";
    try {
      const response = await fetch(info.pdf, { credentials: "omit", signal: AbortSignal.timeout(20000) });
      if (!response.ok) throw new Error("PDF unavailable");
      const filename = (info.title || article.title).replace(/[\\/:*?"<>|]/g, "").slice(0, 120) + ".pdf";
      await savePdf(new File([await response.blob()], filename), article);
      saveLocal.hidden = true;
      status.textContent = "PDF saved in My folder.";
    } catch {
      status.textContent = "Open the PDF, save it to Files or Downloads, then choose Attach PDF here.";
    } finally {
      saveLocal.disabled = false;
    }
  });
  saveLocal.hidden = !info.pdf || Boolean(folder.get(articleKey(article))?.pdf);
  actions.append(saveLocal, importPdf);
  const summary = element("section", "", "abstract");
  summary.append(element("h3", "Summary"));
  detail.append(actions, status, summary);
  dialog.showModal();
  showSummary(article, summary);
  dialog.scrollTop = 0;
}

function makeCard(article) {
  const card = byId("article-template").content.cloneNode(true);
  const info = article.metadata || {};
  const badge = card.querySelector(".badge");
  badge.textContent = article.type;
  badge.dataset.group = {
    "Randomised trial": "trial",
    "Observational study": "observational",
    "Systematic review / meta-analysis": "synthesis",
    "Guideline / consensus": "guideline",
  }[article.type] || "other";
  const time = card.querySelector("time");
  time.dateTime = article.date;
  time.textContent = formattedDate(article.date);
  time.title = "Date added to Journal Watch";
  const title = card.querySelector(".article-title");
  title.textContent = info.title || article.title;
  title.addEventListener("click", () => showArticle(article));
  const journal = info.journal || article.journal;
  const doi = info.doi || article.doi;
  card.querySelector(".article-source").textContent = [journal, doi && "DOI " + doi].filter(Boolean).join(" · ");
  for (const topic of article.topics) {
    const tag = element("button", topic, "tag");
    tag.type = "button";
    tag.addEventListener("click", () => {
      setTopic(topic);
      byId("results-title").scrollIntoView({ block: "start" });
    });
    card.querySelector(".tags").append(tag);
  }
  const actions = card.querySelector(".article-actions");
  if (canLoadSummary(article) && (info.hasAbstract || !info.abstractUrl)) {
    actions.append(action("Summary", () => showArticle(article)));
  } else if (info.abstractUrl) {
    actions.append(externalLink("Abstract ↗", info.abstractUrl));
  }
  const status = element("span", "", "visually-hidden");
  status.setAttribute("role", "status");
  actions.append(bookmarkButton(article, status), status);
  if (info.pdf) actions.append(externalLink("PDF ↗", info.pdf));
  actions.append(externalLink("Article ↗", article.url));
  return card;
}

function render() {
  const terms = normalize(byId("search").value).trim().split(/\s+/).filter(Boolean);
  const type = byId("type").value;
  const pdfOnly = byId("pdf-only").checked;
  const filtered = state.articles.filter((article) =>
    (!state.topic || article.topics.includes(state.topic)) &&
    (!type || article.type === type) &&
    (!pdfOnly || article.metadata?.pdf) &&
    terms.every((term) => article.searchText.includes(term))
  );
  byId("results-title").textContent = topicLabel(state.topic);
  byId("count").textContent = countFormat.format(filtered.length) + (filtered.length === 1 ? " article" : " articles");
  results.replaceChildren(...filtered.slice(0, state.limit).map(makeCard));
  if (!filtered.length) {
    results.append(element("p", "No articles match these filters.", "empty"));
    const reset = element("button", "Clear filters", "more");
    reset.type = "button";
    reset.addEventListener("click", () => {
      byId("search").value = "";
      byId("type").value = "";
      byId("pdf-only").checked = false;
      setTopic("");
      byId("search").focus();
    });
    results.append(reset);
  }
  byId("more").hidden = filtered.length <= state.limit;
}

byId("close-dialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => {
  if (!dialog.open) summaryRequest?.abort();
});
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

function loadIndex() {
  if (!indexRequest) {
    indexRequest = fetchIndex().finally(() => { indexRequest = undefined; });
  }
  return indexRequest;
}

async function fetchIndex() {
  const response = await fetch("evidence.json", { cache: "no-store" });
  if (!response.ok) throw new Error("Evidence index unavailable");
  const data = await response.json();
  if (loadedVersion && Date.parse(data.updated) <= Date.parse(loadedVersion)) return;
  state.articles = data.articles.map((article) => ({
    ...article,
    searchText: normalize([
      article.citation, article.doi, article.metadata?.title, article.metadata?.doi, article.metadata?.journal,
    ].filter(Boolean).join(" ")),
  }));
  state.topics = data.topics;
  byId("window").textContent = formattedDate(data.windowStart) + " – " + formattedDate(data.windowEnd);
  byId("latest-article").textContent = "Latest Journal Watch entries " + formattedDate(data.latest);
  byId("updated").textContent = "Last refreshed " + refreshTimeFormat.format(new Date(data.updated));
  buildTopics();
  const typeFilter = byId("type");
  const selected = typeFilter.value;
  const types = [...new Set(state.articles.map((article) => article.type))].sort();
  typeFilter.replaceChildren(new Option("All publication types", ""), ...types.map((type) => new Option(type, type)));
  typeFilter.value = types.includes(selected) ? selected : "";
  render();
  loadedVersion = data.updated;
}

function initializeControls() {
  byId("topic").addEventListener("change", (event) => setTopic(event.target.value));
  for (const [id, event] of [["search", "input"], ["type", "change"], ["pdf-only", "change"]]) {
    byId(id).addEventListener(event, () => {
      state.limit = 20;
      render();
    });
  }
  byId("more").addEventListener("click", () => {
    const previous = state.limit;
    state.limit += 20;
    render();
    results.children[previous]?.querySelector(".article-title")?.focus();
  });
  const folderToggle = byId("show-folder");
  function showFolder(open) {
    byId("browse").hidden = open;
    byId("personal-folder").hidden = !open;
    folderToggle.setAttribute("aria-pressed", String(open));
    if (open) {
      byId("folder-title").focus();
      refreshFolder();
    } else {
      folderToggle.focus();
    }
  }
  folderToggle.addEventListener("click", () => showFolder(byId("personal-folder").hidden));
  byId("back-to-library").addEventListener("click", () => showFolder(false));
  const importInput = byId("folder-import");
  importInput.addEventListener("change", async () => {
    const files = [...importInput.files];
    importInput.value = "";
    const results = await Promise.allSettled(files.map((file) => savePdf(file)));
    const saved = results.filter((result) => result.status === "fulfilled").length;
    const failed = results.length - saved;
    byId("folder-status").textContent = [
      saved ? `Saved ${saved} ${saved === 1 ? "PDF" : "PDFs"}.` : "",
      failed ? `Couldn’t save ${failed}. Check file type and available storage.` : "",
    ].filter(Boolean).join(" ");
  });
  const refresh = byId("refresh");
  const status = byId("refresh-status");
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    status.textContent = "Loading the latest published library…";
    try {
      await loadIndex();
      status.textContent = "Latest published library loaded.";
    } catch {
      status.textContent = "Couldn’t reload the library. Try again later.";
    } finally {
      refresh.disabled = false;
    }
  });
}

initializeControls();
refreshFolder();
function reloadWhenVisible() {
  if (document.visibilityState !== "visible") return;
  loadIndex().catch(() => {
    byId("refresh-status").textContent = "Couldn’t check for a newer library. Try Reload library.";
  });
}
document.addEventListener("visibilitychange", reloadWhenVisible);
window.addEventListener("focus", reloadWhenVisible);
setInterval(reloadWhenVisible, 5 * 60 * 1000);
loadIndex().catch(() => {
  byId("window").textContent = "Index unavailable";
  const message = element("p", "The evidence index could not load. Reload the page to try again. ", "empty");
  message.append(externalLink("Open Journal Watch ↗", "https://criticalcarereviews.com/latest-evidence/journal-watch"));
  results.replaceChildren(message);
});
