const state = {
  analysis: null,
  analyzedUrl: null,
  jobs: [],
  settings: null,
  analyzing: false,
  queueing: false
};
const jobOpenChoices = new Map();
const $ = (selector) => document.querySelector(selector);

const elements = {
  form: $("#analyze-form"), input: $("#url-input"), analyzeButton: $("#analyze-button"),
  empty: $("#empty-state"), panel: $("#analysis-panel"), attachments: $("#attachments"),
  postAuthor: $("#post-author"), postHandle: $("#post-handle"), postId: $("#post-id"),
  postText: $("#post-text"), articleSummary: $("#article-summary"),
  articleTitle: $("#article-title"), articleDescription: $("#article-description"),
  outputMedia: $("#output-media"), outputMarkdown: $("#output-markdown"),
  outputPdf: $("#output-pdf"), documentMediaOption: $("#document-media-option"),
  includeDocumentMedia: $("#include-document-media"), attachmentHeading: $("#attachment-heading"),
  noAttachments: $("#no-attachments"), summary: $("#selection-summary"),
  download: $("#download-button"), toggleAll: $("#toggle-all"), jobList: $("#job-list"),
  queueEmpty: $("#queue-empty"), jobCount: $("#job-count"), statusDot: $("#status-dot"),
  systemLabel: $("#system-label"), settingsButton: $("#settings-button"),
  settingsDialog: $("#settings-dialog"), settingsForm: $("#settings-form"),
  downloadDir: $("#download-dir"), fragments: $("#fragments"),
  downloadPath: $("#download-path"), outputFolder: $("#output-folder"), toast: $("#toast")
};

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "Size unknown";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1);
  return `${(bytes / 1000 ** index).toFixed(index > 2 ? 2 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return null;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s left`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)}m left`;
  return `${(seconds / 3600).toFixed(1)}h left`;
}

function outputLabel(output) {
  return { media: "Media", markdown: "Markdown", pdf: "PDF" }[output] || output;
}

function normalizedPath(path) {
  return String(path).replaceAll("\\", "/");
}

function joinDisplayPath(root, relative) {
  const base = normalizedPath(root).replace(/\/+$/, "");
  const suffix = normalizedPath(relative).replace(/^\/+/, "");
  if (!base) return `/${suffix}`;
  return suffix ? `${base}/${suffix}` : base;
}

function splitPath(path) {
  const value = String(path);
  const middle = Math.ceil(value.length / 2);
  const beforeMiddle = Math.max(value.lastIndexOf("/", middle), value.lastIndexOf("\\", middle));
  const afterSlash = value.indexOf("/", middle);
  const afterBackslash = value.indexOf("\\", middle);
  const afterMiddle = [afterSlash, afterBackslash].filter((index) => index >= 0).sort((a, b) => a - b)[0];
  let splitAt = beforeMiddle > 0 ? beforeMiddle + 1 : afterMiddle + 1;
  if (!splitAt || splitAt >= value.length) splitAt = middle;
  return [value.slice(0, splitAt), value.slice(splitAt)];
}

function createPathPresentation(path, displayPath = path) {
  const value = String(path);
  const [leading, trailing] = splitPath(String(displayPath));
  const presentation = document.createElement("span");
  presentation.className = "path-presentation";
  presentation.title = value;
  presentation.setAttribute("aria-label", value);
  const leadingSpan = document.createElement("span");
  leadingSpan.className = "path-leading";
  leadingSpan.setAttribute("aria-hidden", "true");
  leadingSpan.textContent = leading;
  const trailingSpan = document.createElement("span");
  trailingSpan.className = "path-trailing";
  trailingSpan.setAttribute("aria-hidden", "true");
  trailingSpan.textContent = trailing;
  presentation.append(leadingSpan, trailingSpan);
  return presentation;
}

function fallbackCopyText(value) {
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  textarea.style.pointerEvents = "none";
  document.body.append(textarea);
  textarea.focus({ preventScroll: true });
  textarea.select();
  try {
    return document.execCommand("copy");
  } finally {
    textarea.remove();
  }
}

async function copyPath(path, context, button) {
  const value = String(path);
  button.disabled = true;
  try {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) copied = fallbackCopyText(value);
    if (!copied) throw new Error("Copy is not available in this browser.");
    showToast(`Copied ${context}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function createPathButton(label, accessibleLabel, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "path-action";
  button.textContent = label;
  button.setAttribute("aria-label", accessibleLabel);
  button.addEventListener("click", () => handler(button));
  return button;
}

