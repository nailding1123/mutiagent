const state = {
  health: null,
  runs: [],
  currentId: null,
  detail: null,
  feedbackAction: null,
  mainView: 'chat',
  detailView: 'overview',
  detailAgent: 'claude',
  refreshTimer: null,
  eventSource: null,
  settings: null,
  collapsedProjects: new Set(),
  showArchived: false,
  contextRunId: null,
  renameRunId: null,
  searchQuery: '',
  searchArchivedOpen: true,
  taskFiles: [],
  returnToNewTaskAfterSettings: false,
  draftTaskMode: null,
  workspaceBrowserPath: '',
  mentionStart: -1,
  mentionIndex: 0,
  openChangeSummaries: new Set(),
  openChangeFiles: new Set(),
  pendingChatMessages: new Map(),
  pendingChatSequence: 0,
  interfaceSavePromise: Promise.resolve(),
  modelCatalog: null,
  modelOrders: { claude: [], codex: [] },
  draggedModel: null,
};

const ACTIVE_RUN_STATUSES = new Set(['starting', 'running', 'awaiting_plan', 'stopping']);
const DOCUMENT_EXTENSIONS = new Set(['csv', 'doc', 'docx', 'html', 'json', 'md', 'odt', 'pdf', 'ppt', 'pptx', 'rtf', 'txt', 'xls', 'xlsx', 'xml', 'yaml', 'yml']);
const MAX_DOCUMENT_FILES = 5;
const MAX_DOCUMENT_FILE_BYTES = 10_000_000;
const MAX_DOCUMENT_TOTAL_BYTES = 20_000_000;
const THEMES = Object.freeze({
  paper: { label: '协作纸张', colorScheme: 'light', themeColor: '#faf5e6' },
  ocean: { label: '深海终端', colorScheme: 'dark', themeColor: '#071019' },
  graphite: { label: '石墨专业', colorScheme: 'light', themeColor: '#f5f7f9' },
  botanical: { label: '植物工作室', colorScheme: 'light', themeColor: '#f5f1e8' },
});

const el = {
  runList: document.querySelector('#run-list'),
  archivedToggle: document.querySelector('#archived-toggle'),
  archivedCount: document.querySelector('#archived-count'),
  archivedRunList: document.querySelector('#archived-run-list'),
  contextMenu: document.querySelector('#run-context-menu'),
  renameDialog: document.querySelector('#rename-run-dialog'),
  renameForm: document.querySelector('#rename-run-form'),
  renameInput: document.querySelector('#rename-run-input'),
  renameError: document.querySelector('#rename-run-error'),
  renameSubmit: document.querySelector('#rename-run-submit'),
  colorSchemeMeta: document.querySelector('#color-scheme-meta'),
  themeColorMeta: document.querySelector('#theme-color-meta'),
  refresh: document.querySelector('#refresh-button'),
  newTask: document.querySelector('#new-task-button'),
  emptyNewTask: document.querySelector('#empty-new-task-button'),
  tasksNew: document.querySelector('#tasks-new-button'),
  search: document.querySelector('#search-button'),
  searchPanel: document.querySelector('#run-search-panel'),
  searchInput: document.querySelector('#run-search-input'),
  closeSearch: document.querySelector('#close-search-button'),
  emptyState: document.querySelector('#empty-state'),
  runView: document.querySelector('#run-view'),
  chatView: document.querySelector('#chat-view'),
  tasksView: document.querySelector('#tasks-view'),
  workspaceChip: document.querySelector('#workspace-chip'),
  sidebarWorkspace: document.querySelector('#sidebar-workspace'),
  connectionDot: document.querySelector('#connection-dot'),
  stopTaskButton: document.querySelector('#stop-task-button'),
  resumeButton: document.querySelector('#resume-button'),
  claudeStatus: document.querySelector('#claude-status'),
  codexStatus: document.querySelector('#codex-status'),
  claudeNavDot: document.querySelector('#claude-nav-dot'),
  codexNavDot: document.querySelector('#codex-nav-dot'),
  statusBadge: document.querySelector('#run-status-badge'),
  errorBanner: document.querySelector('#error-banner'),
  artifactFeed: document.querySelector('#artifact-feed'),
  overview: document.querySelector('#run-overview'),
  runTimeline: document.querySelector('#run-timeline'),
  runTimelineCount: document.querySelector('#run-timeline-count'),
  eventTimeline: document.querySelector('#event-timeline'),
  evidenceBoard: document.querySelector('#evidence-board'),
  kanban: document.querySelector('#kanban-board'),
  taskCount: document.querySelector('#task-count-label'),
  planGate: document.querySelector('#plan-gate'),
  planGateNote: document.querySelector('#plan-gate-note'),
  messageForm: document.querySelector('#message-form'),
  quickTaskInput: document.querySelector('#quick-task-input'),
  quickTaskSubmit: document.querySelector('#quick-task-submit'),
  mentionMenu: document.querySelector('#composer-mention-menu'),
  quickAttach: document.querySelector('#quick-attach-button'),
  quickSettings: document.querySelector('#quick-settings-button'),
  detailPanel: document.querySelector('#detail-panel'),
  detailTitle: document.querySelector('#detail-title'),
  detailSubtitle: document.querySelector('#detail-subtitle'),
  detailOverview: document.querySelector('#detail-overview'),
  detailEvidence: document.querySelector('#detail-evidence'),
  detailAgentPanel: document.querySelector('#detail-agent'),
  agentProfile: document.querySelector('#agent-profile'),
  closeDetails: document.querySelector('#close-details-button'),
  newTaskDialog: document.querySelector('#new-task-dialog'),
  newTaskForm: document.querySelector('#new-task-form'),
  taskInput: document.querySelector('#task-input'),
  taskInputLabel: document.querySelector('#task-input-label'),
  documentInput: document.querySelector('#task-document-input'),
  documentDropZone: document.querySelector('#document-drop-zone'),
  documentList: document.querySelector('#selected-document-list'),
  taskDefaultsSummary: document.querySelector('#task-defaults-summary'),
  taskSettings: document.querySelector('#task-settings-button'),
  taskSubmit: document.querySelector('#task-submit'),
  formError: document.querySelector('#form-error'),
  settingsDialog: document.querySelector('#settings-dialog'),
  settingsForm: document.querySelector('#settings-form'),
  settingsError: document.querySelector('#settings-error'),
  settingsSubmit: document.querySelector('#settings-submit'),
  settingsReset: document.querySelector('#settings-reset-button'),
  settingsSavePath: document.querySelector('#settings-save-path'),
  workspaceBrowse: document.querySelector('#settings-workspace-browse'),
  workspaceBrowser: document.querySelector('#settings-workspace-browser'),
  workspaceParent: document.querySelector('#settings-workspace-parent'),
  workspaceCurrent: document.querySelector('#settings-workspace-current'),
  workspaceShortcuts: document.querySelector('#settings-workspace-shortcuts'),
  workspaceList: document.querySelector('#settings-workspace-list'),
  workspaceBrowserNote: document.querySelector('#settings-workspace-browser-note'),
  workspaceBrowserClose: document.querySelector('#settings-workspace-close'),
  workspaceSelect: document.querySelector('#settings-workspace-select'),
  shutdownUi: document.querySelector('#shutdown-ui-button'),
  feedbackDialog: document.querySelector('#feedback-dialog'),
  feedbackForm: document.querySelector('#feedback-form'),
  feedbackTitle: document.querySelector('#feedback-title'),
  feedbackInput: document.querySelector('#feedback-input'),
  targetAgentField: document.querySelector('#target-agent-field'),
  targetAgentInput: document.querySelector('#target-agent-input'),
  feedbackError: document.querySelector('#feedback-error'),
  feedbackSubmit: document.querySelector('#feedback-submit'),
  toast: document.querySelector('#toast'),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

async function bootstrap() {
  if (window.location.protocol === 'file:') {
    renderDirectFileNotice();
    return;
  }
  bindEvents();
  try {
    state.health = await api('/api/health');
    el.workspaceChip.textContent = state.health.workspace;
    el.sidebarWorkspace.textContent = workspaceFolderName(state.health.workspace);
    await loadSettings(state.health.workspace);
    setConnection(true);
    await loadRuns();
    connectEvents();
  } catch (error) {
    setConnection(false);
    showToast(error.message, true);
  }
}

function renderDirectFileNotice() {
  const heading = el.emptyState.querySelector('h1');
  const description = el.emptyState.querySelector('p');
  heading.textContent = '请打开 MultiAgent Web';
  description.innerHTML = '这个文件只是前端资源，不能自行启动本机后端。<br />请双击 MultiAgent Web 桌面启动器；如果服务已经启动，可以直接重新连接。';
  el.emptyNewTask.textContent = '重新连接本地服务';
  el.emptyNewTask.addEventListener('click', () => {
    window.location.href = 'http://127.0.0.1:8765/';
  }, { once: true });
  el.workspaceChip.textContent = '未连接本地服务';
  el.sidebarWorkspace.textContent = '请打开 MultiAgent Web';
  el.statusBadge.textContent = '未启动';
  el.statusBadge.dataset.status = 'failed';
  setConnection(false);
  document.querySelectorAll('.tool-nav-item, .channel-item, .dm-item').forEach((button) => {
    button.disabled = true;
  });
}

function bindEvents() {
  [el.newTask, el.emptyNewTask, el.tasksNew].forEach((button) => {
    button.addEventListener('click', openNewTask);
  });
  el.refresh.addEventListener('click', () => void refreshAll());
  el.archivedToggle.addEventListener('click', toggleArchivedRuns);
  el.stopTaskButton.addEventListener('click', () => void stopCurrentTask());
  el.resumeButton.addEventListener('click', () => void resumeCurrent());
  el.search.addEventListener('click', openRunSearch);
  el.closeSearch.addEventListener('click', closeRunSearch);
  el.searchInput.addEventListener('input', () => {
    const query = el.searchInput.value.trim().toLocaleLowerCase('zh-CN');
    if (!state.searchQuery && query) state.searchArchivedOpen = true;
    state.searchQuery = query;
    renderRunList();
  });
  [
    document.querySelector('#settings-button'),
    document.querySelector('#header-settings-button'),
    el.quickSettings,
  ].forEach((button) => button.addEventListener('click', () => void openSettings()));
  document.querySelector('#project-switcher').addEventListener('click', () => void openSettings());
  document.querySelector('#open-details-button').addEventListener('click', () => openDetails('evidence'));
  document.querySelector('#header-details-button').addEventListener('click', () => openDetails('overview'));
  el.closeDetails.addEventListener('click', closeDetails);

  document.querySelectorAll('[data-sidebar-tab]').forEach((button) => {
    button.addEventListener('click', () => setSidebarView(button.dataset.sidebarTab));
  });
  document.querySelectorAll('[data-main-view]').forEach((button) => {
    button.addEventListener('click', () => setMainView(button.dataset.mainView));
  });
  document.querySelectorAll('[data-detail-view]').forEach((button) => {
    button.addEventListener('click', () => setDetailView(button.dataset.detailView));
  });
  document.querySelectorAll('[data-agent]').forEach((button) => {
    button.addEventListener('click', () => openAgentProfile(button.dataset.agent));
  });

  el.artifactFeed.addEventListener('click', (event) => {
    const button = event.target.closest('[data-open-detail]');
    if (button) openDetails(button.dataset.openDetail);
  });
  el.artifactFeed.addEventListener('toggle', handleChangeToggle, true);

  el.messageForm.addEventListener('submit', (event) => {
    event.preventDefault();
    void submitQuickTask();
  });
  el.quickTaskInput.addEventListener('input', updateMentionMenu);
  el.quickTaskInput.addEventListener('keydown', handleComposerKeydown);
  el.quickTaskInput.addEventListener('blur', () => {
    window.setTimeout(hideMentionMenu, 100);
  });
  el.mentionMenu.addEventListener('pointerdown', (event) => {
    const option = event.target.closest('[data-mention]');
    if (!option) return;
    event.preventDefault();
    insertMention(option.dataset.mention);
  });
  el.quickAttach.addEventListener('click', () => {
    if (!el.taskInput.value.trim() && el.quickTaskInput.value.trim()) {
      el.taskInput.value = el.quickTaskInput.value.trim();
    }
    openNewTask();
    el.documentInput.click();
  });

  el.documentInput.addEventListener('change', () => {
    addTaskFiles(el.documentInput.files);
    el.documentInput.value = '';
  });
  ['dragenter', 'dragover'].forEach((name) => {
    el.documentDropZone.addEventListener(name, (event) => {
      event.preventDefault();
      el.documentDropZone.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    el.documentDropZone.addEventListener(name, (event) => {
      event.preventDefault();
      el.documentDropZone.classList.remove('dragging');
    });
  });
  el.documentDropZone.addEventListener('drop', (event) => addTaskFiles(event.dataTransfer?.files));
  el.documentList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-document]');
    if (!button) return;
    state.taskFiles.splice(Number(button.dataset.removeDocument), 1);
    renderTaskFiles();
    hideFormError(el.formError);
  });

  el.newTaskForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      el.newTaskDialog.close();
      return;
    }
    void submitTask();
  });

  el.taskSettings.addEventListener('click', () => {
    state.draftTaskMode = selectedTaskMode();
    el.newTaskDialog.close();
    void openSettings({ returnToNewTask: true });
  });
  el.settingsDialog.addEventListener('close', () => {
    restoreNewTaskAfterSettings();
  });

  el.settingsForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      el.settingsDialog.close();
      return;
    }
    void saveSettings();
  });
  el.settingsReset.addEventListener('click', () => void resetSettingsForm());
  el.shutdownUi.addEventListener('click', () => void shutdownUiService());
  el.workspaceBrowse.addEventListener('click', () => void openWorkspaceBrowser());
  el.workspaceParent.addEventListener('click', () => {
    const parent = el.workspaceParent.dataset.path;
    if (parent) void loadWorkspaceDirectory(parent);
  });
  el.workspaceBrowserClose.addEventListener('click', closeWorkspaceBrowser);
  el.workspaceSelect.addEventListener('click', selectWorkspaceDirectory);
  document.querySelectorAll('[data-settings-tab]').forEach((button) => {
    button.addEventListener('click', () => setSettingsTab(button.dataset.settingsTab));
  });
  document.querySelector('#settings-planning-collaboration').addEventListener('change', updateSettingsDependencies);
  document.querySelector('#settings-collaboration-mode').addEventListener('change', updateSettingsDependencies);
  document.querySelector('#settings-token-api-enabled').addEventListener('change', (event) => {
    if (!event.currentTarget.checked || !state.modelCatalog?.defaults) return;
    ['claude', 'codex'].forEach((agent) => {
      if (state.modelOrders[agent].length) return;
      state.modelOrders[agent] = [...(state.modelCatalog.defaults[agent] || [])];
      renderModelOrder(agent);
    });
  });
  document.querySelectorAll('[data-add-model]').forEach((button) => {
    button.addEventListener('click', () => addSelectedModel(button.dataset.addModel));
  });
  document.querySelectorAll('[data-add-custom-model]').forEach((button) => {
    button.addEventListener('click', () => addCustomModel(button.dataset.addCustomModel));
  });
  document.querySelectorAll('input[name="settings-theme"]').forEach((input) => {
    input.addEventListener('change', () => {
      void saveInterfacePreferences({ theme: input.value });
    });
  });
  document.querySelector('#settings-show-archived').addEventListener('change', (event) => {
    void saveInterfacePreferences({ show_archived: event.currentTarget.checked });
  });
  document.querySelector('#settings-compact-sidebar').addEventListener('change', (event) => {
    void saveInterfacePreferences({ compact_sidebar: event.currentTarget.checked });
  });
  document.querySelectorAll('input[name="task-mode"]').forEach((input) => {
    input.addEventListener('change', updateNewTaskMode);
  });

  el.feedbackForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      el.feedbackDialog.close();
      return;
    }
    void submitFeedback();
  });

  el.contextMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-context-action]');
    if (!button || button.disabled) return;
    void handleContextAction(button.dataset.contextAction);
  });
  el.renameForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel') {
      el.renameDialog.close();
      return;
    }
    void submitRunRename();
  });
  el.renameDialog.addEventListener('close', () => {
    state.renameRunId = null;
    hideFormError(el.renameError);
  });

  document.addEventListener('pointerdown', (event) => {
    if (!el.contextMenu.contains(event.target)) closeRunContextMenu();
    if (!el.messageForm.contains(event.target)) hideMentionMenu();
  });
  window.addEventListener('resize', closeRunContextMenu);
  document.querySelector('#sidebar-chat').addEventListener('scroll', closeRunContextMenu);

  document.querySelectorAll('[data-plan-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.planAction;
      if (action === 'revise' || action === 'targeted_revision') {
        openFeedback(action);
        return;
      }
      if (action === 'cancel' && !window.confirm('确定取消当前任务吗？代码尚未执行。')) return;
      void sendPlanAction({ action });
    });
  });

  window.addEventListener('keydown', (event) => {
    const openDialog = document.querySelector('dialog[open]');
    if (openDialog) {
      if (event.key === 'Escape' && openDialog === el.settingsDialog) {
        event.preventDefault();
        el.settingsDialog.close();
      }
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openRunSearch();
    }
    if (event.key === 'Escape') {
      if (!el.contextMenu.classList.contains('hidden')) closeRunContextMenu();
      else if (!el.searchPanel.classList.contains('hidden')) closeRunSearch();
      else if (!el.detailPanel.classList.contains('hidden')) closeDetails();
    }
  });
}

