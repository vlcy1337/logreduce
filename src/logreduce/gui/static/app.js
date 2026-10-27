"use strict";

const token = document.querySelector('meta[name="logreduce-token"]').content;
const byId = (id) => document.getElementById(id);
const terminalStates = new Set(["succeeded", "failed", "canceled"]);
const activeStates = new Set(["starting", "running", "canceling"]);

const state = {
  loadedPath: "",
  loadedExisting: false,
  dirty: false,
  activeTab: "form",
  document: null,
  rawContent: "",
  valid: false,
  formEdited: false,
  validationTimer: null,
  validationSequence: 0,
  jobId: null,
  jobActive: false,
  pollTimer: null,
  browserMode: null,
  browserCallback: null,
  browserPath: "",
  preflight: null,
};

const elements = {
  logDir: byId("log-dir"),
  configPath: byId("config-path"),
  workers: byId("workers"),
  force: byId("force-output"),
  outputPath: byId("output-path"),
  encoding: byId("encoding"),
  rules: byId("rules"),
  ruleCount: byId("rule-count"),
  rawConfig: byId("raw-config"),
  formView: byId("form-view"),
  rawView: byId("raw-view"),
  formTab: byId("form-tab"),
  rawTab: byId("raw-tab"),
  dirtyIndicator: byId("dirty-indicator"),
  validation: byId("validation-status"),
  saveConfig: byId("save-config"),
  dryRun: byId("dry-run"),
  runJob: byId("run-job"),
  jobCard: byId("job-card"),
  jobEmpty: byId("job-empty"),
  jobKind: byId("job-kind"),
  jobState: byId("job-state"),
  jobPhase: byId("job-phase"),
  jobTasks: byId("job-tasks"),
  jobInput: byId("job-input"),
  jobOutput: byId("job-output"),
  progressBar: byId("progress-bar"),
  eventLog: byId("event-log"),
  cancelJob: byId("cancel-job"),
  confirmDialog: byId("confirm-dialog"),
  confirmAcknowledge: byId("confirm-acknowledge"),
  confirmRun: byId("confirm-run"),
  saveAsDialog: byId("save-as-dialog"),
  saveAsPath: byId("save-as-path"),
  pathDialog: byId("path-dialog"),
  pathTitle: byId("path-dialog-title"),
  pathCurrent: byId("path-current"),
  pathList: byId("path-list"),
  pathUp: byId("path-up"),
  pathHint: byId("path-hint"),
  chooseCurrent: byId("choose-current-directory"),
  connection: byId("connection-status"),
  toast: byId("toast"),
};