function createPathControl(path, context, reveal = null, displayPath = path) {
  const control = document.createElement("span");
  control.className = "path-control";
  control.append(createPathPresentation(path, displayPath));
  const actions = document.createElement("span");
  actions.className = "path-actions";
  actions.append(createPathButton("Copy", `Copy ${context}`, (button) => copyPath(path, context, button)));
  if (reveal) {
    actions.append(createPathButton("Reveal", `Reveal ${context}`, (button) => reveal(button)));
  }
  control.append(actions);
  return control;
}

function renderPathControl(container, path, context) {
  container.replaceChildren(createPathControl(path, context));
}

function updateOutputFolder() {
  if (!state.analysis) return;
  const root = state.settings?.download_dir;
  const relative = state.analysis.output_relative_dir;
  let folder = "Post folder assigned when the export is queued";
  if (root && relative) folder = joinDisplayPath(root, relative);
  else if (relative) folder = relative;
  renderPathControl(elements.outputFolder, folder, "post folder path");
}

function completedFilePath(file) {
  return String(typeof file === "string" ? file : (file.path || file.filename || ""));
}

function completedDisplayPath(job, file) {
  const path = normalizedPath(completedFilePath(file));
  const outputDirectory = normalizedPath(job.output_dir || job.destination || "").replace(/\/+$/, "");
  if (outputDirectory && path.startsWith(`${outputDirectory}/`)) {
    return path.slice(outputDirectory.length + 1);
  }
  return path.split("/").pop() || path;
}

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { elements.toast.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    const ready = health.ffmpeg && health.ffprobe && health.gallery_dl && health.yt_dlp && health.queue_running;
    elements.statusDot.className = `status-dot ${ready ? "ready" : "error"}`;
    elements.systemLabel.textContent = ready ? "System ready" : "Dependency missing";
    if (!ready) showToast("A required downloader or media verifier is missing. Check the terminal output.", true);
  } catch (error) {
    elements.statusDot.className = "status-dot error";
    elements.systemLabel.textContent = "Server unavailable";
  }
}

function clearAnalysis() {
  state.analysis = null;
  state.analyzedUrl = null;
  elements.panel.hidden = true;
  elements.empty.hidden = false;
  elements.attachments.replaceChildren();
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = elements.input.value.trim();
  if (!url || state.analyzing) return;
  state.analyzing = true;
  clearAnalysis();
  elements.analyzeButton.disabled = true;
  elements.analyzeButton.querySelector("span").textContent = "Resolving post…";
  try {
    const analysis = await api("/api/analyze", { method: "POST", body: JSON.stringify({ url }) });
    state.analysis = analysis;
    state.analyzedUrl = url;
    renderAnalysis();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.analyzing = false;
    elements.analyzeButton.disabled = false;
    elements.analyzeButton.querySelector("span").textContent = "Analyze link";
  }
});

function attachmentName(attachment) {
  if (attachment.role === "article_cover") return "Article cover";
  if (attachment.role === "article_image") return `Article image ${String(attachment.index).padStart(2, "0")}`;
  if (attachment.role === "article_video") return `Article video ${String(attachment.index).padStart(2, "0")}`;
  return `Attachment ${String(attachment.index).padStart(2, "0")}`;
}