function handleComposerKeydown(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (!el.mentionMenu.classList.contains('hidden')) {
    const options = visibleMentionOptions();
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      setMentionIndex(state.mentionIndex + direction, options);
      return;
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && options.length) {
      event.preventDefault();
      insertMention(options[state.mentionIndex]?.dataset.mention || options[0].dataset.mention);
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      hideMentionMenu();
      return;
    }
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    hideMentionMenu();
    if (el.quickTaskInput.value.trim() && !el.quickTaskSubmit.disabled) {
      el.messageForm.requestSubmit(el.quickTaskSubmit);
    }
  }
}

function updateMentionMenu() {
  if (currentCollaborationMode() !== 'group_chat') {
    hideMentionMenu();
    return;
  }
  const caret = el.quickTaskInput.selectionStart;
  const beforeCaret = el.quickTaskInput.value.slice(0, caret);
  const match = beforeCaret.match(/@([A-Za-z]*)$/);
  if (!match) {
    hideMentionMenu();
    return;
  }
  const query = match[1].toLowerCase();
  const options = Array.from(el.mentionMenu.querySelectorAll('[data-mention]'));
  options.forEach((option) => {
    const mention = option.dataset.mention.slice(1).toLowerCase();
    option.classList.toggle('hidden', Boolean(query) && !mention.startsWith(query));
  });
  const visible = visibleMentionOptions();
  if (!visible.length) {
    hideMentionMenu();
    return;
  }
  state.mentionStart = caret - query.length - 1;
  state.mentionIndex = 0;
  el.mentionMenu.classList.remove('hidden');
  el.quickTaskInput.setAttribute('aria-expanded', 'true');
  setMentionIndex(0, visible);
}

function visibleMentionOptions() {
  return Array.from(el.mentionMenu.querySelectorAll('[data-mention]:not(.hidden)'));
}

function setMentionIndex(index, options = visibleMentionOptions()) {
  if (!options.length) return;
  state.mentionIndex = (index + options.length) % options.length;
  el.mentionMenu.querySelectorAll('[data-mention]').forEach((option) => {
    const selected = option === options[state.mentionIndex];
    option.classList.toggle('active', selected);
    option.setAttribute('aria-selected', selected ? 'true' : 'false');
  });
}

function insertMention(mention) {
  if (state.mentionStart < 0 || !mention) return;
  const end = el.quickTaskInput.selectionStart;
  const value = el.quickTaskInput.value;
  const replacement = `${mention} `;
  el.quickTaskInput.value = `${value.slice(0, state.mentionStart)}${replacement}${value.slice(end)}`;
  const caret = state.mentionStart + replacement.length;
  hideMentionMenu();
  el.quickTaskInput.focus();
  el.quickTaskInput.setSelectionRange(caret, caret);
}

function hideMentionMenu() {
  state.mentionStart = -1;
  state.mentionIndex = 0;
  el.mentionMenu.classList.add('hidden');
  el.quickTaskInput.setAttribute('aria-expanded', 'false');
  el.mentionMenu.querySelectorAll('[data-mention]').forEach((option) => {
    option.classList.remove('active');
    option.removeAttribute('aria-selected');
  });
}

async function loadSettings(workspace) {
  const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : '';
  const settings = await api(`/api/settings${query}`);
  state.settings = settings;
  applyInterfaceSettings(settings.values?.ui || {});
  renderTaskDefaults();
  return settings;
}

async function openSettings({ returnToNewTask = false } = {}) {
  if (returnToNewTask) state.returnToNewTaskAfterSettings = true;
  hideFormError(el.settingsError);
  const selected = state.runs.find((run) => run.id === state.currentId);
  const workspace = runWorkspace(selected) || state.settings?.workspace || state.health?.workspace || '';
  try {
    const settings = await loadSettings(workspace);
    populateSettingsForm(settings);
    setSettingsTab('general');
    el.settingsDialog.showModal();
  } catch (error) {
    showToast(error.message, true);
    restoreNewTaskAfterSettings();
  }
}

function restoreNewTaskAfterSettings() {
  if (!state.returnToNewTaskAfterSettings) return;
  state.returnToNewTaskAfterSettings = false;
  if (!el.newTaskDialog.open) openNewTask();
}

function populateSettingsForm(settings) {
  const values = settings.values || {};
  const setValue = (id, value) => {
    document.querySelector(`#${id}`).value = value ?? '';
  };
  const setChecked = (id, value) => {
    document.querySelector(`#${id}`).checked = Boolean(value);
  };
  setValue('settings-workspace', settings.workspace);
  setValue('settings-collaboration-mode', values.collaboration_mode || 'workflow');
  setValue('settings-group-chat-default-agent', values.group_chat_default_agent || 'both');
  setChecked('settings-group-chat-execution', values.group_chat_execution !== false);
  setValue('settings-executor', values.executor || 'claude');
  setChecked('settings-planning-collaboration', values.planning_collaboration);
  setChecked('settings-consensus', values.consensus);
  setChecked('settings-plan-approval', values.plan_approval);
  setValue('settings-max-consensus-rounds', values.max_consensus_rounds ?? 3);
  setValue('settings-max-plan-revisions', values.max_plan_revisions ?? 2);
  setValue('settings-review-rounds', values.review_rounds ?? 1);
  setChecked('settings-final-review', values.final_review);
  state.modelCatalog = settings.model_catalog || { claude: [], codex: [], defaults: {} };
  const credentials = settings.token_api_credentials || {};
  setChecked('settings-token-api-enabled', values.token_api?.enabled);
  setValue('settings-token-api-base-url', values.token_api?.base_url || 'https://tokencheap.io');
  setValue('settings-token-api-key', '');
  const tokenStatus = document.querySelector('#settings-token-api-status');
  tokenStatus.textContent = credentials.configured
    ? `已配置 ${credentials.masked || ''}${credentials.source ? ` · ${credentials.source}` : ''}`
    : '尚未配置 API Key';
  ['claude', 'codex'].forEach((agent) => {
    const agentValues = values[agent] || {};
    state.modelOrders[agent] = Array.isArray(agentValues.models)
      ? [...new Set(agentValues.models.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.trim()))]
      : agentValues.model ? [agentValues.model] : [];
    setChecked(`settings-${agent}-fallback`, agentValues.fallback_on_timeout !== false);
    setValue(`settings-${agent}-timeout`, agentValues.timeout ?? 900);
    setValue(`settings-${agent}-command`, formatCommandSetting(agentValues.command));
    setValue(`settings-${agent}-extra-args`, Array.isArray(agentValues.extra_args) ? agentValues.extra_args.join('\n') : '');
  });
  renderModelCatalog();
  setValue('settings-agent-a-identity', values.identities?.agent_a || '');
  setValue('settings-agent-b-identity', values.identities?.agent_b || '');
  setValue('settings-group-chat-agent-a-identity', values.group_chat_identities?.agent_a || '');
  setValue('settings-group-chat-agent-b-identity', values.group_chat_identities?.agent_b || '');
  setValue('settings-verification-timeout', values.verification?.timeout ?? 300);
  setValue('settings-verification-commands', JSON.stringify(values.verification?.commands || [], null, 2));
  const theme = normalizeTheme(values.ui?.theme);
  const themeInput = document.querySelector(`input[name="settings-theme"][value="${theme}"]`);
  if (themeInput) themeInput.checked = true;
  setChecked('settings-show-archived', values.ui?.show_archived);
  setChecked('settings-compact-sidebar', values.ui?.compact_sidebar);
  el.settingsSavePath.textContent = `保存位置：${settings.save_path}`;
  el.settingsSavePath.title = settings.save_path;
  el.settingsForm.dataset.workspace = settings.workspace;
  el.settingsForm.dataset.revision = settings.revision || '';
  closeWorkspaceBrowser();
  updateSettingsDependencies();
}

function renderModelCatalog() {
  ['claude', 'codex'].forEach((agent) => {
    const select = document.querySelector(`#settings-${agent}-model-add`);
    select.innerHTML = '';
    const groups = new Map();
    (state.modelCatalog?.[agent] || []).forEach((model) => {
      if (!groups.has(model.family)) groups.set(model.family, []);
      groups.get(model.family).push(model);
    });
    groups.forEach((models, family) => {
      const group = document.createElement('optgroup');
      group.label = family;
      models.forEach((model) => {
        const option = document.createElement('option');
        option.value = model.id;
        option.textContent = `${model.id}${model.temporary ? '（临时）' : ''}`;
        group.appendChild(option);
      });
      select.appendChild(group);
    });
    renderModelOrder(agent);
  });
}