async function api(path, options = {}) {
  const headers = { "X-LogReduce-Token": token };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "same-origin",
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message = payload?.error?.message || `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    error.kind = payload?.error?.kind || "request";
    throw error;
  }
  return payload;
}

async function initialize() {
  bindEvents();
  try {
    const bootstrap = await api("/api/bootstrap");
    elements.workers.placeholder = `自动（${bootstrap.workers}）`;
    if (bootstrap.initial_log_dir) {
      elements.logDir.value = bootstrap.initial_log_dir;
    }
    if (bootstrap.initial_config) {
      elements.configPath.value = bootstrap.initial_config;
      await loadConfig(bootstrap.initial_config, { confirmDiscard: false });
    } else {
      createBlankConfig(false);
    }
    setConnection("online", "本地服务已连接");
  } catch (error) {
    setConnection("offline", "无法连接本地服务");
    showError(error);
  }
  updateButtons();
}

function bindEvents() {
  elements.formTab.addEventListener("click", () => switchTab("form"));
  elements.rawTab.addEventListener("click", () => switchTab("raw"));

  elements.outputPath.addEventListener("input", () => {
    if (!state.document) return;
    state.document.output = elements.outputPath.value;
    markFormEdited();
  });
  elements.encoding.addEventListener("input", () => {
    if (!state.document) return;
    state.document.encoding = elements.encoding.value;
    markFormEdited();
  });
  elements.rawConfig.addEventListener("input", () => {
    state.rawContent = elements.rawConfig.value;
    invalidatePreflight();
    setDirty(true);
    scheduleValidation();
  });
  elements.logDir.addEventListener("input", () => {
    invalidatePreflight();
    updateButtons();
  });
  elements.configPath.addEventListener("input", () => {
    invalidatePreflight();
    updateButtons();
  });
  elements.workers.addEventListener("input", invalidatePreflight);
  elements.force.addEventListener("change", invalidatePreflight);

  byId("add-rule").addEventListener("click", addRule);
  byId("new-config").addEventListener("click", () => createBlankConfig(true));
  byId("save-config").addEventListener("click", () => saveCurrentConfig());
  byId("save-as-config").addEventListener("click", openSaveAs);
  byId("browse-log-dir").addEventListener("click", () =>
    openPathBrowser("directory", elements.logDir.value || "", (path) => {
      elements.logDir.value = path;
      invalidatePreflight();
      updateButtons();
    }),
  );
  byId("browse-config").addEventListener("click", () =>
    openPathBrowser("config", directoryOf(elements.configPath.value), (path) => {
      elements.configPath.value = path;
      loadConfig(path);
    }),
  );
  byId("browse-save-directory").addEventListener("click", openSaveDirectory);
  byId("confirm-save-as").addEventListener("click", saveAs);
  elements.dryRun.addEventListener("click", () => startPreparedJob(true));
  elements.runJob.addEventListener("click", openRunConfirmation);
  elements.confirmAcknowledge.addEventListener("change", () => {
    elements.confirmRun.disabled = !elements.confirmAcknowledge.checked;
  });
  elements.confirmRun.addEventListener("click", () => {
    elements.confirmDialog.close();
    startPreparedJob(false);
  });
  elements.cancelJob.addEventListener("click", cancelJob);

  byId("path-go").addEventListener("click", () =>
    navigatePath(elements.pathCurrent.value),
  );
  elements.pathCurrent.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      navigatePath(elements.pathCurrent.value);
    }
  });
  elements.pathUp.addEventListener("click", () => {
    if (elements.pathUp.dataset.parent) {
      navigatePath(elements.pathUp.dataset.parent);
    }
  });
  elements.chooseCurrent.addEventListener("click", () => {
    const callback = state.browserCallback;
    const path = state.browserPath;
    closePathBrowser();
    callback?.(path);
  });

  window.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

function setConnection(status, message) {
  elements.connection.className = `connection ${status}`;
  elements.connection.querySelector("span:last-child").textContent = message;
}

async function loadConfig(path, options = {}) {
  const confirmDiscard = options.confirmDiscard !== false;
  if (
    confirmDiscard &&
    state.dirty &&
    !window.confirm("当前配置有未保存修改，确定要放弃并打开其他配置吗？")
  ) {
    return;
  }
  try {
    invalidatePreflight();
    const result = await api("/api/config/load", {
      method: "POST",
      body: { path },
    });
    state.loadedPath = result.path;
    state.loadedExisting = true;
    state.rawContent = result.content;
    elements.configPath.value = result.path;
    elements.rawConfig.value = result.content;
    state.formEdited = false;
    state.dirty = false;
    setDirty(false);
    if (result.valid && result.document) {
      state.document = result.document;
      renderForm();
      state.valid = true;
      setValidation("valid", "配置有效，可以预检");
      if (state.activeTab === "raw") {
        await switchTab("form", { validate: false });
      }
    } else {
      state.document = null;
      state.valid = false;
      renderRules();
      setValidation("invalid", result.error || "配置无效");
      await switchTab("raw", { validate: false });
      showToast(result.error || "配置无效");
    }
    updateButtons();
  } catch (error) {
    state.valid = false;
    setValidation("invalid", error.message);
    showError(error);
  }
}

function createBlankConfig(confirmDiscard) {
  if (
    confirmDiscard &&
    state.dirty &&
    !window.confirm("当前配置有未保存修改，确定要新建配置吗？")
  ) {
    return;
  }
  invalidatePreflight();
  state.loadedPath = "";
  state.loadedExisting = false;
  state.document = {
    version: 1,
    output: "combined.log",
    encoding: "utf-8",
    rules: [],
  };
  state.rawContent =
    'version = 1\noutput = "combined.log"\nencoding = "utf-8"\n';
  state.formEdited = true;
  state.dirty = true;
  state.valid = true;
  elements.configPath.value = "";
  elements.rawConfig.value = state.rawContent;
  renderForm();
  setDirty(true);
  setValidation("valid", "新配置，保存后才能运行");
  switchTab("form", { validate: false });
  updateButtons();
}

function renderForm() {
  if (!state.document) {
    elements.outputPath.value = "";
    elements.encoding.value = "";
    renderRules();
    return;
  }
  elements.outputPath.value = state.document.output || "";
  elements.encoding.value = state.document.encoding || "utf-8";
  renderRules();
}

function renderRules() {
  elements.rules.replaceChildren();
  const rules = state.document?.rules || [];
  elements.ruleCount.textContent = `${rules.length} 条`;
  if (rules.length === 0) {
    const empty = document.createElement("div");
    empty.className = "rules-empty";
    empty.textContent = "暂无规则。没有规则时，日志文件会按路径顺序原样合并。";
    elements.rules.append(empty);
    return;
  }
  rules.forEach((rule, index) => {
    elements.rules.append(createRuleCard(rule, index, rules.length));
  });
}

function createRuleCard(rule, index, count) {
  const card = document.createElement("article");
  card.className = "rule-card";

  const head = document.createElement("div");
  head.className = "rule-head";
  const badge = document.createElement("span");
  badge.className = "rule-index";
  badge.textContent = String(index + 1).padStart(2, "0");
  const name = document.createElement("input");
  name.className = "rule-name";
  name.value = rule.name || `rule ${index + 1}`;
  name.setAttribute("aria-label", `规则 ${index + 1} 名称`);
  name.addEventListener("input", () => {
    rule.name = name.value;
    markFormEdited();
  });
  const actions = document.createElement("div");
  actions.className = "rule-actions";
  actions.append(
    ruleAction("↑", "上移", () => moveRule(index, -1), index === 0),
    ruleAction("↓", "下移", () => moveRule(index, 1), index === count - 1),
    ruleAction(
      "×",
      "删除",
      () => removeRule(index),
      false,
      "remove-rule",
    ),
  );
  head.append(badge, name, actions);
  card.append(head);

  card.append(
    createRuleSection(
      "筛选",
      Boolean(rule.filter),
      (enabled) => {
        rule.filter = enabled
          ? { mode: "include", pattern: "ERROR" }
          : null;
        renderRules();
        markFormEdited();
      },
      () => {
        const fields = document.createElement("div");
        fields.className = "rule-fields";
        const mode = document.createElement("select");
        mode.setAttribute("aria-label", "筛选模式");
        for (const [value, label] of [
          ["include", "保留匹配行"],
          ["exclude", "排除匹配行"],
        ]) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          mode.append(option);
        }
        mode.value = rule.filter.mode;
        mode.addEventListener("change", () => {
          rule.filter.mode = mode.value;
          markFormEdited();
        });
        const pattern = textInput(
          rule.filter.pattern,
          "筛选正则",
          (value) => {
            rule.filter.pattern = value;
            markFormEdited();
          },
        );
        fields.append(mode, pattern);
        return fields;
      },
    ),
  );

  card.append(
    createRuleSection(
      "替换",
      Boolean(rule.replace),
      (enabled) => {
        rule.replace = enabled
          ? { pattern: "old", replacement: "new" }
          : null;
        renderRules();
        markFormEdited();
      },
      () => {
        const fields = document.createElement("div");
        fields.className = "rule-fields";
        const pattern = textInput(
          rule.replace.pattern,
          "替换正则",
          (value) => {
            rule.replace.pattern = value;
            markFormEdited();
          },
        );
        const replacement = textInput(
          rule.replace.replacement,
          "替换文本（支持 \\1 和 \\g<name>）",
          (value) => {
            rule.replace.replacement = value;
            markFormEdited();
          },
        );
        fields.append(pattern, replacement);
        return fields;
      },
    ),
  );
  return card;
}

function createRuleSection(label, enabled, onToggle, buildFields) {
  const section = document.createElement("div");
  section.className = "rule-section";
  const toggle = document.createElement("label");
  toggle.className = "rule-toggle";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = enabled;
  checkbox.addEventListener("change", () => onToggle(checkbox.checked));
  const text = document.createElement("span");
  text.textContent = label;
  toggle.append(checkbox, text);
  section.append(toggle);
  if (enabled) {
    section.append(buildFields());
  } else {
    const disabled = document.createElement("span");
    disabled.className = "inline-note";
    disabled.textContent = `未启用${label}`;
    section.append(disabled);
  }
  return section;
}

function textInput(value, placeholder, onInput) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.placeholder = placeholder;
  input.spellcheck = false;
  input.addEventListener("input", () => onInput(input.value));
  return input;
}

function ruleAction(text, label, handler, disabled, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.disabled = disabled;
  if (className) button.className = className;
  button.addEventListener("click", handler);
  return button;
}

function addRule() {
  if (!state.document) return;
  state.document.rules.push({
    name: `规则 ${state.document.rules.length + 1}`,
    filter: { mode: "include", pattern: "ERROR" },
    replace: null,
  });
  renderRules();
  markFormEdited();
}

function removeRule(index) {
  state.document.rules.splice(index, 1);
  renderRules();
  markFormEdited();
}

function moveRule(index, offset) {
  const rules = state.document.rules;
  const target = index + offset;
  if (target < 0 || target >= rules.length) return;
  [rules[index], rules[target]] = [rules[target], rules[index]];
  renderRules();
  markFormEdited();
}

function markFormEdited() {
  state.formEdited = true;
  invalidatePreflight();
  setDirty(true);
  scheduleValidation();
}

function setDirty(dirty) {
  state.dirty = dirty;
  elements.dirtyIndicator.hidden = !dirty;
  updateButtons();
}

function scheduleValidation() {
  clearTimeout(state.validationTimer);
  state.valid = false;
  setValidation("checking", "正在校验配置");
  state.validationTimer = setTimeout(() => validateCurrent({ quiet: true }), 320);
}

async function validateCurrent(options = {}) {
  clearTimeout(state.validationTimer);
  const sequence = ++state.validationSequence;
  if (!options.quiet) setValidation("checking", "正在校验配置");
  const payload =
    state.activeTab === "raw"
      ? { content: elements.rawConfig.value }
      : { document: state.document };
  if (
    (state.activeTab === "raw" && typeof payload.content !== "string") ||
    (state.activeTab === "form" && !payload.document)
  ) {
    state.valid = false;
    setValidation("invalid", "没有可校验的配置");
    updateButtons();
    return false;
  }
  try {
    const result = await api("/api/config/validate", {
      method: "POST",
      body: payload,
    });
    if (sequence !== state.validationSequence) return false;
    if (!result.valid) {
      state.valid = false;
      setValidation("invalid", result.error || "配置无效");
      updateButtons();
      return false;
    }
    state.valid = true;
    if (state.activeTab === "raw") {
      if (result.document) state.document = result.document;
      state.rawContent = elements.rawConfig.value;
    } else {
      state.rawContent = result.content;
    }
    setValidation("valid", state.dirty ? "配置有效，但尚未保存" : "配置有效，可以预检");
    updateButtons();
    return true;
  } catch (error) {
    if (sequence !== state.validationSequence) return false;
    state.valid = false;
    setValidation("invalid", error.message);
    updateButtons();
    if (!options.quiet) showError(error);
    return false;
  }
}

function setValidation(status, message) {
  elements.validation.dataset.state = status;
  elements.validation.lastElementChild.textContent = message;
  elements.validation.title = message;
}

async function switchTab(tab, options = {}) {
  if (tab === state.activeTab) return;
  if (options.validate !== false) {
    const valid = await validateCurrent();
    if (!valid) {
      showToast("请先修复当前配置中的错误");
      return;
    }
  }
  if (tab === "form") {
    if (!state.document) {
      showToast("配置无效，无法切换到可视化表单");
      return;
    }
    renderForm();
    state.formEdited = false;
  } else if (state.formEdited) {
    elements.rawConfig.value = state.rawContent;
  }
  state.activeTab = tab;
  const formActive = tab === "form";
  elements.formView.hidden = !formActive;
  elements.rawView.hidden = formActive;
  elements.formTab.classList.toggle("active", formActive);
  elements.rawTab.classList.toggle("active", !formActive);
  elements.formTab.setAttribute("aria-selected", String(formActive));
  elements.rawTab.setAttribute("aria-selected", String(!formActive));
}

async function saveCurrentConfig() {
  if (!elements.configPath.value.trim()) {
    openSaveAs();
    return;
  }
  await saveConfigToPath(elements.configPath.value, state.loadedExisting);
}

async function saveAs() {
  const path = elements.saveAsPath.value.trim();
  if (!path) {
    showToast("请输入配置文件路径");
    return;
  }
  if (!(await saveConfigToPath(path, false))) return;
  elements.saveAsDialog.close();
}

async function saveConfigToPath(path, overwrite) {
  if (!(await validateCurrent())) {
    showToast("配置无效，尚未保存");
    return false;
  }
  const useDocument = state.activeTab === "form" && state.formEdited;
  const payload = {
    path,
    overwrite,
    ...(useDocument
      ? { document: state.document }
      : { content: elements.rawConfig.value }),
  };
  try {
    const result = await api("/api/config/save", {
      method: "POST",
      body: payload,
    });
    applySavedConfig(result);
    showToast("配置已保存");
    return true;
  } catch (error) {
    if (error.status === 409) {
      if (window.confirm(`文件已存在：\n${path}\n\n确定覆盖吗？`)) {
        return saveConfigToPath(path, true);
      }
      return false;
    }
    showError(error);
    return false;
  }
}

function applySavedConfig(result) {
  invalidatePreflight();
  state.loadedPath = result.path;
  state.loadedExisting = true;
  state.rawContent = result.content;
  state.document = result.document;
  state.formEdited = false;
  state.valid = true;
  elements.configPath.value = result.path;
  elements.rawConfig.value = result.content;
  renderForm();
  setDirty(false);
  setValidation("valid", "配置已保存，可以预检");
  updateButtons();
}

function openSaveAs() {
  elements.saveAsPath.value ||=
    elements.configPath.value.trim() || defaultConfigPath();
  elements.saveAsDialog.showModal();
}

function openSaveDirectory() {
  const current = elements.saveAsPath.value.trim();
  const filename = fileNameOf(current) || "reduce.toml";
  elements.saveAsDialog.close();
  openPathBrowser("directory", directoryOf(current), (directory) => {
    elements.saveAsPath.value = joinPath(directory, filename);
    elements.saveAsDialog.showModal();
  });
}

async function openPathBrowser(mode, startPath, callback) {
  state.browserMode = mode;
  state.browserCallback = callback;
  elements.pathTitle.textContent =
    mode === "config" ? "打开 TOML 配置" : "选择目录";
  elements.pathHint.textContent =
    mode === "config"
      ? "选择目录继续浏览，或直接选择一个 .toml 文件。"
      : "进入目标目录后，点击“使用此目录”。";
  elements.chooseCurrent.hidden = mode === "config";
  if (elements.pathDialog.open) elements.pathDialog.close();
  elements.pathDialog.showModal();
  await navigatePath(startPath);
}

function closePathBrowser() {
  state.browserMode = null;
  state.browserCallback = null;
  if (elements.pathDialog.open) elements.pathDialog.close();
}

async function navigatePath(path) {
  try {
    const query = new URLSearchParams({ path: path || "" });
    const result = await api(`/api/fs?${query}`);
    state.browserPath = result.path;
    elements.pathCurrent.value = result.path;
    elements.pathUp.dataset.parent = result.parent || "";
    elements.pathUp.disabled = !result.parent;
    elements.pathList.replaceChildren();
    for (const directory of result.directories) {
      elements.pathList.append(
        pathItem(directory, "DIR", () => navigatePath(directory.path)),
      );
    }
    if (state.browserMode === "config") {
      for (const config of result.configs) {
        elements.pathList.append(
          pathItem(config, "TOML", () => {
            const callback = state.browserCallback;
            closePathBrowser();
            callback?.(config.path);
          }),
        );
      }
    }
    if (!elements.pathList.children.length) {
      const empty = document.createElement("div");
      empty.className = "rules-empty";
      empty.textContent = "此目录中没有可选项目。";
      elements.pathList.append(empty);
    }
  } catch (error) {
    showError(error);
  }
}

function pathItem(item, icon, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "path-item";
  const marker = document.createElement("span");
  marker.className = "path-item-icon";
  marker.textContent = icon;
  const name = document.createElement("span");
  name.textContent = item.name;
  const location = document.createElement("small");
  location.textContent = item.path;
  button.append(marker, name, location);
  button.addEventListener("click", handler);
  return button;
}

async function ensureReadyForJob() {
  if (!elements.logDir.value.trim()) {
    showToast("请选择日志目录");
    return false;
  }
  if (!elements.configPath.value.trim() || !state.loadedExisting) {
    showToast("请先保存配置文件");
    return false;
  }
  if (state.dirty) {
    showToast("存在未保存修改，请先保存配置");
    return false;
  }
  if (!(await validateCurrent())) {
    showToast("配置无效，无法运行");
    return false;
  }
  return true;
}

async function startPreparedJob(dryRun) {
  if (state.jobActive) {
    showToast("已有任务正在运行");
    return;
  }
  if (!(await ensureReadyForJob())) return;
  const workersText = elements.workers.value.trim();
  await startJob({
    log_dir: elements.logDir.value.trim(),
    config_path: elements.configPath.value.trim(),
    workers: workersText ? Number(workersText) : null,
    force: elements.force.checked,
    dry_run: dryRun,
    acknowledge_deletion: !dryRun,
  });
}

async function openRunConfirmation() {
  if (state.jobActive) {
    showToast("已有任务正在运行");
    return;
  }
  if (!(await ensureReadyForJob())) return;
  byId("confirm-log-dir").textContent = elements.logDir.value.trim();
  byId("confirm-config").textContent = elements.configPath.value.trim();
  byId("confirm-output").textContent =
    state.document?.output || "未设置";
  byId("confirm-workers").textContent =
    elements.workers.value.trim() || "自动";
  byId("confirm-force").textContent = elements.force.checked ? "是" : "否";
  byId("confirm-preflight").textContent = describePreflight();
  elements.confirmAcknowledge.checked = false;
  elements.confirmRun.disabled = true;
  elements.confirmDialog.showModal();
}

async function startJob(request) {
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: request,
    });
    state.jobId = job.id;
    state.jobActive = true;
    elements.jobCard.hidden = false;
    elements.jobEmpty.hidden = true;
    elements.jobKind.textContent = request.dry_run ? "预检任务" : "正式任务";
    elements.eventLog.replaceChildren();
    renderJob(job);
    updateButtons();
    pollJob();
  } catch (error) {
    showError(error);
  }
}

async function pollJob() {
  clearTimeout(state.pollTimer);
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(state.jobId)}`);
    renderJob(job);
    if (terminalStates.has(job.state)) {
      state.jobActive = false;
      updateButtons();
      if (job.state === "succeeded") {
        if (job.dry_run) state.preflight = job;
        showToast(job.dry_run ? "预检完成" : "处理完成，输出已提交");
      } else {
        showToast(job.error || `任务状态：${job.state}`);
      }
      state.jobId = null;
      return;
    }
    state.pollTimer = setTimeout(pollJob, 600);
  } catch (error) {
    state.jobActive = false;
    updateButtons();
    showError(error);
  }
}