function renderAnalysis() {
  const analysis = state.analysis;
  const attachments = analysis.attachments || [];
  const article = analysis.article;
  const isArticle = analysis.content_kind === "article";
  elements.empty.hidden = true;
  elements.panel.hidden = false;
  elements.postAuthor.textContent = analysis.post.author_name;
  elements.postHandle.textContent = `@${analysis.post.author_handle}`;
  elements.postId.textContent = `${isArticle ? "ARTICLE" : "POST"} / ${article?.id || analysis.post.post_id}`;
  elements.postText.textContent = analysis.post.text || (isArticle ? "" : "Media-only post");
  elements.articleSummary.hidden = !isArticle;
  elements.articleTitle.textContent = article?.title || "Untitled article";
  elements.articleDescription.textContent = article?.description || article?.excerpt || "Export the complete article as a portable document.";
  updateOutputFolder();

  elements.outputMedia.checked = attachments.length > 0;
  elements.outputMedia.disabled = attachments.length === 0;
  elements.outputMarkdown.checked = isArticle;
  elements.outputPdf.checked = isArticle;
  elements.includeDocumentMedia.checked = true;
  elements.attachments.replaceChildren();

  for (const attachment of attachments) {
    const row = document.createElement("article");
    row.className = "attachment";
    row.dataset.id = attachment.id;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "attachment-check";
    checkbox.checked = true;
    const name = attachmentName(attachment);
    checkbox.setAttribute("aria-label", `Select ${name.toLowerCase()}`);
    checkbox.addEventListener("change", updateSelectionSummary);

    const details = document.createElement("div");
    const title = document.createElement("div");
    title.className = "attachment-title";
    const strong = document.createElement("strong");
    strong.textContent = name;
    const tag = document.createElement("span");
    tag.className = "type-tag";
    tag.textContent = attachment.media_type === "gif" ? "Animated GIF · MP4" : (attachment.role || attachment.media_type).replaceAll("_", " ");
    title.append(strong, tag);
    const meta = document.createElement("div");
    meta.className = "attachment-meta";
    const dimensions = attachment.width && attachment.height ? `${attachment.width} × ${attachment.height}` : null;
    const duration = formatDuration(attachment.duration);
    const extension = attachment.extension?.toUpperCase();
    for (const value of [dimensions, duration, formatBytes(attachment.size_bytes), extension].filter(Boolean)) {
      const span = document.createElement("span");
      span.textContent = value;
      meta.append(span);
    }
    details.append(title, meta);
    if (attachment.alt_text) {
      const altText = document.createElement("p");
      altText.className = "attachment-alt";
      altText.textContent = attachment.alt_text;
      details.append(altText);
    }
    row.append(checkbox, details);

    if (attachment.qualities?.length) {
      const select = document.createElement("select");
      select.setAttribute("aria-label", `Quality for ${name.toLowerCase()}`);
      for (const quality of attachment.qualities) {
        const option = document.createElement("option");
        option.value = quality.id;
        const size = quality.size_bytes ? ` · ${quality.size_is_estimate ? "~" : ""}${formatBytes(quality.size_bytes)}` : "";
        option.textContent = `${quality.label}${size}`;
        select.append(option);
      }
      select.addEventListener("change", updateSelectionSummary);
      row.append(select);
    }
    elements.attachments.append(row);
  }

  elements.attachmentHeading.hidden = attachments.length === 0;
  elements.noAttachments.hidden = attachments.length > 0;
  updateSelectionSummary();
  elements.panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function currentOutputs() {
  return [elements.outputMedia, elements.outputMarkdown, elements.outputPdf]
    .filter((input) => input.checked && !input.disabled)
    .map((input) => input.value);
}

function includesDocumentMedia() {
  return !elements.documentMediaOption.hidden && elements.includeDocumentMedia.checked;
}

function currentSelections() {
  if (!state.analysis || (!elements.outputMedia.checked && !includesDocumentMedia())) return [];
  return [...elements.attachments.querySelectorAll(".attachment")].flatMap((row) => {
    const checkbox = row.querySelector(".attachment-check");
    if (!checkbox.checked) return [];
    return [{ attachment_id: row.dataset.id, quality_id: row.querySelector("select")?.value || null }];
  });
}

function updateSelectionSummary() {
  if (!state.analysis) return;
  const outputs = currentOutputs();
  const hasDocumentOutput = outputs.some((output) => output === "markdown" || output === "pdf");
  const canIncludeDocumentMedia = hasDocumentOutput && state.analysis.attachments.length > 0;
  elements.documentMediaOption.hidden = !canIncludeDocumentMedia;
  const includeDocumentMedia = canIncludeDocumentMedia && elements.includeDocumentMedia.checked;
  const selections = currentSelections();
  const attachmentControlsEnabled = elements.outputMedia.checked || includeDocumentMedia;
  const mediaControls = elements.attachments.querySelectorAll("input, select");
  mediaControls.forEach((control) => { control.disabled = !attachmentControlsEnabled; });
  elements.toggleAll.disabled = !attachmentControlsEnabled;

  let bytes = 0;
  let hasSize = false;
  for (const selection of selections) {
    const attachment = state.analysis.attachments.find((item) => item.id === selection.attachment_id);
    const quality = attachment.qualities?.find((item) => item.id === selection.quality_id);
    const size = quality?.size_bytes || attachment.size_bytes;
    if (size) { bytes += size; hasSize = true; }
  }

  const labels = outputs.map(outputLabel);
  if (outputs.includes("media")) {
    const index = labels.indexOf("Media");
    labels[index] = `Media (${selections.length}${hasSize ? `, ${formatBytes(bytes)}` : ""})`;
  }
  let summary = labels.length ? labels.join(" + ") : "Choose at least one output";
  if (includeDocumentMedia) {
    summary += ` · ${selections.length} included attachment${selections.length === 1 ? "" : "s"}`;
  }
  elements.summary.textContent = summary;
  const invalidMedia = outputs.includes("media") && selections.length === 0;
  elements.download.disabled = state.queueing || outputs.length === 0 || invalidMedia;
  elements.download.textContent = state.queueing ? "Adding export…" : "Add export to queue";
  const all = selections.length === state.analysis.attachments.length;
  elements.toggleAll.textContent = all ? "Clear all" : "Select all";
}

[elements.outputMedia, elements.outputMarkdown, elements.outputPdf].forEach((input) => {
  input.addEventListener("change", updateSelectionSummary);
});
elements.includeDocumentMedia.addEventListener("change", updateSelectionSummary);

elements.toggleAll.addEventListener("click", () => {
  const checkboxes = [...elements.attachments.querySelectorAll(".attachment-check")];
  const shouldSelect = checkboxes.some((item) => !item.checked);
  checkboxes.forEach((item) => { item.checked = shouldSelect; });
  updateSelectionSummary();
});

elements.download.addEventListener("click", async () => {
  if (!state.analysis || state.queueing) return;
  const analysis = state.analysis;
  const outputs = currentOutputs();
  const selections = currentSelections();
  if (!outputs.length || (outputs.includes("media") && !selections.length)) return;
  state.queueing = true;
  updateSelectionSummary();
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        analysis_id: analysis.id,
        outputs,
        selections,
        include_document_media: elements.includeDocumentMedia.checked && !elements.documentMediaOption.hidden
      })
    });
    showToast("Export added to the queue.");
    elements.input.value = "";
    clearAnalysis();
    await refreshJobs();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.queueing = false;
    updateSelectionSummary();
  }
});