function renderModelOrder(agent) {
  const list = document.querySelector(`#settings-${agent}-model-order`);
  list.innerHTML = '';
  const order = state.modelOrders[agent] || [];
  order.forEach((model, index) => {
    const item = document.createElement('li');
    item.dataset.modelIndex = String(index);
    const dragHandle = document.createElement('span');
    dragHandle.className = 'model-drag-handle';
    dragHandle.textContent = '⠿';
    dragHandle.title = `拖动 ${model} 调整顺序`;
    dragHandle.setAttribute('aria-label', dragHandle.title);
    dragHandle.draggable = true;
    dragHandle.addEventListener('dragstart', (event) => {
      state.draggedModel = { agent, index };
      item.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(index));
    });
    dragHandle.addEventListener('dragend', () => {
      state.draggedModel = null;
      item.classList.remove('dragging');
      list.querySelectorAll('.drag-over').forEach((row) => row.classList.remove('drag-over'));
    });
    item.addEventListener('dragover', (event) => {
      if (state.draggedModel?.agent !== agent) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      item.classList.add('drag-over');
    });
    item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
    item.addEventListener('drop', (event) => {
      event.preventDefault();
      item.classList.remove('drag-over');
      const source = state.draggedModel;
      state.draggedModel = null;
      if (!source || source.agent !== agent || source.index === index) return;
      moveModel(agent, source.index, index);
    });
    const rank = document.createElement('span');
    rank.className = 'model-rank';
    rank.textContent = String(index + 1);
    const name = document.createElement('code');
    name.textContent = model;
    const actions = document.createElement('span');
    actions.className = 'model-order-actions';
    [
      ['上移', index - 1],
      ['下移', index + 1],
    ].forEach(([label, target]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'model-order-button';
      button.textContent = label;
      button.title = `${label} ${model}`;
      button.setAttribute('aria-label', button.title);
      button.disabled = target < 0 || target >= order.length;
      button.addEventListener('click', () => moveModel(agent, index, target));
      actions.appendChild(button);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'model-order-button remove';
    remove.textContent = '×';
    remove.title = `移除 ${model}`;
    remove.addEventListener('click', () => {
      state.modelOrders[agent].splice(index, 1);
      renderModelOrder(agent);
    });
    actions.appendChild(remove);
    item.append(dragHandle, rank, name, actions);
    list.appendChild(item);
  });
  if (!order.length) {
    const empty = document.createElement('li');
    empty.className = 'model-order-empty';
    empty.textContent = '使用原生 CLI 默认模型';
    list.appendChild(empty);
  }
}

function addSelectedModel(agent) {
  const value = document.querySelector(`#settings-${agent}-model-add`).value;
  addModel(agent, value);
}

function addCustomModel(agent) {
  const input = document.querySelector(`#settings-${agent}-custom-model`);
  const value = input.value.trim();
  if (addModel(agent, value)) input.value = '';
}

function addModel(agent, model) {
  if (!model) return false;
  if (state.modelOrders[agent].includes(model)) {
    showToast(`${model} 已在列表中。`, true);
    return false;
  }
  state.modelOrders[agent].push(model);
  renderModelOrder(agent);
  return true;
}

function moveModel(agent, from, to) {
  if (to < 0 || to >= state.modelOrders[agent].length) return;
  const [model] = state.modelOrders[agent].splice(from, 1);
  state.modelOrders[agent].splice(to, 0, model);
  renderModelOrder(agent);
}

function formatCommandSetting(command) {
  if (Array.isArray(command)) return JSON.stringify(command);
  return typeof command === 'string' ? command : '';
}

function setSettingsTab(tab) {
  const requested = ['general', 'workflow', 'agents', 'verification', 'interface'].includes(tab) ? tab : 'general';
  const mode = document.querySelector('#settings-collaboration-mode').value;
  const requestedPanel = document.querySelector(`[data-settings-panel="${requested}"]`);
  const selected = requestedPanel?.dataset.collaborationOnly && requestedPanel.dataset.collaborationOnly !== mode
    ? 'general'
    : requested;
  document.querySelectorAll('[data-settings-tab]').forEach((button) => {
    button.classList.toggle('active', button.dataset.settingsTab === selected);
  });
  document.querySelectorAll('[data-settings-panel]').forEach((panel) => {
    const applies = !panel.dataset.collaborationOnly || panel.dataset.collaborationOnly === mode;
    panel.classList.toggle('hidden', panel.dataset.settingsPanel !== selected || !applies);
  });
}

function updateSettingsDependencies() {
  const mode = document.querySelector('#settings-collaboration-mode').value;
  const workflow = mode === 'workflow';
  const planning = document.querySelector('#settings-planning-collaboration').checked;
  const consensus = document.querySelector('#settings-consensus');
  const workflowOnly = [
    'settings-executor',
    'settings-planning-collaboration',
    'settings-consensus',
    'settings-plan-approval',
    'settings-max-consensus-rounds',
    'settings-max-plan-revisions',
    'settings-review-rounds',
    'settings-final-review',
  ];
  workflowOnly.forEach((id) => { document.querySelector(`#${id}`).disabled = !workflow; });
  consensus.disabled = !workflow || !planning;
  ['settings-group-chat-default-agent', 'settings-group-chat-execution'].forEach((id) => {
    document.querySelector(`#${id}`).disabled = workflow;
  });
  document.querySelectorAll('[data-collaboration-only]').forEach((item) => {
    if (item.dataset.settingsPanel) return;
    item.classList.toggle('hidden', item.dataset.collaborationOnly !== mode);
  });
  const activeTab = document.querySelector('[data-settings-tab].active')?.dataset.settingsTab || 'general';
  setSettingsTab(activeTab);
}

async function openWorkspaceBrowser() {
  hideFormError(el.settingsError);
  const workspace = document.querySelector('#settings-workspace').value.trim()
    || state.settings?.workspace
    || state.health?.workspace
    || '';
  el.workspaceBrowser.classList.remove('hidden');
  await loadWorkspaceDirectory(workspace);
}

function closeWorkspaceBrowser() {
  el.workspaceBrowser.classList.add('hidden');
  state.workspaceBrowserPath = '';
}

async function loadWorkspaceDirectory(path) {
  el.workspaceList.innerHTML = '<div class="workspace-directory-empty">正在读取目录…</div>';
  el.workspaceBrowserNote.textContent = '';
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    const listing = await api(`/api/directories${query}`);
    state.workspaceBrowserPath = listing.path;
    el.workspaceCurrent.textContent = listing.path;
    el.workspaceCurrent.title = listing.path;
    el.workspaceParent.dataset.path = listing.parent || '';
    el.workspaceParent.disabled = !listing.parent;
    renderWorkspaceDirectoryButtons(el.workspaceShortcuts, [
      ...(listing.roots || []),
      ...(listing.shortcuts || []),
    ], true);
    renderWorkspaceDirectoryButtons(el.workspaceList, listing.directories || [], false);
    el.workspaceBrowserNote.textContent = listing.truncated
      ? '目录较多，仅显示前 500 个文件夹。'
      : `${(listing.directories || []).length} 个子文件夹`;
  } catch (error) {
    state.workspaceBrowserPath = '';
    el.workspaceCurrent.textContent = '无法打开目录';
    el.workspaceList.innerHTML = '';
    el.workspaceBrowserNote.textContent = error.message;
  }
}

function renderWorkspaceDirectoryButtons(container, directories, compact) {
  container.innerHTML = '';
  const seen = new Set();
  directories.forEach((directory) => {
    if (!directory?.path || seen.has(directory.path)) return;
    seen.add(directory.path);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = compact ? 'workspace-shortcut' : 'workspace-directory';
    button.title = directory.path;
    button.textContent = compact ? directory.name : `▰  ${directory.name}`;
    button.addEventListener('click', () => void loadWorkspaceDirectory(directory.path));
    container.appendChild(button);
  });
  if (!compact && !container.childElementCount) {
    const empty = document.createElement('div');
    empty.className = 'workspace-directory-empty';
    empty.textContent = '当前文件夹没有子文件夹';
    container.appendChild(empty);
  }
}

function selectWorkspaceDirectory() {
  if (!state.workspaceBrowserPath) return;
  document.querySelector('#settings-workspace').value = state.workspaceBrowserPath;
  closeWorkspaceBrowser();
}

async function resetSettingsForm() {
  const workspace = document.querySelector('#settings-workspace').value.trim();
  if (!workspace) {
    showFormError(el.settingsError, '请先填写有效工作区。');
    return;
  }
  try {
    const defaults = await api(`/api/settings?defaults=1&workspace=${encodeURIComponent(workspace)}`);
    populateSettingsForm(defaults);
    const saved = await saveInterfacePreferences(defaults.values?.ui || {});
    if (saved) {
      showToast('界面设置已恢复并保存；其他默认值点击“保存设置”后生效。');
    }
  } catch (error) {
    showFormError(el.settingsError, error.message);
  }
}

async function saveSettings() {
  hideFormError(el.settingsError);
  setButtonBusy(el.settingsSubmit, true, '正在保存…');
  try {
    await state.interfaceSavePromise;
    const workspace = document.querySelector('#settings-workspace').value.trim();
    if (!workspace) throw new Error('工作区不能为空。');
    let revision = el.settingsForm.dataset.revision || '';
    if (workspace !== el.settingsForm.dataset.workspace) {
      const target = await api(`/api/settings?workspace=${encodeURIComponent(workspace)}`);
      revision = target.revision || '';
    }
    const payload = {
      workspace,
      revision,
      values: collectSettingsValues(),
      token_api_key: document.querySelector('#settings-token-api-key').value.trim(),
    };
    const saved = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.settings = saved;
    state.health.workspace = saved.workspace;
    applyInterfaceSettings(saved.values?.ui || {});
    renderTaskDefaults();
    el.settingsDialog.close();
    showToast('设置已保存；界面已更新，任务流程配置将用于之后启动的任务。');
  } catch (error) {
    showFormError(el.settingsError, error.message);
  } finally {
    setButtonBusy(el.settingsSubmit, false, '保存设置');
  }
}

function saveInterfacePreferences(values) {
  const workspace = el.settingsForm.dataset.workspace || state.settings?.workspace || '';
  const preferences = {};
  if (Object.hasOwn(values, 'theme')) preferences.theme = normalizeTheme(values.theme);
  if (Object.hasOwn(values, 'show_archived')) preferences.show_archived = Boolean(values.show_archived);
  if (Object.hasOwn(values, 'compact_sidebar')) preferences.compact_sidebar = Boolean(values.compact_sidebar);
  applyInterfaceSettings(preferences, { partial: true });
  if (!workspace || !Object.keys(preferences).length) return Promise.resolve(false);

  if (state.settings?.values && state.settings.workspace === workspace) {
    state.settings.values.ui = {
      ...(state.settings.values.ui || {}),
      ...preferences,
    };
  }

  state.interfaceSavePromise = state.interfaceSavePromise
    .catch(() => {})
    .then(async () => {
      const saved = await api('/api/settings/interface', {
        method: 'POST',
        body: JSON.stringify({ workspace, ui: preferences }),
      });
      if (state.settings?.workspace === workspace) {
        const active = currentInterfaceSettings();
        state.settings = saved;
        state.settings.values.ui = {
          ...(saved.values?.ui || {}),
          ...active,
        };
      }
      if (el.settingsForm.dataset.workspace === workspace) {
        el.settingsForm.dataset.revision = saved.revision || '';
      }
      return true;
    })
    .catch((error) => {
      showToast(`界面已更新，但未能保存：${error.message}`, true);
      return false;
    });
  return state.interfaceSavePromise;
}

async function shutdownUiService() {
  if (!window.confirm('确定关闭本地 Web 服务吗？正在运行的任务必须先停止。')) return;
  setButtonBusy(el.shutdownUi, true, '正在关闭…');
  try {
    await api('/api/shutdown', {
      method: 'POST',
      body: '{}',
    });
    state.eventSource?.close();
    window.clearTimeout(state.refreshTimer);
    el.settingsDialog.close();
    setConnection(false);
    el.statusBadge.textContent = '服务已关闭';
    el.statusBadge.dataset.status = 'completed';
    el.workspaceChip.textContent = '本地服务已关闭';
    showToast('本地服务已关闭，可以关闭此页面。');
  } catch (error) {
    setButtonBusy(el.shutdownUi, false, '关闭服务');
    showToast(error.message, true);
  }
}

