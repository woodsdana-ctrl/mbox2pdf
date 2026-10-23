"use strict";
let token = "",
  project = "",
  listing = null,
  selected = new Set(),
  allFiltered = false,
  active = "",
  pending = null,
  busy = false,
  appliedFilters = {};
let lastJob = "";
const $ = (id) => document.getElementById(id);
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}
async function api(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? "GET" : "POST",
    headers:
      data === undefined
        ? {}
        : { "Content-Type": "application/json", "X-Review-Token": token },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || "Request failed");
  return value;
}
function safe(fn) {
  return async (event) => {
    if (event) event.preventDefault();
    try {
      await fn(event);
    } catch (e) {
      notice(e.message, true);
    }
  };
}
function filters() {
  return Object.fromEntries(new FormData($("filters")));
}
function selection() {
  const n = allFiltered ? listing.filtered : selected.size;
  $("selection").textContent = allFiltered
    ? `All ${n} filtered results selected (across all pages)`
    : `${n} messages selected on this page`;
  $("include").disabled = $("exclude").disabled = n === 0;
}
function clearSelection() {
  selected.clear();
  allFiltered = false;
  selection();
  document
    .querySelectorAll("#messages input")
    .forEach((x) => (x.checked = false));
}
function element(tag, text) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  return el;
}
function totals(c) {
  return `${c.total} total · ${c.included} included · ${c.excluded} excluded`;
}
async function refresh(page = 1, nextFilters = appliedFilters) {
  const query = new URLSearchParams({
    project,
    filters: JSON.stringify(nextFilters),
    page,
  });
  listing = await api("/api/messages?" + query);
  appliedFilters = { ...nextFilters };
  $("totals").textContent = totals(listing.counts);
  $("messages").replaceChildren();
  selected.clear();
  allFiltered = false;
  for (const item of listing.items) {
    const row = element("tr");
    const cell = element("td");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.setAttribute(
      "aria-label",
      "Select " + (item.subject || "(no subject)"),
    );
    box.onchange = () => {
      if (allFiltered) {
        allFiltered = false;
        selected = new Set(listing.items.map((x) => x.id));
      }
      box.checked ? selected.add(item.id) : selected.delete(item.id);
      selection();
    };
    cell.append(box);
    row.append(cell);
    row.append(
      element("td", item.date ? item.date.replace("T", " ") : "Unknown date"),
    );
    const people = element("td", item.sender || "(unknown sender)");
    people.append(element("small", "To/Cc/Bcc: " + item.recipients));
    row.append(people);
    const subject = element("td");
    const open = element("button", item.subject || "(no subject)");
    open.type = "button";
    open.onclick = safe(() => read(item.id));
    subject.append(open);
    row.append(
      subject,
      element("td", String(item.attachments.length)),
      element("td", item.include ? "Include" : "Exclude"),
    );
    $("messages").append(row);
  }
  $("page-info").textContent =
    `Page ${listing.page} of ${Math.max(1, Math.ceil(listing.filtered / 25))} · ${listing.filtered} filtered results`;
  $("previous").disabled = listing.page <= 1;
  $("next").disabled = listing.page * 25 >= listing.filtered;
  selection();
  if (active) await read(active);
}
async function read(id) {
  active = id;
  const pid = project;
  const item = await api(
    "/api/message?" + new URLSearchParams({ project: pid, id }),
  );
  if (project !== pid || active !== id) return;
  const pane = $("reading");
  pane.replaceChildren(element("h3", item.subject || "(no subject)"));
  const fields = element("dl");
  for (const [label, value] of [
    ["Status", item.include ? "Include" : "Exclude"],
    ["Exclusion reason", item.reason || "None"],
    ["Date", item.date || item.original_date || "Unknown"],
    ["Original Date header", item.original_date || "Not supplied"],
    ["Message-ID", item.message_id || "Not supplied"],
    ["In-Reply-To", item.in_reply_to || "Not supplied"],
    ["References", item.references.join(", ") || "None supplied"],
    ["From", item.sender],
    ["To", item.to],
    ["Cc", item.cc],
    ["Bcc", item.bcc],
    ["Gmail labels", item.labels.join(", ") || "None supplied"],
  ]) {
    fields.append(element("dt", label), element("dd", value));
  }
  pane.append(fields);
  pane.append(
    element("h4", "Message"),
    element("pre", item.body || "(no available body)"),
  );
  pane.append(element("h4", "Attachments"));
  const attachments = element("ul");
  for (const name of item.attachments) attachments.append(element("li", name));
  pane.append(attachments);
  if (!item.attachments.length) pane.append(element("p", "No attachments"));
  pane.append(element("h4", "All source copies (internal)"));
  const sources = element("ul");
  for (const src of item.sources)
    sources.append(
      element(
        "li",
        `${src.archive} · SHA-256 ${src.sha256} · copy ${src.archive_id} · message position ${src.position} · labels: ${src.labels.join(", ") || "none"}`,
      ),
    );
  pane.append(sources);
  if (item.issues.length)
    pane.append(element("p", "Intake exceptions: " + item.issues.join("; ")));
  const action = element(
    "button",
    item.include ? "Exclude this message" : "Restore this message to Include",
  );
  action.type = "button";
  action.onclick = safe(async () => {
    await api("/api/decide", {
      project,
      revision: listing.revision,
      ids: [id],
      include: !item.include,
      reason: $("reason").value,
    });
    await refresh(listing.page);
    notice("Decision saved for this message and all its copies.");
  });
  pane.append(action);
}
async function projects() {
  const session = await api("/api/session");
  token = session.token;
  $("projects").replaceChildren(element("option", "Choose a review…"));
  $("projects").firstChild.value = "";
  for (const p of session.projects) {
    const option = element("option", p.names.join(", ") + " · " + totals(p));
    option.value = p.id;
    $("projects").append(option);
  }
}
async function openProject(id) {
  await api("/api/open", { project: id });
  project = id;
  $("projects").value = id;
  active = "";
  $("review").hidden = false;
  const data = await api("/api/messages?" + new URLSearchParams({ project }));
  $("generate-form").elements.timezone.value = data.timezone;
  $("filters").reset();
  await refresh(1, filters());
  await jobs();
  notice("Review opened. Decisions are saved automatically.");
  localStorage.setItem("mbox-review-project", id);
}
$("import-form").onsubmit = safe(async () => {
  if (busy) return;
  busy = true;
  $("import-button").disabled = true;
  try {
    const uploads = [];
    for (const file of $("archives").files) {
      notice("Copying " + file.name + " to local internal storage…");
      const response = await fetch(
        "/api/upload?" + new URLSearchParams({ name: file.name }),
        {
          method: "POST",
          headers: {
            "X-Review-Token": token,
            "Content-Type": "application/octet-stream",
          },
          body: file,
        },
      );
      const value = await response.json();
      if (!response.ok) throw Error(value.error);
      uploads.push(value.id);
    }
    notice("Reading and indexing messages locally…");
    const imported = await api("/api/import", {
      uploads,
      timezone: $("import-zone").value,
    });
    await projects();
    await openProject(imported.id);
  } finally {
    busy = false;
    $("import-button").disabled = false;
  }
});
$("open-project").onclick = safe(async () => {
  if (!$("projects").value) throw Error("Choose a saved review");
  await openProject($("projects").value);
});
$("filters").onsubmit = safe(() => refresh(1, filters()));
$("clear-filters").onclick = safe(async () => {
  $("filters").reset();
  await refresh(1, filters());
});
$("select-page").onclick = () => {
  allFiltered = false;
  selected = new Set(listing.items.map((i) => i.id));
  document
    .querySelectorAll("#messages input")
    .forEach((x) => (x.checked = true));
  selection();
};
$("select-filtered").onclick = () => {
  selected.clear();
  allFiltered = true;
  document
    .querySelectorAll("#messages input")
    .forEach((x) => (x.checked = true));
  selection();
};
$("clear-selection").onclick = clearSelection;
async function decide(include) {
  const n = allFiltered ? listing.filtered : selected.size;
  if (!n) throw Error("Select messages first");
  if (
    allFiltered &&
    !window.confirm(
      `${include ? "Restore" : "Exclude"} all ${n} filtered messages across all pages?`,
    )
  )
    return;
  await api("/api/decide", {
    project,
    revision: listing.revision,
    include,
    reason: $("reason").value,
    ...(allFiltered ? { filters: appliedFilters } : { ids: [...selected] }),
  });
  await refresh(listing.page);
  notice(
    "Decisions saved. Use Undo last decision change to reverse this action.",
  );
}
$("exclude").onclick = safe(() => decide(false));
$("include").onclick = safe(() => decide(true));
$("undo").onclick = safe(async () => {
  await api("/api/undo", { project, revision: listing.revision });
  await refresh(listing.page);
  notice("Last decision change undone and saved.");
});
$("previous").onclick = safe(() => refresh(listing.page - 1));
$("next").onclick = safe(() => refresh(listing.page + 1));
$("generate-form").onsubmit = safe(async () => {
  const revision = listing.revision;
  const form = $("generate-form");
  const settings = Object.fromEntries(new FormData(form));
  for (const key of ["start", "volume_pages"])
    settings[key] = Number(settings[key]);
  settings.appendix_over =
    settings.appendix_over === "" ? null : Number(settings.appendix_over);
  for (const key of ["convert_attachments", "dedup_attachments"])
    settings[key] = settings[key] === "true";
  await refresh(listing.page);
  if (listing.revision !== revision)
    throw Error(
      "Review decisions changed. Review the new totals and click again.",
    );
  pending = { settings, revision };
  $("generation-summary").textContent = totals(listing.counts);
  $("generation-options").textContent =
    `${settings.mode} · ${settings.timezone} · ${settings.order} order · Bates ${settings.prefix}${settings.start} · ${settings.volume_pages} pages/volume · ${settings.convert_attachments ? "convert attachments" : "attachment placeholders"}${settings.appendix_over === null ? "" : ` · appendix over ${settings.appendix_over} pages`}`;
  $("generate").disabled = listing.counts.included === 0;
  $("confirm-generation").showModal();
});
$("generate").onclick = safe(async () => {
  $("generate").disabled = true;
  try {
    await api("/api/generate", {
      project,
      revision: pending.revision,
      settings: pending.settings,
    });
    $("confirm-generation").close();
    notice(
      "Generation queued. The offline worker will process the saved included set.",
    );
    await jobs();
  } finally {
    $("generate").disabled = false;
  }
});
$("cancel-generation").onclick = () => $("confirm-generation").close();
async function jobs() {
  if (!project) return;
  const pid = project;
  const list = await api("/api/jobs?" + new URLSearchParams({ project: pid }));
  if (pid !== project) return;
  const newest = list[0];
  const signature = newest ? newest.id + newest.status : "";
  if (signature !== lastJob && newest) {
    if (newest.status === "completed")
      notice("PDFs ready for your existing review and redaction workflow.");
    if (newest.status === "failed")
      notice("Generation failed: " + newest.error, true);
  }
  lastJob = signature;
  $("jobs").replaceChildren();
  for (const job of list) {
    const box = element("div");
    box.className = "job";
    box.append(
      element("h3", `${job.mode} · ${job.status}`),
      element("p", totals(job.counts)),
      element("p", "Saved on this computer: review-data/" + job.output),
    );
    if (job.error) box.append(element("p", job.error));
    if (job.status === "queued")
      box.append(
        element(
          "p",
          "Waiting for the offline worker. Keep Docker Compose running.",
        ),
      );
    if (job.status === "completed" && job.mode === "review") {
      const link = element("a", "Download review volume ZIP");
      link.className = "download";
      link.href = "/api/package?" + new URLSearchParams({ id: job.id });
      box.append(link);
    } else if (job.status === "completed")
      box.append(
        element(
          "p",
          "Survey and triage files are internal. Open the run’s output folder on this computer.",
        ),
      );
    $("jobs").append(box);
  }
}
safe(async () => {
  await projects();
  const id = localStorage.getItem("mbox-review-project");
  if (id) await openProject(id);
})();
setInterval(() => {
  if (project) jobs().catch((e) => notice(e.message, true));
}, 2500);