function jobOutputSummary(job) {
  const outputs = job.outputs || (job.selections?.length ? ["media"] : []);
  return outputs.map(outputLabel).join(" + ") || "Export";
}

function jobContentLabel(job) {
  if (job.content_kind === "article") {
    return `ARTICLE ${job.article?.title || job.article?.id || job.post.post_id}`;
  }
  return `POST ${job.post.post_id}`;
}

function jobAccessibleLabel(job) {
  const kind = job.content_kind === "article" ? "article" : "post";
  const id = job.article?.id || job.post.post_id;
  return `${kind} ${id} by @${job.post.author_handle}`;
}

function renderJobs() {
  const activeJobs = state.jobs.filter((job) => ["queued", "running", "paused"].includes(job.status));
  elements.jobCount.textContent = `${activeJobs.length} active`;
  elements.queueEmpty.hidden = state.jobs.length > 0;

  const currentJobIds = new Set(state.jobs.map((job) => String(job.id)));
  for (const jobId of jobOpenChoices.keys()) {
    if (!currentJobIds.has(jobId)) jobOpenChoices.delete(jobId);
  }

  elements.jobList.replaceChildren();
  state.jobs.forEach((job, index) => {
    const jobId = String(job.id);
    const details = document.createElement("details");
    details.className = "job";
    details.open = jobOpenChoices.has(jobId) ? jobOpenChoices.get(jobId) : job.status !== "completed";

    const headingId = `job-heading-${index}`;
    const summary = document.createElement("summary");
    summary.className = "job-summary";
    summary.addEventListener("click", () => {
      setTimeout(() => {
        if (details.isConnected) jobOpenChoices.set(jobId, details.open);
      });
    });

    const top = document.createElement("span");
    top.className = "job-top";
    const author = document.createElement("span");
    author.id = headingId;
    author.className = "job-author";
    author.setAttribute("role", "heading");
    author.setAttribute("aria-level", "3");
    author.textContent = `@${job.post.author_handle}`;
    const status = document.createElement("span");
    status.className = `job-state ${job.status}`;
    status.setAttribute("role", "status");
    status.setAttribute("aria-label", `Export status: ${job.status}`);
    status.textContent = job.status;
    top.append(author, status);

    const phase = document.createElement("span");
    phase.className = "job-phase";
    phase.textContent = job.phase || "Waiting";
    const target = document.createElement("span");
    target.className = "job-target";
    target.textContent = `${jobOutputSummary(job)} · ${jobContentLabel(job)}`;
    summary.append(top, phase, target);

    const body = document.createElement("div");
    body.className = "job-body";
    body.setAttribute("aria-labelledby", headingId);
    const outputDirectory = job.output_dir || job.destination;
    if (outputDirectory) {
      const directory = document.createElement("div");
      directory.className = "job-directory";
      const directoryLabel = document.createElement("span");
      directoryLabel.textContent = "Output folder";
      const context = `output folder for ${jobAccessibleLabel(job)}`;
      directory.append(
        directoryLabel,
        createPathControl(outputDirectory, context, job.id ? (button) => revealJobTarget(job, { target: "output_folder" }, context, button) : null)
      );
      if (job.layout_version === 1) {
        const legacy = document.createElement("span");
        legacy.className = "legacy-layout";
        legacy.textContent = "Legacy flat export";
        directory.append(legacy);
      }
      body.append(directory);
    }

    const track = document.createElement("div");
    track.className = "progress-track";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", `Export progress for ${jobAccessibleLabel(job)}`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.round(job.progress || 0)));
    const fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
    track.append(fill);
    const stats = document.createElement("div");
    stats.className = "job-stats";
    const progress = document.createElement("span");
    progress.textContent = `${(job.progress || 0).toFixed(1)}% · ${formatBytes(job.downloaded_bytes)}`;
    const speed = document.createElement("span");
    speed.textContent = job.speed ? `${formatBytes(job.speed)}/s · ${formatEta(job.eta)}` : formatEta(job.eta);
    stats.append(progress, speed);
    body.append(track, stats);

    if (job.completed_files?.length) {
      const completed = document.createElement("div");
      completed.className = "completed-files";
      const label = document.createElement("span");
      label.textContent = "Completed paths";
      const list = document.createElement("ul");
      job.completed_files.forEach((file, fileIndex) => {
        const path = completedFilePath(file);
        const displayPath = completedDisplayPath(job, file);
        const entry = document.createElement("li");
        const context = `completed file ${fileIndex + 1} for ${jobAccessibleLabel(job)}`;
        entry.append(createPathControl(
          path,
          context,
          job.id ? (button) => revealJobTarget(job, { target: "completed_file", completed_file_index: fileIndex }, context, button) : null,
          displayPath
        ));
        list.append(entry);
      });
      completed.append(label, list);
      body.append(completed);
    }
    if (job.error) {
      const error = document.createElement("p");
      error.className = "job-error";
      error.textContent = job.error;
      body.append(error);
    }
    const actions = jobActions(job);
    if (actions.length) {
      const actionBar = document.createElement("div");
      actionBar.className = "job-actions";
      actionBar.setAttribute("aria-label", `Actions for ${jobAccessibleLabel(job)}`);
      for (const [label, action] of actions) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.setAttribute("aria-label", `${label} export for ${jobAccessibleLabel(job)}`);
        button.addEventListener("click", () => controlJob(job.id, action, button));
        actionBar.append(button);
      }
      body.append(actionBar);
    }
    details.append(summary, body);
    elements.jobList.append(details);
  });
}