function collectSettingsValues() {
  const get = (id) => document.querySelector(`#${id}`);
  const integer = (id, label, minimum) => {
    const value = Number(get(id).value);
    if (!Number.isInteger(value) || value < minimum) throw new Error(`${label}必须是大于或等于 ${minimum} 的整数。`);
    return value;
  };
  const positive = (id, label) => {
    const value = Number(get(id).value);
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${label}必须是正数。`);
    return value;
  };
  let commands;
  try {
    commands = JSON.parse(get('settings-verification-commands').value || '[]');
  } catch {
    throw new Error('验证命令必须是有效的 JSON 数组。');
  }
  if (!Array.isArray(commands)) throw new Error('验证命令必须是 JSON 数组。');
  const agent = (name) => ({
    command: parseCommandSetting(get(`settings-${name}-command`).value, name),
    model: state.modelOrders[name][0] || '',
    models: [...state.modelOrders[name]],
    fallback_on_timeout: get(`settings-${name}-fallback`).checked,
    timeout: positive(`settings-${name}-timeout`, `${agentName(name)} 超时`),
    extra_args: get(`settings-${name}-extra-args`).value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
  });
  return {
    executor: get('settings-executor').value,
    collaboration_mode: get('settings-collaboration-mode').value,
    group_chat_default_agent: get('settings-group-chat-default-agent').value,
    group_chat_execution: get('settings-group-chat-execution').checked,
    planning_collaboration: get('settings-planning-collaboration').checked,
    consensus: get('settings-consensus').checked,
    max_consensus_rounds: integer('settings-max-consensus-rounds', '最大共识审核轮次', 1),
    plan_approval: get('settings-plan-approval').checked,
    max_plan_revisions: integer('settings-max-plan-revisions', '最大人工修订次数', 0),
    review_rounds: integer('settings-review-rounds', '代码审核轮次', 0),
    final_review: get('settings-final-review').checked,
    identities: {
      agent_a: get('settings-agent-a-identity').value.trim(),
      agent_b: get('settings-agent-b-identity').value.trim(),
    },
    group_chat_identities: {
      agent_a: get('settings-group-chat-agent-a-identity').value.trim(),
      agent_b: get('settings-group-chat-agent-b-identity').value.trim(),
    },
    verification: {
      timeout: positive('settings-verification-timeout', '验证超时'),
      commands,
    },
    token_api: {
      enabled: get('settings-token-api-enabled').checked,
      base_url: get('settings-token-api-base-url').value.trim(),
    },
    claude: agent('claude'),
    codex: agent('codex'),
    ui: {
      theme: document.querySelector('input[name="settings-theme"]:checked')?.value || 'paper',
      show_archived: get('settings-show-archived').checked,
      compact_sidebar: get('settings-compact-sidebar').checked,
    },
  };
}

function parseCommandSetting(value, name) {
  const text = value.trim();
  if (!text) return '';
  if (!text.startsWith('[')) return text;
  try {
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed) || !parsed.length || !parsed.every((item) => typeof item === 'string')) throw new Error();
    return parsed;
  } catch {
    throw new Error(`${agentName(name)} CLI 命令的 JSON 数组无效。`);
  }
}

function applyInterfaceSettings(ui, { partial = false } = {}) {
  if (!partial || Object.hasOwn(ui, 'theme')) applyTheme(ui.theme);
  if (!partial || Object.hasOwn(ui, 'show_archived')) {
    state.showArchived = Boolean(ui.show_archived);
    if (state.runs.length) renderRunList();
  }
  if (!partial || Object.hasOwn(ui, 'compact_sidebar')) {
    document.body.classList.toggle('compact-sidebar', Boolean(ui.compact_sidebar));
  }
}

function currentInterfaceSettings() {
  return {
    theme: normalizeTheme(document.body.dataset.theme),
    show_archived: state.showArchived,
    compact_sidebar: document.body.classList.contains('compact-sidebar'),
  };
}

function normalizeTheme(value) {
  return Object.hasOwn(THEMES, value) ? value : 'paper';
}

function applyTheme(value) {
  const theme = normalizeTheme(value);
  const settings = THEMES[theme];
  document.body.dataset.theme = theme;
  document.documentElement.style.colorScheme = settings.colorScheme;
  el.colorSchemeMeta.setAttribute('content', settings.colorScheme);
  el.themeColorMeta.setAttribute('content', settings.themeColor);
}

function renderTaskDefaults() {
  if (!el.taskDefaultsSummary) return;
  const values = state.settings?.values || {};
  const workspace = workspaceFolderName(state.settings?.workspace || state.health?.workspace || '');
  const executor = agentName(values.executor || 'claude');
  const selected = document.querySelector('input[name="task-mode"]:checked')?.value;
  const collaborationMode = selected || values.collaboration_mode || 'workflow';
  const mode = collaborationMode === 'group_chat'
    ? '群聊协作 · 可定向执行'
    : values.consensus ? '共识实施 · 证据化共识' : values.planning_collaboration === false ? '单智能体执行' : '共识实施 · 快速协作';
  el.taskDefaultsSummary.textContent = collaborationMode === 'group_chat'
    ? `${workspace} · Claude Code + Codex · ${mode}`
    : `${workspace} · ${executor} 执行 · ${mode}`;
}

function updateNewTaskMode() {
  const groupChat = selectedTaskMode() === 'group_chat';
  el.taskInput.required = !groupChat;
  el.taskInputLabel.textContent = groupChat ? '第一条消息（可选）' : '目标与需求';
  el.taskInput.placeholder = groupChat
    ? '可以留空，创建群聊后再从底部发送第一条消息…'
    : '描述需要 Claude Code 和 Codex 共同完成的任务…';
  el.taskSubmit.textContent = groupChat ? '创建群聊' : '开始协作';
  hideFormError(el.formError);
  renderTaskDefaults();
}

function setSidebarView(view) {
  const members = view === 'members';
  document.querySelector('#sidebar-chat').classList.toggle('hidden', members);
  document.querySelector('#sidebar-members').classList.toggle('hidden', !members);
  document.querySelectorAll('[data-sidebar-tab]').forEach((button) => {
    button.classList.toggle('active', button.dataset.sidebarTab === view);
  });
}

function setMainView(view) {
  if (!state.currentId && view === 'tasks') {
    showToast('先创建一个协作任务。', true);
    return;
  }
  state.mainView = view === 'tasks' ? 'tasks' : 'chat';
  el.chatView.classList.toggle('hidden', state.mainView !== 'chat');
  el.tasksView.classList.toggle('hidden', state.mainView !== 'tasks');
  document.querySelectorAll('[data-main-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.mainView === state.mainView);
  });
}

function openDetails(view = 'overview') {
  el.detailPanel.classList.remove('hidden');
  setDetailView(view);
}

function closeDetails() {
  el.detailPanel.classList.add('hidden');
}

function setDetailView(view) {
  state.detailView = ['overview', 'evidence', 'agent'].includes(view) ? view : 'overview';
  el.detailOverview.classList.toggle('hidden', state.detailView !== 'overview');
  el.detailEvidence.classList.toggle('hidden', state.detailView !== 'evidence');
  el.detailAgentPanel.classList.toggle('hidden', state.detailView !== 'agent');
  document.querySelectorAll('[data-detail-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.detailView === state.detailView);
  });
  const labels = {
    overview: ['运行详情', '状态、耗时与活动记录'],
    evidence: ['证据', '需求、争议与决策'],
    agent: [agentName(state.detailAgent), '对等协作者资料'],
  };
  [el.detailTitle.textContent, el.detailSubtitle.textContent] = labels[state.detailView];
}

function openAgentProfile(agent) {
  state.detailAgent = agent === 'codex' ? 'codex' : 'claude';
  renderAgentProfile();
  openDetails('agent');
}

function openRunSearch() {
  el.searchPanel.classList.remove('hidden');
  el.search.classList.add('active');
  el.search.setAttribute('aria-expanded', 'true');
  window.setTimeout(() => el.searchInput.focus(), 0);
}

function closeRunSearch() {
  state.searchQuery = '';
  state.searchArchivedOpen = true;
  el.searchInput.value = '';
  el.searchPanel.classList.add('hidden');
  el.search.classList.remove('active');
  el.search.setAttribute('aria-expanded', 'false');
  renderRunList();
}

function openNewTask() {
  hideFormError(el.formError);
  const defaultMode = state.draftTaskMode || state.settings?.values?.collaboration_mode || 'workflow';
  state.draftTaskMode = null;
  const modeInput = document.querySelector(`input[name="task-mode"][value="${defaultMode}"]`);
  if (modeInput) modeInput.checked = true;
  updateNewTaskMode();
  renderTaskFiles();
  el.newTaskDialog.showModal();
  window.setTimeout(() => {
    (selectedTaskMode() === 'group_chat' ? el.taskSubmit : el.taskInput).focus();
  }, 0);
}

function addTaskFiles(fileList) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;
  const errors = [];
  incoming.forEach((file) => {
    const extension = file.name.includes('.') ? file.name.split('.').at(-1).toLowerCase() : '';
    if (!DOCUMENT_EXTENSIONS.has(extension)) {
      errors.push(`${file.name} 不是支持的文档格式`);
      return;
    }
    if (!file.size) {
      errors.push(`${file.name} 是空文件`);
      return;
    }
    if (file.size > MAX_DOCUMENT_FILE_BYTES) {
      errors.push(`${file.name} 超过 10 MB`);
      return;
    }
    const duplicate = state.taskFiles.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified);
    if (duplicate) return;
    if (state.taskFiles.length >= MAX_DOCUMENT_FILES) {
      errors.push(`每个任务最多上传 ${MAX_DOCUMENT_FILES} 个文档`);
      return;
    }
    const total = state.taskFiles.reduce((sum, item) => sum + item.size, 0) + file.size;
    if (total > MAX_DOCUMENT_TOTAL_BYTES) {
      errors.push('文档合计大小不能超过 20 MB');
      return;
    }
    state.taskFiles.push(file);
  });
  renderTaskFiles();
  if (errors.length) showFormError(el.formError, errors[0]);
  else hideFormError(el.formError);
}

function renderTaskFiles() {
  el.documentList.replaceChildren();
  el.documentList.classList.toggle('hidden', !state.taskFiles.length);
  state.taskFiles.forEach((file, index) => {
    const row = document.createElement('div');
    row.className = 'selected-document';
    const icon = document.createElement('span');
    icon.className = 'selected-document-icon';
    icon.textContent = '▧';
    const copy = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = file.name;
    const size = document.createElement('small');
    size.textContent = formatBytes(file.size);
    copy.append(name, size);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.dataset.removeDocument = String(index);
    remove.setAttribute('aria-label', `移除 ${file.name}`);
    remove.title = '移除文档';
    remove.textContent = '×';
    row.append(icon, copy, remove);
    el.documentList.append(row);
  });
}

async function encodeTaskFiles() {
  return Promise.all(state.taskFiles.map(async (file) => ({
    name: file.name,
    size: file.size,
    content_type: file.type || 'application/octet-stream',
    data: await readFileAsBase64(file),
  })));
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取文档：${file.name}`));
    reader.onload = () => {
      const result = String(reader.result || '');
      const separator = result.indexOf(',');
      if (separator < 0) reject(new Error(`无法编码文档：${file.name}`));
      else resolve(result.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}

function clearTaskFiles() {
  state.taskFiles = [];
  el.documentInput.value = '';
  renderTaskFiles();
}

async function submitTask() {
  const task = el.taskInput.value.trim();
  const collaborationMode = selectedTaskMode();
  if (!task && collaborationMode !== 'group_chat') {
    showFormError(el.formError, '需求不能为空。');
    return;
  }
  if (!task && state.taskFiles.length) {
    showFormError(el.formError, '添加参考文档时，请同时填写第一条消息。');
    return;
  }
  setButtonBusy(el.taskSubmit, true, '正在启动…');
  hideFormError(el.formError);
  try {
    const attachments = await encodeTaskFiles();
    await startTask({
      task,
      attachments,
      ...taskSettingsPayload(collaborationMode),
    });
    el.newTaskDialog.close();
    el.taskInput.value = '';
    el.quickTaskInput.value = '';
    clearTaskFiles();
  } catch (error) {
    showFormError(el.formError, error.message);
  } finally {
    setButtonBusy(el.taskSubmit, false, collaborationMode === 'group_chat' ? '创建群聊' : '开始协作');
  }
}

async function submitQuickTask() {
  const task = el.quickTaskInput.value.trim();
  if (!task) {
    el.quickTaskInput.focus();
    return;
  }
  setButtonBusy(el.quickTaskSubmit, true, '正在发送…');
  try {
    if (currentCollaborationMode() === 'group_chat' && state.currentId) {
      await sendGroupChatMessage(task);
    } else {
      await startTask({
        task,
        ...taskSettingsPayload(state.settings?.values?.collaboration_mode || 'workflow'),
      });
    }
    el.quickTaskInput.value = '';
    hideMentionMenu();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(el.quickTaskSubmit, false, '发送 ◁');
  }
}

function selectedTaskMode() {
  return document.querySelector('input[name="task-mode"]:checked')?.value || state.settings?.values?.collaboration_mode || 'workflow';
}

function taskSettingsPayload(collaborationMode = 'workflow') {
  const values = state.settings?.values || {};
  return {
    workspace: state.settings?.workspace || state.health?.workspace || '',
    config: state.settings?.source_path || '',
    executor: values.executor || 'claude',
    consensus: Boolean(values.consensus),
    collaboration_mode: collaborationMode,
  };
}

async function startTask(payload) {
  const hasInitialMessage = Boolean(String(payload.task || '').trim());
  const session = await api('/api/tasks', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  state.currentId = session.id;
  state.detail = null;
  state.mainView = 'chat';
  showToast(session.collaboration_mode === 'group_chat'
    ? hasInitialMessage
      ? '群聊已创建，正在等待 Agent 回复。'
      : '群聊已创建，可以发送第一条消息。'
    : '任务已启动，Claude Code 与 Codex 正在并行分析。');
  await refreshAll();
}

async function sendGroupChatMessage(message) {
  if (!state.currentId) throw new Error('没有选中的群聊对话。');
  const runId = state.currentId;
  const pending = queuePendingChatMessage(runId, message);
  renderDetail();
  scrollChatToBottom();
  try {
    await api(`/api/sessions/${encodeURIComponent(runId)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    });
    pending.delivery_status = 'accepted';
    if (state.currentId === runId) renderDetail();
    showToast('消息已发送到群聊。');
    await refreshAll();
  } catch (error) {
    removePendingChatMessage(runId, pending.client_id);
    if (state.currentId === runId) renderDetail();
    throw error;
  }
}

function queuePendingChatMessage(runId, content) {
  const chat = state.detail?.session?.group_chat || state.detail?.record?.group_chat || {};
  const serverMessageCount = Array.isArray(chat.messages) ? chat.messages.length : 0;
  const pending = {
    client_id: `pending-${Date.now()}-${state.pendingChatSequence += 1}`,
    sender: 'user',
    role: 'user',
    content,
    recipients: [],
    created_at: new Date().toISOString(),
    action: 'discuss',
    optimistic: true,
    delivery_status: 'sending',
    server_message_count: serverMessageCount,
    expected_recipients: optimisticChatRecipients(content),
    server_user_id: '',
  };
  const messages = state.pendingChatMessages.get(runId) || [];
  messages.push(pending);
  state.pendingChatMessages.set(runId, messages);
  return pending;
}

function optimisticChatRecipients(content) {
  const aliases = {
    a: 'claude',
    agenta: 'claude',
    'agent-a': 'claude',
    claude: 'claude',
    b: 'codex',
    agentb: 'codex',
    'agent-b': 'codex',
    codex: 'codex',
  };
  const broadcasts = new Set(['all', 'both', 'everyone']);
  const mentions = [...String(content).matchAll(/@([A-Za-z][A-Za-z0-9_-]*)/g)]
    .map((match) => match[1].toLowerCase());
  if (mentions.some((value) => broadcasts.has(value))) return ['claude', 'codex'];
  if (mentions.length) {
    const selected = new Set(mentions.map((value) => aliases[value]).filter(Boolean));
    return ['claude', 'codex'].filter((agent) => selected.has(agent));
  }
  const fallback = state.settings?.values?.group_chat_default_agent || 'both';
  return fallback === 'both' ? ['claude', 'codex'] : [fallback];
}

function removePendingChatMessage(runId, clientId) {
  const remaining = (state.pendingChatMessages.get(runId) || [])
    .filter((message) => message.client_id !== clientId);
  if (remaining.length) state.pendingChatMessages.set(runId, remaining);
  else state.pendingChatMessages.delete(runId);
}

function reconcilePendingChatMessages(runId, serverMessages, runStatus) {
  const pending = state.pendingChatMessages.get(runId) || [];
  const active = ACTIVE_RUN_STATUSES.has(String(runStatus || '').toLowerCase());
  const remaining = pending.filter((optimistic) => {
    if (!optimistic.server_user_id) {
      const serverUser = serverMessages
        .slice(optimistic.server_message_count)
        .find((message) => message.sender === 'user' && message.content === optimistic.content);
      if (serverUser) optimistic.server_user_id = serverUser.id || '';
    }
    const responded = new Set(serverMessages
      .filter((message) => (
        message.role === 'assistant'
        && optimistic.server_user_id
        && message.reply_to === optimistic.server_user_id
      ))
      .map((message) => message.sender));
    optimistic.waiting_recipients = optimistic.expected_recipients
      .filter((agent) => !responded.has(agent));
    if (!optimistic.waiting_recipients.length) return false;
    return !optimistic.server_user_id || active;
  });
  if (remaining.length) state.pendingChatMessages.set(runId, remaining);
  else state.pendingChatMessages.delete(runId);
  return remaining;
}

function scrollChatToBottom() {
  window.requestAnimationFrame(() => {
    el.artifactFeed.scrollTop = el.artifactFeed.scrollHeight;
  });
}

function currentCollaborationMode() {
  return state.detail?.session?.collaboration_mode
    || state.detail?.record?.collaboration_mode
    || null;
}

async function resumeCurrent() {
  if (!state.currentId) return;
  await resumeRun(state.currentId);
}

async function resumeRun(runId) {
  el.resumeButton.disabled = true;
  try {
    await api('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ resume_id: runId }),
    });
    state.currentId = runId;
    state.detail = null;
    showToast('任务已从精确检查点恢复。');
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    el.resumeButton.disabled = false;
  }
}

function openFeedback(action) {
  state.feedbackAction = action;
  hideFormError(el.feedbackError);
  el.feedbackInput.value = '';
  const targeted = action === 'targeted_revision';
  el.feedbackTitle.textContent = targeted ? '向指定智能体提出要求' : '修订统一方案';
  el.targetAgentField.classList.toggle('hidden', !targeted);
  el.feedbackDialog.showModal();
  window.setTimeout(() => el.feedbackInput.focus(), 0);
}

async function submitFeedback() {
  const feedback = el.feedbackInput.value.trim();
  if (!feedback) {
    showFormError(el.feedbackError, '修订要求不能为空。');
    return;
  }
  setButtonBusy(el.feedbackSubmit, true, '正在发送…');
  try {
    await sendPlanAction({
      action: state.feedbackAction,
      feedback,
      target_agent: state.feedbackAction === 'targeted_revision' ? el.targetAgentInput.value : '',
    });
    el.feedbackDialog.close();
  } catch (error) {
    showFormError(el.feedbackError, error.message);
  } finally {
    setButtonBusy(el.feedbackSubmit, false, '发送要求');
  }
}

async function sendPlanAction(payload) {
  if (!state.currentId) throw new Error('没有选中的任务。');
  try {
    await api(`/api/sessions/${encodeURIComponent(state.currentId)}/actions`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const labels = {
      execute: '已批准统一方案，准备进入执行阶段。',
      revise: '整体修订要求已发送。',
      targeted_revision: '定向要求已发送给目标智能体。',
      export: '正在导出最终技术文档。',
      cancel: '正在取消任务。',
    };
    showToast(labels[payload.action] || '操作已提交。');
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
    throw error;
  }
}

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource('/api/events');
  state.eventSource = source;
  source.addEventListener('ready', () => setConnection(true));
  source.addEventListener('update', (event) => {
    try {
      const update = JSON.parse(event.data || '{}');
      if (update.type === 'workspace') {
        void refreshDefaultWorkspace();
        return;
      }
    } catch {
      // A malformed notification should still trigger the normal refresh path.
    }
    scheduleRefresh();
  });
  source.onopen = () => setConnection(true);
  source.onerror = () => {
    setConnection(false);
    scheduleRefresh(1200);
  };
  window.setInterval(() => void refreshAll(), 8000);
}

function scheduleRefresh(delay = 100) {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(() => void refreshAll(), delay);
}

async function refreshAll() {
  await loadRuns(false, false);
  if (state.currentId) await loadDetail(state.currentId);
}

async function refreshDefaultWorkspace() {
  try {
    const health = await api('/api/health');
    const workspace = health.workspace || '';
    if (!workspace || workspace === state.health?.workspace) {
      scheduleRefresh();
      return;
    }
    state.health = health;
    state.currentId = null;
    state.detail = null;
    el.workspaceChip.textContent = workspace;
    el.workspaceChip.title = workspace;
    el.sidebarWorkspace.textContent = workspaceFolderName(workspace);
    await loadSettings(workspace);
    await loadRuns(true);
    showToast(`默认工作区已切换到 ${workspaceFolderName(workspace)}。`);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadRuns(selectNewest = true, loadSelected = true) {
  const payload = await api('/api/runs');
  state.runs = Array.isArray(payload.runs) ? payload.runs.map(normalizeRunSummary) : [];
  if (state.currentId && !state.runs.some((run) => run.id === state.currentId)) state.currentId = null;
  if (!state.currentId && selectNewest) {
    const workspace = state.health?.workspace || '';
    state.currentId = state.runs.find((run) => (
      !run.archived && (!workspace || run.workspace === workspace)
    ))?.id || null;
  }
  renderRunList();
  if (state.currentId && loadSelected) await loadDetail(state.currentId);
  if (!state.currentId) renderEmpty();
}

async function loadDetail(runId) {
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (state.currentId !== runId) return;
    state.detail = detail;
    const run = state.runs.find((item) => item.id === runId);
    if (run?.detached && detail.record?.checkpoint) {
      run.resumable = true;
      renderRunList();
    }
    renderDetail();
  } catch (error) {
    if (state.currentId === runId) showToast(error.message, true);
  }
}

function selectRun(runId) {
  closeRunContextMenu();
  state.currentId = runId;
  state.detail = null;
  state.mainView = 'chat';
  renderRunList();
  void loadDetail(runId);
}

function renderRunList() {
  el.runList.replaceChildren();
  el.archivedRunList.replaceChildren();
  const matchingRuns = state.searchQuery
    ? state.runs.filter(runMatchesSearch)
    : state.runs;
  const activeRuns = matchingRuns.filter((run) => !run.archived);
  const archivedRuns = matchingRuns.filter((run) => run.archived);
  const totalArchived = state.runs.filter((run) => run.archived).length;
  const archiveExpanded = state.searchQuery ? state.searchArchivedOpen : state.showArchived;

  if (!activeRuns.length) {
    const empty = document.createElement('p');
    empty.className = 'sidebar-empty';
    empty.textContent = state.searchQuery
      ? '活跃对话中没有匹配项。'
      : state.runs.length
      ? '没有活跃对话。可展开已归档，或创建新任务。'
      : '还没有任务。在协作大厅开始一次协作。';
    el.runList.append(empty);
  } else {
    renderProjectGroups(el.runList, activeRuns, 'active');
  }

  el.archivedCount.textContent = state.searchQuery
    ? `${archivedRuns.length}/${totalArchived}`
    : String(totalArchived);
  el.archivedToggle.setAttribute('aria-expanded', String(archiveExpanded));
  el.archivedToggle.classList.toggle('expanded', archiveExpanded);
  el.archivedRunList.classList.toggle('hidden', !archiveExpanded);
  if (archiveExpanded) {
    if (archivedRuns.length) renderProjectGroups(el.archivedRunList, archivedRuns, 'archived');
    else {
      const empty = document.createElement('p');
      empty.className = 'sidebar-empty';
      empty.textContent = state.searchQuery ? '已归档对话中没有匹配项。' : '暂无已归档对话。';
      el.archivedRunList.append(empty);
    }
  }
}

function renderProjectGroups(container, runs, section) {
  const groups = new Map();
  runs.forEach((run) => {
    const workspace = runWorkspace(run);
    if (!groups.has(workspace)) groups.set(workspace, []);
    groups.get(workspace).push(run);
  });

  groups.forEach((projectRuns, workspace) => {
    const group = document.createElement('section');
    group.className = 'project-group';
    const collapseKey = `${section}:${workspace}`;
    const collapsed = state.collapsedProjects.has(collapseKey);
    const header = document.createElement('button');
    header.type = 'button';
    header.className = 'project-group-header';
    header.setAttribute('aria-expanded', String(!collapsed));
    header.innerHTML = `<span class="project-chevron" aria-hidden="true">${collapsed ? '›' : '⌄'}</span><span class="project-folder" aria-hidden="true">▰</span><strong>${escapeHtml(workspaceFolderName(workspace))}</strong><span class="project-count">${projectRuns.length}</span>`;
    header.addEventListener('click', () => {
      if (collapsed) state.collapsedProjects.delete(collapseKey);
      else state.collapsedProjects.add(collapseKey);
      renderRunList();
    });
    group.append(header);

    if (!collapsed) {
      const list = document.createElement('div');
      list.className = 'project-run-list';
      projectRuns.forEach((run) => list.append(createRunButton(run)));
      group.append(list);
    }
    container.append(group);
  });
}

function createRunButton(run) {
  const button = document.createElement('button');
  const taskTitle = displayTaskTitle(run);
  button.type = 'button';
  button.className = `run-item${run.id === state.currentId ? ' selected' : ''}${run.archived ? ' archived' : ''}`;
  button.setAttribute('aria-label', `打开对话：${taskTitle}`);
  button.addEventListener('click', () => selectRun(run.id));
  button.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    openRunContextMenu(run, event.clientX, event.clientY);
  });
  button.addEventListener('keydown', (event) => {
    if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
    event.preventDefault();
    const bounds = button.getBoundingClientRect();
    openRunContextMenu(run, bounds.left + 18, bounds.top + 18);
  });

  const title = document.createElement('span');
  title.className = 'run-item-title';
  title.textContent = taskTitle;
  const meta = document.createElement('span');
  meta.className = 'run-item-meta';
  const dot = document.createElement('span');
  dot.className = `status-dot status-${statusKey(run.status)}`;
  const status = document.createElement('span');
  status.textContent = `${statusLabel(run.status)} · ${relativeTime(run.updated_at)}`;
  meta.append(dot, status);
  button.append(title, meta);
  return button;
}

function toggleArchivedRuns() {
  if (state.searchQuery) state.searchArchivedOpen = !state.searchArchivedOpen;
  else state.showArchived = !state.showArchived;
  closeRunContextMenu();
  renderRunList();
}

function openRunContextMenu(run, clientX, clientY) {
  state.contextRunId = run.id;
  const resume = el.contextMenu.querySelector('[data-context-action="resume"]');
  const stop = el.contextMenu.querySelector('[data-context-action="stop"]');
  const archive = el.contextMenu.querySelector('[data-context-action="archive"]');
  const deleteButton = el.contextMenu.querySelector('[data-context-action="delete"]');
  const archiveLabel = archive.querySelector('span:last-child');
  const active = run.live === true;

  resume.classList.toggle('hidden', !run.resumable);
  stop.classList.toggle('hidden', !active);
  stop.disabled = String(run.status || '').toLowerCase() === 'stopping';
  stop.querySelector('span:last-child').textContent = stop.disabled ? '正在停止…' : '停止任务';
  archive.disabled = active;
  archiveLabel.textContent = active
    ? '运行中（不可归档）'
    : run.archived ? '取消归档' : '归档对话';
  archive.querySelector('span:first-child').textContent = run.archived ? '↩' : '□';
  deleteButton.classList.toggle('hidden', !run.archived);

  el.contextMenu.classList.remove('hidden');
  const bounds = el.contextMenu.getBoundingClientRect();
  const left = Math.max(8, Math.min(clientX, window.innerWidth - bounds.width - 8));
  const top = Math.max(8, Math.min(clientY, window.innerHeight - bounds.height - 8));
  el.contextMenu.style.left = `${left}px`;
  el.contextMenu.style.top = `${top}px`;
  el.contextMenu.querySelector('[data-context-action="open"]').focus();
}

function closeRunContextMenu() {
  state.contextRunId = null;
  el.contextMenu.classList.add('hidden');
}

async function handleContextAction(action) {
  const run = state.runs.find((item) => item.id === state.contextRunId);
  if (!run) {
    closeRunContextMenu();
    return;
  }
  closeRunContextMenu();
  if (action === 'open') {
    selectRun(run.id);
    return;
  }
  if (action === 'rename') {
    openRunRename(run);
    return;
  }
  if (action === 'resume') {
    await resumeRun(run.id);
    return;
  }
  if (action === 'stop') {
    await requestTaskStop(run.id);
    return;
  }
  if (action === 'copy-id') {
    await copyText(run.id, '任务 ID');
    return;
  }
  if (action === 'copy-workspace') {
    await copyText(runWorkspace(run), '项目路径');
    return;
  }
  if (action === 'delete') {
    await deleteArchivedRun(run);
    return;
  }
  if (action === 'archive') await setRunArchived(run.id, !run.archived);
}

function openRunRename(run) {
  state.renameRunId = run.id;
  el.renameInput.value = displayTaskTitle(run);
  hideFormError(el.renameError);
  el.renameDialog.showModal();
  window.setTimeout(() => {
    el.renameInput.focus();
    el.renameInput.select();
  }, 0);
}

async function submitRunRename() {
  const runId = state.renameRunId;
  const title = el.renameInput.value.trim();
  if (!runId) {
    el.renameDialog.close();
    return;
  }
  if (!title) {
    showFormError(el.renameError, '任务名称不能为空。');
    return;
  }
  setButtonBusy(el.renameSubmit, true, '正在保存…');
  hideFormError(el.renameError);
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
    el.renameDialog.close();
    showToast('任务名称已更新。');
    await refreshAll();
  } catch (error) {
    showFormError(el.renameError, error.message);
  } finally {
    setButtonBusy(el.renameSubmit, false, '保存名称');
  }
}

async function deleteArchivedRun(run) {
  const title = displayTaskTitle(run);
  if (!window.confirm(`永久删除“${title}”？\n\n任务记录、检查点和上传文档都会被删除，且无法恢复。`)) return;
  try {
    await api(`/api/runs/${encodeURIComponent(run.id)}`, { method: 'DELETE' });
    if (state.currentId === run.id) {
      state.currentId = null;
      state.detail = null;
    }
    showToast('已永久删除归档任务。');
    await loadRuns(true);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function setRunArchived(runId, archived) {
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived }),
    });
    if (archived && state.currentId === runId) {
      state.currentId = null;
      state.detail = null;
    } else if (!archived) {
      state.currentId = runId;
      state.detail = null;
    }
    showToast(archived ? '对话已归档，可在“已归档”区域找回。' : '对话已移回项目列表。');
    await loadRuns(true);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function stopCurrentTask() {
  if (!state.currentId) {
    showToast('没有选中的运行中任务。', true);
    return;
  }
  await requestTaskStop(state.currentId);
}

async function requestTaskStop(runId) {
  if (!window.confirm('确定停止当前任务吗？已完成的步骤和检查点会保留，可稍后恢复。')) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(runId)}/stop`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
    showToast('正在停止 Claude Code、Codex 和当前验证进程…');
    await refreshAll();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function copyText(value, label) {
  if (!value) {
    showToast(`没有可复制的${label}。`, true);
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const input = document.createElement('textarea');
    input.value = value;
    input.setAttribute('readonly', '');
    input.style.position = 'fixed';
    input.style.opacity = '0';
    document.body.append(input);
    input.select();
    const copied = document.execCommand('copy');
    input.remove();
    if (!copied) {
      showToast(`复制${label}失败。`, true);
      return;
    }
  }
  showToast(`已复制${label}。`);
}

function renderEmpty() {
  el.emptyState.classList.remove('hidden');
  el.runView.classList.add('hidden');
  el.stopTaskButton.classList.add('hidden');
  el.resumeButton.classList.add('hidden');
  el.statusBadge.textContent = '等待任务';
  el.statusBadge.dataset.status = 'waiting';
  document.title = 'MultiAgent 工作台';
}

function renderDetail() {
  const record = state.detail?.record || {};
  const session = state.detail?.session || null;
  const recordedStatus = session?.status || record.status || 'unknown';
  const detached = !session && ACTIVE_RUN_STATUSES.has(String(recordedStatus).toLowerCase());
  const status = detached ? 'interrupted' : recordedStatus;
  const workspace = session?.workspace || record.workspace || state.health?.workspace || '';
  const task = session?.task || record.display_task || record.task || '未命名任务';
  const collaborationMode = session?.collaboration_mode || record.collaboration_mode || 'workflow';
  const groupChat = collaborationMode === 'group_chat';

  el.emptyState.classList.add('hidden');
  el.runView.classList.remove('hidden');
  el.workspaceChip.textContent = workspace;
  el.workspaceChip.title = workspace;
  el.sidebarWorkspace.textContent = workspaceFolderName(workspace);
  el.statusBadge.textContent = statusLabel(status);
  el.statusBadge.dataset.status = statusKey(status);
  document.title = `${task} · MultiAgent`;
  el.quickTaskInput.placeholder = groupChat
    ? '讨论：@Claude（Claude Code）请审核…；执行：@Claude 执行：…（一次仅一个 Agent）'
    : '在协作大厅输入新需求，让 Claude Code 与 Codex 开始协作';
  el.quickTaskSubmit.textContent = groupChat ? '发送消息 ◁' : '发送 ◁';
  el.quickAttach.classList.toggle('hidden', groupChat);
  if (!groupChat) hideMentionMenu();

  const error = detached
    ? '任务已不在当前 UI 服务中运行；上次服务可能退出，可从最近检查点恢复。'
    : session?.error || record.error || '';
  el.errorBanner.textContent = error;
  el.errorBanner.classList.toggle('notice-banner', Boolean(error) && ['interrupted', 'cancelled'].includes(status));
  el.errorBanner.classList.toggle('hidden', !error);

  renderAgentStatus(el.claudeStatus, el.claudeNavDot, 'claude', 'Claude Code', session, status);
  renderAgentStatus(el.codexStatus, el.codexNavDot, 'codex', 'Codex', session, status);
  renderArtifacts(record, session);
  renderOverview(record, session, status);
  renderTimeline(record, session);
  renderEvidence(record);
  renderKanban(record);
  renderPlanGate(session);
  renderAgentProfile();
  document.querySelectorAll('[data-main-view="tasks"]').forEach((button) => {
    button.classList.toggle('hidden', groupChat);
  });
  if (groupChat && state.mainView === 'tasks') state.mainView = 'chat';
  setMainView(state.mainView);

  const canResume = ['failed', 'interrupted', 'cancelled'].includes(status) && Boolean(record.checkpoint);
  const canStop = ['starting', 'running', 'awaiting_plan', 'stopping'].includes(status);
  el.stopTaskButton.classList.toggle('hidden', !canStop);
  el.stopTaskButton.disabled = status === 'stopping';
  el.stopTaskButton.textContent = status === 'stopping' ? '■ 正在停止…' : '■ 停止';
  el.resumeButton.classList.toggle('hidden', !canResume);
}

function renderAgentStatus(container, navDot, key, name, session, runStatus) {
  const event = session?.agent_events?.[key];
  const stopping = runStatus === 'stopping';
  const eventStatus = stopping ? 'stopping' : event?.status || (runStatus === 'complete' ? 'complete' : runStatus);
  const detail = stopping ? fallbackAgentDetail(name, runStatus) : event?.safe_summary || fallbackAgentDetail(name, runStatus);
  const elapsed = Number(event?.elapsed_seconds);
  container.innerHTML = `
    <div class="agent-status-title">
      <span class="status-dot status-${statusKey(eventStatus)}"></span>
      <span class="agent-status-name">${escapeHtml(name)}</span>
      <span class="agent-status-time">${Number.isFinite(elapsed) ? formatDuration(elapsed) : ''}</span>
    </div>
    <div class="agent-status-detail">${escapeHtml(detail)}</div>`;
  navDot.className = `status-dot status-${statusKey(eventStatus)}`;
}

function fallbackAgentDetail(name, status) {
  if (status === 'complete') return '本次协作已完成';
  if (status === 'ready') return `等待下一条群聊消息或 @${name}`;
  if (status === 'awaiting_plan') return '方案已提交，等待用户决定';
  if (status === 'stopping') return '正在安全停止当前任务';
  if (['failed', 'cancelled', 'interrupted'].includes(status)) return '当前任务已停止';
  return `等待 ${name} 的安全进度事件`;
}

function renderArtifacts(record, session) {
  const collaborationMode = session?.collaboration_mode || record.collaboration_mode || 'workflow';
  if (collaborationMode === 'group_chat') {
    renderGroupChat(record, session);
    return;
  }
  const checkpoint = record.checkpoint || {};
  const artifacts = checkpoint.artifacts || {};
  const plan = session?.plan || {};
  const task = session?.task || record.display_task || record.task || '';
  const attachments = session?.attachments || record.attachments || [];
  const parts = [messageCard('你', '需求方', { final_text: task, attachments }, 'user', '需求')];

  const proposalA = plan.proposal_a || artifacts.proposal_a;
  const proposalB = plan.proposal_b || artifacts.proposal_b;
  if (proposalA) parts.push(messageCard('Claude Code', '独立方案 · 对等审核 Codex', proposalA, 'claude', '方案'));
  if (proposalB) parts.push(messageCard('Codex', '独立方案 · 对等审核 Claude Code', proposalB, 'codex', '方案'));

  const reviews = plan.cross_reviews || [artifacts.cross_review_a, artifacts.cross_review_b].filter(Boolean);
  if (reviews[0]) parts.push(messageCard('Claude Code', '对 Codex 方案的交叉审核', reviews[0], 'claude', '审核', true));
  if (reviews[1]) parts.push(messageCard('Codex', '对 Claude Code 方案的交叉审核', reviews[1], 'codex', '审核', true));

  const unified = plan.unified_proposal || artifacts.unified_proposal;
  if (unified) {
    const agent = agentKeyFromName(unified.agent);
    parts.push(messageCard(unified.agent || '协作组', '双方共同认可的统一方案', unified, agent, '统一方案', true, true));
  }

  const consensus = Object.entries(artifacts)
    .filter(([key]) => key.startsWith('consensus_review_v'))
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
  consensus.forEach(([key, result]) => {
    parts.push(messageCard(result.agent || '审核', `共识审核 ${key.replace('consensus_review_', '')}`, result, agentKeyFromName(result.agent), '共识审核', true));
  });
  if (plan.consensus_review && !consensus.length) {
    parts.push(messageCard(plan.consensus_review.agent || '审核', '最新统一方案审核', plan.consensus_review, agentKeyFromName(plan.consensus_review.agent), '共识审核', true));
  }

  if (artifacts.execution_result) {
    const result = artifacts.execution_result;
    parts.push(messageCard(
      result.agent || '执行智能体',
      '实施结果',
      {
        ...result,
        changes: checkpoint.change_summary || result.changes,
        changes_key: `workflow-${record.id || 'current'}`,
      },
      agentKeyFromName(result.agent),
      '实施',
      true,
    ));
  }
  const codeReviews = Array.isArray(checkpoint.reviews) ? checkpoint.reviews : [];
  codeReviews.forEach((review, index) => {
    parts.push(messageCard(review.agent || '审核智能体', `第 ${index + 1} 轮代码审核`, review, agentKeyFromName(review.agent), '代码审核', true));
  });

  el.artifactFeed.innerHTML = parts.join('');
}

function renderGroupChat(record, session) {
  const chat = session?.group_chat || record.group_chat || {};
  const serverMessages = Array.isArray(chat.messages) ? chat.messages : [];
  const pendingTurns = reconcilePendingChatMessages(
    record.id || state.currentId || 'current',
    serverMessages,
    session?.status || record.status,
  );
  const pendingUsers = pendingTurns.filter((message) => !message.server_user_id);
  const pendingReplies = pendingTurns.flatMap((turn) => (
    (turn.waiting_recipients || turn.expected_recipients).map((agent) => ({
      id: `${turn.client_id}-${agent}`,
      sender: agent,
      role: 'assistant',
      content: '',
      recipients: ['user'],
      created_at: turn.created_at,
      action: turn.action,
      loading_reply: true,
    }))
  ));
  const messages = [...serverMessages, ...pendingUsers, ...pendingReplies];
  const parts = messages.map((message) => {
    const sender = message.sender || 'system';
    const user = sender === 'user';
    const recipients = Array.isArray(message.recipients) ? message.recipients : [];
    const recipientNames = recipients
      .filter((value) => value !== 'user')
      .map(agentName)
      .join(' + ');
    const execution = message.action === 'execute';
    const optimistic = message.optimistic === true;
    const loadingReply = message.loading_reply === true;
    const role = loadingReply
      ? '正在回复 · 等待内容'
      : optimistic
      ? message.delivery_status === 'accepted' ? '已发送 · 等待 Agent 回复' : '正在发送到群聊'
      : user
      ? `${execution ? '执行请求' : '讨论消息'} · 发送给 ${recipientNames || '群聊'}`
      : execution ? '目标工作区执行结果 · 共享给所有成员' : '群聊回复 · 共享给所有成员';
    return messageCard(
      user ? '你' : agentName(sender),
      role,
      {
        final_text: message.content || '',
        attachments: message.attachments || [],
        duration_seconds: message.duration_seconds || 0,
        created_at: message.created_at || '',
        workspace: message.workspace || '',
        changes: message.changes || null,
        changes_key: `chat-${record.id || 'current'}-${message.id || 'message'}`,
        pending: optimistic,
        loading: loadingReply,
      },
      user ? 'user' : sender,
      loadingReply
        ? '回复中'
        : optimistic
        ? message.delivery_status === 'accepted' ? '已发送' : '发送中'
        : execution ? (user ? '执行' : '执行结果') : (user ? '消息' : '回复'),
    );
  });
  el.artifactFeed.innerHTML = parts.join('') || '<div class="board-empty">发送第一条消息开始群聊。</div>';
}

function messageCard(name, role, result, agent, tag, details = false, highlight = false) {
  if (!result) return '';
  const speaker = ['user', 'claude', 'codex'].includes(agent) ? agent : 'system';
  const text = result.final_text ?? result.content ?? '';
  const duration = Number(result.duration_seconds);
  const createdAt = result.created_at ? formatEventTime(result.created_at) : '';
  const attachments = Array.isArray(result.attachments) ? result.attachments : [];
  const workspace = result.workspace || '';
  const changes = result.changes;
  const pending = result.pending === true;
  const loading = result.loading === true;
  return `<article class="message-row message-${speaker}${highlight ? ' message-highlight' : ''}${pending ? ' message-pending' : ''}${loading ? ' message-loading' : ''}">
    ${avatarMarkup(agent)}
    <div class="message-main">
      <div class="message-header">
        <strong>${escapeHtml(name)}</strong>
        <span class="message-role">${escapeHtml(role)}</span>
        <span class="message-time">${createdAt || (Number.isFinite(duration) && duration > 0 ? formatDuration(duration) : '')}</span>
      </div>
      ${tag ? `<span class="message-tag">${escapeHtml(tag)}</span>` : ''}
      ${workspace ? `<div class="execution-workspace"><strong>写入工作区</strong><code title="${escapeHtml(workspace)}">${escapeHtml(workspace)}</code></div>` : ''}
      ${loading ? replyLoadingMarkup(name) : `<div class="markdown-body">${renderMarkdown(normalizeContent(text))}</div>`}
      ${changeSummaryMarkup(changes, result.changes_key)}
      ${attachmentMarkup(attachments)}
      ${details ? '<div class="message-actions"><button class="thread-button" data-open-detail="evidence" type="button">▢ 查看证据</button></div>' : ''}
    </div>
  </article>`;
}

function replyLoadingMarkup(name) {
  return `<div class="reply-loading" role="status" aria-label="${escapeHtml(name)} 正在回复">
    <span class="reply-loading-dots" aria-hidden="true"><i></i><i></i><i></i></span>
    <span>${escapeHtml(name)} 正在思考并组织回复…</span>
  </div>`;
}

function changeSummaryMarkup(summary, rawKey) {
  if (!summary || typeof summary !== 'object') return '';
  const key = String(rawKey || 'changes');
  const available = summary.available !== false;
  const files = Array.isArray(summary.files) ? summary.files : [];
  const fileCount = Math.max(0, Number(summary.file_count) || files.length);
  const additions = Math.max(0, Number(summary.additions) || 0);
  const deletions = Math.max(0, Number(summary.deletions) || 0);
  const title = available
    ? fileCount ? `已修改 ${fileCount} 个文件` : '未检测到文件修改'
    : '无法生成变更预览';
  const body = available
    ? files.map((file) => changeFileMarkup(file, key)).join('') || '<div class="change-empty">本次执行没有产生可见的文件变化。</div>'
    : `<div class="change-empty">${escapeHtml(summary.reason || '当前工作区无法计算文件差异。')}</div>`;
  const truncated = summary.truncated
    ? '<div class="change-warning">文件列表或 diff 内容过大，当前仅显示部分预览。</div>'
    : '';
  const open = state.openChangeSummaries.has(key) ? ' open' : '';
  return `<details class="change-summary" data-change-summary="${escapeHtml(key)}"${open}>
    <summary>
      <span class="change-summary-icon" aria-hidden="true">±</span>
      <span class="change-summary-title"><strong>${escapeHtml(title)}</strong><small><span class="change-add">+${additions}</span> <span class="change-delete">-${deletions}</span></small></span>
      <span class="change-summary-toggle">展开</span>
    </summary>
    <div class="change-files">${truncated}${body}</div>
  </details>`;
}

function changeFileMarkup(file, summaryKey) {
  const path = String(file?.path || '未知文件');
  const key = `${summaryKey}:${path}`;
  const binary = file?.binary === true;
  const additions = Number.isFinite(Number(file?.additions)) ? Math.max(0, Number(file.additions)) : null;
  const deletions = Number.isFinite(Number(file?.deletions)) ? Math.max(0, Number(file.deletions)) : null;
  const patch = String(file?.patch || '');
  const preview = patch
    ? `<pre class="diff-preview"><code>${diffMarkup(patch)}</code></pre>`
    : `<div class="change-empty">${binary ? '二进制文件已变化，无法显示逐行预览。' : '没有可显示的文本 diff。'}</div>`;
  const open = state.openChangeFiles.has(key) ? ' open' : '';
  return `<details class="change-file" data-change-file="${escapeHtml(key)}"${open}>
    <summary>
      <span class="change-file-status status-${escapeHtml(file?.status || 'modified')}">${escapeHtml(changeStatusLabel(file?.status))}</span>
      <code title="${escapeHtml(path)}">${escapeHtml(path)}</code>
      <span class="change-file-stats">${binary ? '<span>二进制</span>' : `<span class="change-add">+${additions ?? 0}</span> <span class="change-delete">-${deletions ?? 0}</span>`}</span>
    </summary>
    ${preview}
  </details>`;
}

function handleChangeToggle(event) {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement)) return;
  const summaryKey = details.dataset.changeSummary;
  const fileKey = details.dataset.changeFile;
  if (summaryKey) {
    if (details.open) state.openChangeSummaries.add(summaryKey);
    else state.openChangeSummaries.delete(summaryKey);
  }
  if (fileKey) {
    if (details.open) state.openChangeFiles.add(fileKey);
    else state.openChangeFiles.delete(fileKey);
  }
}

function diffMarkup(patch) {
  return patch.split('\n').map((line) => {
    let kind = 'context';
    if (line.startsWith('+++') || line.startsWith('---')) kind = 'header';
    else if (line.startsWith('+')) kind = 'add';
    else if (line.startsWith('-')) kind = 'delete';
    else if (line.startsWith('@@') || line.startsWith('diff ') || line.startsWith('index ')) kind = 'meta';
    return `<span class="diff-line diff-${kind}">${escapeHtml(line || ' ')}</span>`;
  }).join('');
}

function changeStatusLabel(status) {
  return { added: '新增', deleted: '删除', modified: '修改' }[status] || '修改';
}

function attachmentMarkup(attachments) {
  if (!attachments.length) return '';
  return `<div class="message-attachments">${attachments.map((item) => `<span class="message-attachment" title="${escapeHtml(item.path || item.name || '')}"><span aria-hidden="true">▧</span><strong>${escapeHtml(item.name || '文档')}</strong><small>${escapeHtml(formatBytes(item.size || 0))}</small></span>`).join('')}</div>`;
}

function avatarMarkup(agent) {
  const key = agent === 'codex' ? 'codex' : agent === 'user' ? 'user' : agent === 'claude' ? 'claude' : 'system';
  const initials = { claude: 'CL', codex: 'CX', user: 'YO', system: 'MA' }[key];
  return `<span class="avatar avatar-${key}">${initials}</span>`;
}

function renderOverview(record, session, status) {
  const summary = record.summary || {};
  const checkpoint = record.checkpoint || {};
  const collaboration = record.collaboration || checkpoint.collaboration || {};
  const collaborationMode = session?.collaboration_mode || record.collaboration_mode || 'workflow';
  const rows = [
    ['状态', statusLabel(status)],
    ['工作区', compactPath(session?.workspace || record.workspace || '')],
    ['协作模式', collaborationMode === 'group_chat' ? '群聊协作' : '共识实施'],
    ['执行方式', collaborationMode === 'group_chat' ? '按消息中的 @ 动态指定' : agentName(session?.executor || record.executor)],
    ['方案版本', `v${collaboration.proposal_version || 0}`],
    ['流程细节', collaborationMode === 'group_chat' ? '讨论只读 · 单 Agent 写目标工作区 · 全员共享上下文' : (session?.consensus ?? record.consensus) ? '证据化共识' : '快速协作'],
    ['累计耗时', formatDuration(summary.elapsed_seconds || 0)],
    ['运行次数', String(record.attempts || 1)],
    ['输入令牌数', formatNumber(summary.input_tokens || 0)],
    ['输出令牌数', formatNumber(summary.output_tokens || 0)],
    ['上传文档', `${Array.isArray(session?.attachments || record.attachments) ? (session?.attachments || record.attachments).length : 0} 个`],
  ];
  if (collaborationMode === 'group_chat') {
    rows.push(['执行轮次', `${summary.execution_turns || 0} 次`]);
  }
  el.overview.innerHTML = rows.map(([label, value]) => `<div class="info-row"><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join('');
}

function renderTimeline(record, session) {
  const combined = [...(Array.isArray(record.events) ? record.events : []), ...(Array.isArray(session?.events) ? session.events : [])];
  const seen = new Set();
  const events = combined.filter((event) => {
    if (event.kind === 'collaboration') return false;
    const key = `${event.timestamp || ''}|${event.source || ''}|${event.step_id || ''}|${event.safe_summary || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const visibleEvents = events.slice(-80).reverse();
  el.runTimeline.classList.toggle('hidden', !events.length);
  el.runTimelineCount.textContent = events.length > visibleEvents.length
    ? `最近 ${visibleEvents.length} / ${events.length} 条事件`
    : `${events.length} 条事件`;
  if (!events.length) {
    el.eventTimeline.innerHTML = '<div class="board-empty">智能体开始工作后，活动记录会显示在这里。</div>';
    return;
  }
  el.eventTimeline.innerHTML = visibleEvents.map((event) => `<div class="event-item">
    <div class="event-item-head"><span class="status-dot status-${statusKey(event.status)}"></span><strong>${escapeHtml(eventSourceLabel(event.source))}</strong></div>
    <div>${escapeHtml(event.safe_summary || event.text || eventKindLabel(event.kind))}</div>
    <small>${escapeHtml(stepLabel(event.step_id) || eventKindLabel(event.kind, ''))} · ${escapeHtml(formatEventTime(event.timestamp))}</small>
  </div>`).join('');
}

function renderEvidence(record) {
  const collaboration = record.collaboration || record.checkpoint?.collaboration || {};
  const issues = Array.isArray(collaboration.issues) ? collaboration.issues : [];
  const requirements = Array.isArray(collaboration.requirements) ? collaboration.requirements : [];
  const instructions = (Array.isArray(collaboration.messages) ? collaboration.messages : []).filter((message) => message.kind === 'instruction');
  const items = [];

  if (record.technical_document) {
    items.push(evidenceItem('MARKDOWN', '技术文档已导出', record.technical_document, 'complete'));
  }
  issues.forEach((issue) => {
    items.push(evidenceItem(issue.severity || '争议', `${issue.id || '争议'} · ${issue.problem || ''}`, `${issueStatusLabel(issue.status)} · ${issue.resolution || '尚未解决'}`, issue.status === 'resolved' ? 'complete' : 'failed'));
  });
  requirements.forEach((requirement) => {
    items.push(evidenceItem(requirement.id || 'REQ', requirement.text || '', `${requirement.covered ? '已覆盖' : '未覆盖'} · ${(requirement.evidence || []).length} 项证据`, requirement.covered ? 'complete' : 'waiting'));
  });
  instructions.forEach((instruction) => {
    items.push(evidenceItem(`你 → ${agentName(instruction.recipient)}`, instruction.body || '', '定向要求', 'awaiting_plan'));
  });
  el.evidenceBoard.innerHTML = items.join('') || '<div class="board-empty">暂无结构化证据或争议。</div>';
}

function evidenceItem(tag, title, meta, status) {
  return `<div class="evidence-item">
    <div class="evidence-item-head"><span class="status-dot status-${statusKey(status)}"></span><span class="tag">${escapeHtml(tag)}</span></div>
    <strong>${escapeHtml(title)}</strong><small>${escapeHtml(meta)}</small>
  </div>`;
}

function renderKanban(record) {
  const collaboration = record.collaboration || record.checkpoint?.collaboration || {};
  const tasks = Array.isArray(collaboration.tasks) ? collaboration.tasks : [];
  const columns = [
    ['todo', '待处理'],
    ['progress', '进行中'],
    ['blocked', '受阻'],
    ['done', '已完成'],
  ];
  el.taskCount.textContent = `本次协作共 ${tasks.length} 项任务`;
  el.kanban.innerHTML = columns.map(([lane, label]) => {
    const laneTasks = tasks.filter((task) => taskLane(task) === lane);
    const cards = laneTasks.length
      ? laneTasks.map((task, index) => `<article class="task-card"><small>协作大厅 · ${escapeHtml(task.id || String(index + 1))}</small><strong>${escapeHtml(task.title || task.id || '未命名任务')}</strong><div class="task-card-meta">负责人：${escapeHtml(agentName(task.owner))} · ${escapeHtml(statusLabel(task.status))}</div></article>`).join('')
      : '<div class="kanban-empty">暂无任务</div>';
    return `<section class="kanban-column"><div class="kanban-column-header"><span class="kanban-badge ${lane}">${label}</span><span class="kanban-count">${laneTasks.length}</span></div>${cards}</section>`;
  }).join('');
}

function taskLane(task) {
  const status = String(task.status || '').toLowerCase();
  if (['done', 'complete', 'completed', 'approved', 'skipped'].includes(status)) return 'done';
  if (['in_progress', 'running', 'working'].includes(status)) return 'progress';
  if (['blocked', 'failed', 'rejected'].includes(status)) return 'blocked';
  return 'todo';
}

function renderAgentProfile() {
  const record = state.detail?.record || {};
  const session = state.detail?.session || {};
  const key = state.detailAgent;
  const name = agentName(key);
  const persistedEvents = Array.isArray(record.events) ? record.events : [];
  const persistedEvent = persistedEvents.filter((item) => agentKeyFromName(item.source) === key).at(-1);
  const event = session.agent_events?.[key] || persistedEvent;
  const status = event?.status || session.status || record.status || 'waiting';
  const role = '独立提案者、交叉审核者与对等协作者';
  const coordinator = (session.executor || record.executor) === key ? '当前执行协调者' : '对等审核协作者';
  el.agentProfile.innerHTML = `<div class="agent-profile-card">
    ${avatarMarkup(key)}
    <h2>${escapeHtml(name)} <span class="status-dot status-${statusKey(status)}"></span></h2>
    <p>@${key}</p>
    <div class="agent-profile-fields">
      <div class="agent-profile-field"><span>职责</span><strong>${escapeHtml(role)}</strong></div>
      <div class="agent-profile-field"><span>当前角色</span><strong>${escapeHtml(coordinator)}</strong></div>
      <div class="agent-profile-field"><span>状态</span><strong>${escapeHtml(statusLabel(status))}</strong></div>
      <div class="agent-profile-field"><span>最近安全事件</span><strong>${escapeHtml(event?.safe_summary || '暂无实时事件')}</strong></div>
    </div>
  </div>`;
}

function renderPlanGate(session) {
  const waiting = session?.status === 'awaiting_plan' && Boolean(session.plan);
  el.planGate.classList.toggle('hidden', !waiting);
  if (!waiting) return;
  const revisions = session.plan.revision_count || 0;
  el.planGateNote.textContent = session.document
    ? `已导出：${session.document}`
    : `已人工修订 ${revisions} 次 · 可执行、整体修订、定向要求、导出或取消。`;
}

function issueStatusLabel(status) {
  const labels = {
    open: '待解决',
    resolved: '已解决',
    accepted: '已接受',
    rejected: '已拒绝',
    blocked: '受阻',
  };
  return labels[String(status || '').toLowerCase()] || String(status || '待解决');
}

function normalizeContent(text) {
  const value = String(text || '').trim();
  if (!value.startsWith('{')) return value;
  try {
    const data = JSON.parse(value);
    if (!data || typeof data !== 'object' || !data.verdict) return value;
    const lines = [`## 审核结论：${data.verdict === 'accept' ? '接受' : '要求修订'}`, ''];
    if (data.criteria && typeof data.criteria === 'object') {
      lines.push('| 审核维度 | 结果 |', '| --- | --- |');
      const labels = {
        requirements: '需求覆盖',
        architecture: '架构合理性',
        failure_paths: '失败与边界路径',
        compatibility: '兼容性',
        testing: '测试与验收',
      };
      Object.entries(data.criteria).forEach(([key, passed]) => {
        lines.push(`| ${labels[key] || key} | ${passed ? '通过' : '未通过'} |`);
      });
      lines.push('');
    }
    appendList(lines, '已达成事项', data.agreements);
    appendList(lines, '剩余分歧', data.remaining_disagreements);
    appendList(lines, '要求修订', data.required_revisions);
    if (Array.isArray(data.issues) && data.issues.length) {
      lines.push('### 争议');
      data.issues.forEach((issue) => {
        lines.push(`- **${issue.id || '争议'} [${issue.severity || ''}/${issueStatusLabel(issue.status)}]** ${issue.problem || ''}`);
        if (issue.resolution) lines.push(`  - 处理：${issue.resolution}`);
      });
    }
    return lines.join('\n');
  } catch {
    return value;
  }
}

function appendList(lines, title, values) {
  if (!Array.isArray(values) || !values.length) return;
  lines.push(`### ${title}`);
  values.forEach((value) => lines.push(`- ${value}`));
  lines.push('');
}

function renderMarkdown(source) {
  const codeBlocks = [];
  const text = String(source || '').replace(/```([^\n]*)\n([\s\S]*?)```/g, (_match, language, code) => {
    const token = `@@CODEBLOCK${codeBlocks.length}@@`;
    codeBlocks.push(`<pre><code data-language="${escapeHtml(language.trim())}">${escapeHtml(code.trimEnd())}</code></pre>`);
    return `\n${token}\n`;
  });
  const lines = text.split(/\r?\n/);
  const html = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const codeMatch = line.match(/^@@CODEBLOCK(\d+)@@$/);
    if (codeMatch) {
      html.push(codeBlocks[Number(codeMatch[1])] || '');
      index += 1;
      continue;
    }
    if (isTableStart(lines, index)) {
      const headers = splitTableRow(lines[index]);
      index += 2;
      const rows = [];
      while (index < lines.length && /^\s*\|?.+\|.+\|?\s*$/.test(lines[index]) && lines[index].trim()) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      html.push(`<div class="table-wrap"><table><thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${headers.map((_header, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    if (unordered) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${inlineMarkdown(item[1])}</li>`);
        index += 1;
      }
      html.push(`<ul>${items.join('')}</ul>`);
      continue;
    }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${inlineMarkdown(item[1])}</li>`);
        index += 1;
      }
      html.push(`<ol>${items.join('')}</ol>`);
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isSpecialLine(lines, index)) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    html.push(`<p>${inlineMarkdown(paragraph.join(' '))}</p>`);
  }
  return html.join('');
}

function inlineMarkdown(value) {
  const snippets = [];
  let text = String(value || '').replace(/`([^`]+)`/g, (_match, code) => {
    const token = `@@INLINE${snippets.length}@@`;
    snippets.push(`<code>${escapeHtml(code)}</code>`);
    return token;
  });
  text = escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    .replace(/(^|\s)(@[A-Za-z][\w-]*)/g, '$1<span class="mention">$2</span>');
  snippets.forEach((snippet, index) => {
    text = text.replace(`@@INLINE${index}@@`, snippet);
  });
  return text;
}

function isTableStart(lines, index) {
  return index + 1 < lines.length && lines[index].includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1]);
}

function splitTableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function isSpecialLine(lines, index) {
  const line = lines[index];
  return /^(#{1,4})\s+/.test(line) || /^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line) || /^@@CODEBLOCK\d+@@$/.test(line) || isTableStart(lines, index);
}

function setConnection(connected) {
  el.connectionDot.className = `status-dot ${connected ? 'status-complete' : 'status-running'}`;
  el.connectionDot.title = connected ? '本地事件流已连接' : '事件流正在重连';
}

function statusKey(status) {
  const value = String(status || '').toLowerCase();
  if (value.includes('complete') || value === 'ready' || value === 'done' || value === 'resolved' || value === 'approved') return 'complete';
  if (value.includes('fail') || value.includes('error') || value === 'open') return 'failed';
  if (value.includes('interrupt')) return 'interrupted';
  if (value.includes('cancel') || value === 'blocked') return 'cancelled';
  if (value.includes('await') || value.includes('review')) return 'awaiting_plan';
  if (value === 'stopping') return 'cancelled';
  if (value.includes('run') || value.includes('work') || value.includes('progress') || value === 'starting') return 'running';
  return 'waiting';
}

function statusLabel(status) {
  const labels = {
    starting: '正在启动',
    running: '协作中',
    ready: '等待消息',
    awaiting_plan: '等待方案确认',
    stopping: '正在停止',
    complete: '已完成',
    completed: '已完成',
    done: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
    waiting: '等待中',
    pending: '待处理',
    in_progress: '进行中',
    working: '进行中',
    reviewing: '审核中',
    review: '审核中',
    skipped: '已跳过',
    approved: '已通过',
    resolved: '已解决',
    rejected: '已拒绝',
    blocked: '受阻',
    unknown: '未知状态',
  };
  return labels[String(status || '').toLowerCase()] || '未知状态';
}

function normalizeRunSummary(run) {
  const detached = !run.live && ACTIVE_RUN_STATUSES.has(String(run.status || '').toLowerCase());
  if (!detached) return run;
  return {
    ...run,
    status: 'interrupted',
    detached: true,
    error: run.error || '任务已不在当前 UI 服务中运行',
  };
}

function agentName(agent) {
  const value = String(agent || '').toLowerCase();
  if (value.includes('claude')) return 'Claude Code';
  if (value.includes('codex')) return 'Codex';
  if (value === 'both') return 'Claude Code + Codex';
  if (value === 'user' || value === 'you') return '你';
  if (value === 'system') return '系统';
  if (value === 'bridge') return '桥接器';
  return String(agent || '—');
}

function eventSourceLabel(source) {
  const value = String(source || '').toLowerCase();
  if (!value || value === 'bridge') return '桥接器';
  if (value === 'system') return '系统';
  if (value === 'user') return '用户';
  return String(source);
}

function eventKindLabel(kind, fallback = '状态更新') {
  const labels = {
    phase: '阶段更新',
    progress: '进度更新',
    status: '状态更新',
    result: '结果更新',
    error: '发生错误',
    plan: '方案更新',
    review: '审核更新',
    tool: '工具活动',
  };
  return labels[String(kind || '').toLowerCase()] || fallback;
}

function stepLabel(step) {
  const value = String(step || '');
  const labels = {
    proposal: '独立方案',
    proposal_a: 'Claude Code 独立方案',
    proposal_b: 'Codex 独立方案',
    initial_planning: '初始规划',
    cross_review: '交叉审核',
    unified_proposal: '统一方案',
    consensus: '共识审核',
    implementation: '实施',
    verification: '验证',
    final_review: '最终审核',
    user_plan_revision: '用户要求修订方案',
  };
  if (labels[value]) return labels[value];
  const codeReview = value.match(/^code_review_(\d+)$/);
  if (codeReview) return `第 ${codeReview[1]} 轮代码审核`;
  const codeRevision = value.match(/^code_revision_(\d+)$/);
  if (codeRevision) return `第 ${codeRevision[1]} 轮代码修订`;
  const consensusRevision = value.match(/^consensus_revision_v(\d+)$/);
  if (consensusRevision) return `共识方案修订 v${consensusRevision[1]}`;
  const consensusReview = value.match(/^consensus_review_v(\d+)(?:_(.+))?$/);
  if (consensusReview) return `共识审核 v${consensusReview[1]}${consensusReview[2] ? ` · ${agentName(consensusReview[2])}` : ''}`;
  return value;
}

function agentKeyFromName(agent) {
  const value = String(agent || '').toLowerCase();
  if (value.includes('claude')) return 'claude';
  if (value.includes('codex')) return 'codex';
  return 'system';
}

function compactPath(value) {
  const path = String(value || '');
  const homeMatch = path.match(/^\/Users\/[^/]+(\/.*)?$/);
  return homeMatch ? `~${homeMatch[1] || ''}` : path || '工作区';
}

function runWorkspace(run) {
  return String(run?.workspace || state.health?.workspace || '');
}

function workspaceFolderName(value) {
  const normalized = String(value || '').replace(/[\\/]+$/, '');
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || '工作区';
}

function runMatchesSearch(run) {
  const searchable = [
    displayTaskTitle(run),
    workspaceFolderName(runWorkspace(run)),
    statusLabel(run.status),
    run.id,
  ].join(' ').toLocaleLowerCase('zh-CN');
  return searchable.includes(state.searchQuery);
}

function displayTaskTitle(run) {
  let title = String(run?.display_task || run?.task || '未命名任务');
  const prefixes = [run?.workspace]
    .filter(Boolean)
    .map((value) => String(value).replace(/[\\/]+$/, ''))
    .sort((left, right) => right.length - left.length);
  prefixes.forEach((prefix) => {
    title = title.replaceAll(`${prefix}/`, '');
    title = title.replaceAll(`${prefix}\\`, '');
  });
  title = title.replace(
    /\/Users\/[^/]+\/(?:[^/,，\n]+\/)+([^/,，\n]+)/g,
    (_match, basename) => basename.trim(),
  );
  return title.trim() || '未命名任务';
}

function relativeTime(value) {
  const stamp = Date.parse(value || '');
  if (!Number.isFinite(stamp)) return '刚刚';
  const seconds = Math.max(0, Math.round((Date.now() - stamp) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function formatEventTime(value) {
  const stamp = Date.parse(value || '');
  if (!Number.isFinite(stamp)) return '';
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(stamp);
}

function formatDuration(seconds) {
  const value = Number(seconds) || 0;
  if (value < 1) return '—';
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return `${minutes}分 ${rest}秒`;
}

function formatNumber(value) {
  return new Intl.NumberFormat('zh-CN').format(Number(value) || 0);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1_000_000) return `${(bytes / 1000).toFixed(bytes < 10_000 ? 1 : 0)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function showToast(message, error = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle('error', error);
  el.toast.classList.remove('hidden');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => el.toast.classList.add('hidden'), 3200);
}

function showFormError(target, message) {
  target.textContent = message;
  target.classList.remove('hidden');
}

function hideFormError(target) {
  target.textContent = '';
  target.classList.add('hidden');
}

function setButtonBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

void bootstrap();