function renderJob(job) {
  const labels = {
    starting: "启动中",
    running: "处理中",
    canceling: "正在取消",
    succeeded: "已完成",
    failed: "失败",
    canceled: "已取消",
  };
  elements.jobState.textContent = labels[job.state] || job.state;
  elements.jobPhase.textContent = job.phase || "-";
  elements.jobTasks.textContent = `${job.completed_tasks || 0} / ${job.total_tasks || 0}`;
  elements.jobInput.textContent = formatBytes(job.input_bytes);
  elements.jobOutput.textContent = formatBytes(job.output_bytes);
  elements.cancelJob.hidden = !activeStates.has(job.state);

  elements.progressBar.classList.toggle(
    "indeterminate",
    activeStates.has(job.state) && !job.total_tasks,
  );
  if (job.total_tasks > 0) {
    const percent = Math.min(
      100,
      Math.max(0, (job.completed_tasks / job.total_tasks) * 100),
    );
    elements.progressBar.value = percent;
  } else if (!activeStates.has(job.state)) {
    elements.progressBar.value = job.state === "succeeded" ? 100 : 0;
  }

  elements.eventLog.replaceChildren();
  for (const event of job.events || []) {
    const line = document.createElement("div");
    line.className = "event-line";
    line.dataset.phase = event.phase || "";
    const time = document.createElement("time");
    time.textContent = formatTime(event.timestamp);
    const message = document.createElement("span");
    message.textContent = event.message || "";
    line.append(time, message);
    elements.eventLog.append(line);
  }
  elements.eventLog.scrollTop = elements.eventLog.scrollHeight;
}