function jobActions(job) {
  if (job.status === "running" || job.status === "queued") return [["Pause", "pause"], ["Cancel", "cancel"]];
  if (job.status === "paused") return [["Resume", "resume"], ["Cancel", "cancel"]];
  if (job.status === "failed" || job.status === "cancelled") return [["Retry", "retry"]];
  return [];
}

async function revealJobTarget(job, target, context, button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${job.id}/reveal`, {
      method: "POST",
      body: JSON.stringify(target)
    });
    showToast(`Revealed ${context}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function controlJob(id, action, button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${id}/${action}`, { method: "POST" });
    await refreshJobs();
  } catch (error) {
    button.disabled = false;
    showToast(error.message, true);
  }
}

async function refreshJobs() {
  try {
    state.jobs = await api("/api/jobs");
    renderJobs();
  } catch (error) {
    showToast(error.message, true);
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.addEventListener("jobs", (event) => {
    state.jobs = JSON.parse(event.data);
    renderJobs();
  });
  source.onerror = () => {
    source.close();
    setTimeout(connectEvents, 1800);
  };
}

elements.settingsButton.addEventListener("click", async () => {
  try {
    state.settings = await api("/api/settings");
    renderPathControl(elements.downloadPath, state.settings.download_dir, "configured export root path");
    updateOutputFolder();
    elements.downloadDir.value = state.settings.download_dir;
    elements.fragments.value = String(state.settings.concurrent_fragments);
    elements.settingsDialog.showModal();
    requestAnimationFrame(() => elements.downloadDir.focus());
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.settingsForm.addEventListener("submit", async (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  try {
    state.settings = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({
        download_dir: elements.downloadDir.value.trim(),
        concurrent_fragments: Number(elements.fragments.value)
      })
    });
    elements.settingsDialog.close();
    renderPathControl(elements.downloadPath, state.settings.download_dir, "configured export root path");
    updateOutputFolder();
    showToast("Settings saved.");
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.input.addEventListener("input", () => {
  if (state.analysis && elements.input.value.trim() !== state.analyzedUrl) clearAnalysis();
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    elements.input.value = "";
    clearAnalysis();
  }
});

checkHealth();
api("/api/settings").then((settings) => {
  state.settings = settings;
  renderPathControl(elements.downloadPath, settings.download_dir, "configured export root path");
  updateOutputFolder();
}).catch((error) => showToast(error.message, true));
refreshJobs();
connectEvents();