async function cancelJob() {
  if (!state.jobId) return;
  if (!window.confirm("确定取消当前任务吗？中断事务将在下次运行时恢复。")) {
    return;
  }
  try {
    const job = await api(
      `/api/jobs/${encodeURIComponent(state.jobId)}/cancel`,
      { method: "POST", body: {} },
    );
    renderJob(job);
  } catch (error) {
    showError(error);
  }
}

function updateButtons() {
  const running = state.jobActive;
  const ready =
    !running &&
    state.valid &&
    !state.dirty &&
    state.loadedExisting &&
    Boolean(elements.logDir.value.trim()) &&
    Boolean(elements.configPath.value.trim());
  elements.dryRun.disabled = !ready;
  elements.runJob.disabled = !ready;
  elements.saveConfig.disabled = !state.document && state.activeTab === "form";
}

function invalidatePreflight() {
  state.preflight = null;
}

function describePreflight() {
  const job = state.preflight;
  const summary = job?.summary;
  if (!job || job.state !== "succeeded" || !summary) {
    return "尚未预检（可选）";
  }
  return `通过：${summary.file_count} 个文件，${formatBytes(summary.input_bytes)}，${summary.task_count} 个任务`;
}

function showToast(message, error = false) {
  clearTimeout(showToast.timer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.hidden = false;
  showToast.timer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 5200);
}

function showError(error) {
  showToast(error?.message || String(error), true);
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let scaled = bytes;
  let unit = "B";
  for (const candidate of units) {
    if (scaled < 1024) break;
    scaled /= 1024;
    unit = candidate;
  }
  return `${scaled.toFixed(scaled >= 100 ? 0 : 1)} ${unit}`;
}

function formatTime(timestamp) {
  if (!timestamp) return "--:--:--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

function directoryOf(path) {
  if (!path) return "";
  const index = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return index > 0 ? path.slice(0, index) : "";
}

function fileNameOf(path) {
  if (!path) return "";
  return path.slice(Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\")) + 1);
}

function joinPath(directory, filename) {
  return `${directory.replace(/[\\/]+$/, "")}/${filename}`;
}

function defaultConfigPath() {
  const logDir = elements.logDir.value.trim().replace(/[\\/]+$/, "");
  return logDir ? `${logDir}/reduce.toml` : "reduce.toml";
}

initialize();
