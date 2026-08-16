const state = {
  health: null,
  runs: [],
  currentId: null,
  detail: null,
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
  newTaskFiles: [],
  composerFiles: [],
  previewUrls: new WeakMap(),
  returnToNewTaskAfterSettings: false,
  draftTaskMode: null,
  workspaceBrowserPath: '',
  mentionStart: -1,
  mentionIndex: 0,
  openChangeSummaries: new Set(),
  openChangeFiles: new Set(),
  pendingChatMessages: new Map(),
  pendingChatSequence: 0,
  messageTexts: new Map(),
  feedPinnedToBottom: true,
  streamBuffers: new Map(),
  streamModelText: false,
  streamRefreshTimer: null,
  streamRefreshAt: 0,
  interfaceSavePromise: Promise.resolve(),
  modelCatalog: null,
  modelOrders: { claude: [], codex: [] },
  draggedModel: null,
  nativeInteractionId: null,
  nativeInteractionRunId: null,
  editingMessageId: null,
  unreadRuns: new Set(),
  notifiedEvents: new Set(),
  notificationsEnabled: false,
  draftSaveTimer: null,
  draftLoadedRunId: null,
};

const ACTIVE_RUN_STATUSES = new Set(['starting', 'running', 'awaiting_interaction', 'stopping']);
const DOCUMENT_EXTENSIONS = new Set(['csv', 'doc', 'docx', 'html', 'json', 'md', 'odt', 'pdf', 'ppt', 'pptx', 'rtf', 'txt', 'xls', 'xlsx', 'xml', 'yaml', 'yml']);
// Raster images only; svg is excluded server-side as a stored-XSS vector.
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);
const UPLOAD_EXTENSIONS = new Set([...DOCUMENT_EXTENSIONS, ...IMAGE_EXTENSIONS]);
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
  search: document.querySelector('#search-button'),
  searchPanel: document.querySelector('#run-search-panel'),
  searchInput: document.querySelector('#run-search-input'),
  closeSearch: document.querySelector('#close-search-button'),
  emptyState: document.querySelector('#empty-state'),
  runView: document.querySelector('#run-view'),
  chatView: document.querySelector('#chat-view'),
  workspaceChip: document.querySelector('#workspace-chip'),
  sidebarWorkspace: document.querySelector('#sidebar-workspace'),
  connectionDot: document.querySelector('#connection-dot'),
  stopTaskButton: document.querySelector('#stop-task-button'),
  claudeStatus: document.querySelector('#claude-status'),
  codexStatus: document.querySelector('#codex-status'),
  claudeNavDot: document.querySelector('#claude-nav-dot'),
  codexNavDot: document.querySelector('#codex-nav-dot'),
  statusBadge: document.querySelector('#run-status-badge'),
  errorBanner: document.querySelector('#error-banner'),
  artifactFeed: document.querySelector('#artifact-feed'),
  feedJump: document.querySelector('#feed-jump-button'),
  overview: document.querySelector('#run-overview'),
  runTimeline: document.querySelector('#run-timeline'),
  runTimelineCount: document.querySelector('#run-timeline-count'),
  eventTimeline: document.querySelector('#event-timeline'),
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
  composerFileInput: document.querySelector('#composer-file-input'),
  composerAttachmentList: document.querySelector('#composer-attachment-list'),
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
  nativeInteractionDialog: document.querySelector('#native-interaction-dialog'),
  nativeInteractionForm: document.querySelector('#native-interaction-form'),
  nativeInteractionSource: document.querySelector('#native-interaction-source'),
  nativeInteractionTitle: document.querySelector('#native-interaction-title'),
  nativeInteractionQueue: document.querySelector('#native-interaction-queue'),
  nativeInteractionMessage: document.querySelector('#native-interaction-message'),
  nativeInteractionCommand: document.querySelector('#native-interaction-command'),
  nativeInteractionCwd: document.querySelector('#native-interaction-cwd'),
  nativeInteractionQuestions: document.querySelector('#native-interaction-questions'),
  nativeInteractionError: document.querySelector('#native-interaction-error'),
  nativeInteractionActions: document.querySelector('#native-interaction-actions'),
  nativeInteractionClose: document.querySelector('#native-interaction-close'),
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
    loadUnreadRuns(state.health.workspace);
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
  [el.newTask, el.emptyNewTask].forEach((button) => {
    button.addEventListener('click', openNewTask);
  });
  el.refresh.addEventListener('click', () => void refreshAll());
  el.archivedToggle.addEventListener('click', toggleArchivedRuns);
  el.stopTaskButton.addEventListener('click', () => void stopCurrentTask());
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
  document.querySelector('#open-details-button').addEventListener('click', () => openDetails('overview'));
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
    const detail = event.target.closest('[data-open-detail]');
    if (detail) {
      openDetails(detail.dataset.openDetail);
      return;
    }
    const codeCopy = event.target.closest('[data-code-copy]');
    if (codeCopy) {
      const code = codeCopy.closest('.code-block')?.querySelector('code');
      void copyText(code?.textContent || '', '代码');
      return;
    }
    const copy = event.target.closest('[data-message-copy]');
    if (copy) {
      void copyText(state.messageTexts.get(copy.dataset.messageCopy) || '', '消息原文');
      return;
    }
    const quote = event.target.closest('[data-message-quote]');
    if (quote) {
      quoteMessage(quote.dataset.messageQuote);
      return;
    }
    const edit = event.target.closest('[data-message-edit]');
    if (edit) {
      editMessage(edit.dataset.messageEdit);
      return;
    }
    const retry = event.target.closest('[data-message-retry]');
    if (retry) void retryMessage(retry.dataset.messageRetry, retry.dataset.retryMode || 'regenerate');
  });
  el.artifactFeed.addEventListener('toggle', handleChangeToggle, true);
  el.artifactFeed.addEventListener('scroll', () => {
    state.feedPinnedToBottom = isFeedNearBottom();
    updateFeedJumpButton();
  }, { passive: true });
  el.feedJump.addEventListener('click', () => {
    state.feedPinnedToBottom = true;
    scrollChatToBottom();
    updateFeedJumpButton();
  });

  el.messageForm.addEventListener('submit', (event) => {
    event.preventDefault();
    void submitQuickTask();
  });
  el.quickTaskInput.addEventListener('input', updateMentionMenu);
  el.quickTaskInput.addEventListener('input', resizeComposer);
  el.quickTaskInput.addEventListener('input', scheduleDraftSave);
  el.quickTaskInput.addEventListener('keydown', handleComposerKeydown);
  // Pasted screenshots arrive as File objects with empty/generic names;
  // synthesize a stable name so the upload passes extension validation.
  el.quickTaskInput.addEventListener('paste', handleComposerPaste);
  el.taskInput.addEventListener('paste', handleComposerPaste);
  el.taskInput.addEventListener('input', scheduleNewTaskDraftSave);
  el.quickTaskInput.addEventListener('blur', () => {
    window.setTimeout(hideMentionMenu, 100);
  });
  el.mentionMenu.addEventListener('pointerdown', (event) => {
    const option = event.target.closest('[data-mention]');
    if (!option) return;
    event.preventDefault();
    insertMention(option.dataset.mention);
  });
  el.quickAttach.addEventListener('click', () => el.composerFileInput.click());
  el.composerFileInput.addEventListener('change', () => {
    addTaskFiles(el.composerFileInput.files, 'composer');
    el.composerFileInput.value = '';
  });

  el.documentInput.addEventListener('change', () => {
    addTaskFiles(el.documentInput.files, 'task');
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
  el.documentDropZone.addEventListener('drop', (event) => addTaskFiles(event.dataTransfer?.files, 'task'));
  el.documentList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-document]');
    if (!button) return;
    const [removed] = state.newTaskFiles.splice(Number(button.dataset.removeDocument), 1);
    releasePreviewUrl(removed);
    renderTaskFiles();
    hideFormError(el.formError);
  });
  el.composerAttachmentList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-composer-document]');
    if (!button) return;
    const [removed] = state.composerFiles.splice(Number(button.dataset.removeComposerDocument), 1);
    releasePreviewUrl(removed);
    renderTaskFiles();
  });
  ['dragenter', 'dragover'].forEach((name) => {
    el.messageForm.addEventListener(name, (event) => {
      if (!event.dataTransfer?.types?.includes('Files')) return;
      event.preventDefault();
      el.messageForm.classList.add('composer-dragging');
    });
  });
  ['dragleave', 'drop'].forEach((name) => {
    el.messageForm.addEventListener(name, (event) => {
      if (name === 'drop') event.preventDefault();
      el.messageForm.classList.remove('composer-dragging');
    });
  });
  el.messageForm.addEventListener('drop', (event) => addTaskFiles(event.dataTransfer?.files, 'composer'));

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
  document.querySelector('#settings-stream-model-text').addEventListener('change', (event) => {
    void saveInterfacePreferences({ stream_model_text: event.currentTarget.checked });
  });
  document.querySelector('#settings-browser-notifications').addEventListener('change', (event) => {
    void saveInterfacePreferences({ browser_notifications: event.currentTarget.checked });
  });
  document.querySelectorAll('input[name="task-mode"]').forEach((input) => {
    input.addEventListener('change', updateNewTaskMode);
  });

  el.nativeInteractionForm.addEventListener('submit', (event) => event.preventDefault());
  el.nativeInteractionDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    void declineNativeInteraction();
  });
  el.nativeInteractionClose.addEventListener('click', () => void declineNativeInteraction());
  el.nativeInteractionActions.addEventListener('click', (event) => {
    const button = event.target.closest('[data-native-action]');
    if (!button) return;
    void submitNativeInteraction(button.dataset.nativeAction, button);
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
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
      event.preventDefault();
      openNewTask();
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
    } else if (state.composerFiles.length && !el.quickTaskSubmit.disabled) {
      // Screenshot + Enter with no text still sends; submitQuickTask fills the prompt.
      el.messageForm.requestSubmit(el.quickTaskSubmit);
    }
  }
}

function handleComposerPaste(event) {
  const items = Array.from(event.clipboardData?.items || []);
  const files = items
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter(Boolean)
    .map((file) => {
      if (file.name && file.name.includes('.')) return file;
      const extension = (file.type.split('/')[1] || 'png').toLowerCase().replace(/[^a-z0-9]/g, '') || 'png';
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      return new File([file], `paste-${stamp}.${extension}`, { type: file.type || 'image/png' });
    });
  if (!files.length) return;
  event.preventDefault();
  const target = event.currentTarget === el.taskInput ? 'task' : 'composer';
  addTaskFiles(files, target);
  showToast(`已粘贴 ${files.length} 个图片附件。`);
}

function resizeComposer() {
  el.quickTaskInput.style.height = 'auto';
  el.quickTaskInput.style.height = `${Math.min(el.quickTaskInput.scrollHeight, 180)}px`;
}

function updateMentionMenu() {
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
    const element = document.querySelector(`#${id}`);
    if (element) element.value = value ?? '';
  };
  const setChecked = (id, value) => {
    const element = document.querySelector(`#${id}`);
    if (element) element.checked = Boolean(value);
  };
  setValue('settings-workspace', settings.workspace);
  setValue('settings-group-chat-default-agent', values.group_chat_default_agent || 'both');
  setChecked('settings-group-chat-execution', values.group_chat_execution !== false);
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
  setValue('settings-group-chat-agent-a-identity', values.group_chat_identities?.agent_a || '');
  setValue('settings-group-chat-agent-b-identity', values.group_chat_identities?.agent_b || '');
  const theme = normalizeTheme(values.ui?.theme);
  const themeInput = document.querySelector(`input[name="settings-theme"][value="${theme}"]`);
  if (themeInput) themeInput.checked = true;
  setChecked('settings-show-archived', values.ui?.show_archived);
  setChecked('settings-compact-sidebar', values.ui?.compact_sidebar);
  setChecked('settings-stream-model-text', values.ui?.stream_model_text);
  setChecked('settings-browser-notifications', values.ui?.browser_notifications);
  state.notificationsEnabled = values.ui?.browser_notifications === true;
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
  const requested = ['general', 'agents', 'interface'].includes(tab) ? tab : 'general';
  const requestedPanel = document.querySelector(`[data-settings-panel="${requested}"]`);
  const selected = requestedPanel ? requested : 'general';
  document.querySelectorAll('[data-settings-tab]').forEach((button) => {
    button.classList.toggle('active', button.dataset.settingsTab === selected);
  });
  document.querySelectorAll('[data-settings-panel]').forEach((panel) => {
    const applies = !panel.dataset.collaborationOnly || panel.dataset.collaborationOnly === 'group_chat';
    panel.classList.toggle('hidden', panel.dataset.settingsPanel !== selected || !applies);
  });
}

function updateSettingsDependencies() {
  document.querySelectorAll('[data-collaboration-only]').forEach((item) => {
    if (item.dataset.settingsPanel) return;
    item.classList.toggle('hidden', item.dataset.collaborationOnly !== 'group_chat');
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
  if (Object.hasOwn(values, 'stream_model_text')) preferences.stream_model_text = Boolean(values.stream_model_text);
  if (Object.hasOwn(values, 'browser_notifications')) preferences.browser_notifications = Boolean(values.browser_notifications);
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
  const positive = (id, label) => {
    const value = Number(get(id).value);
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${label}必须是正数。`);
    return value;
  };
  const agent = (name) => ({
    command: parseCommandSetting(get(`settings-${name}-command`).value, name),
    model: state.modelOrders[name][0] || '',
    models: [...state.modelOrders[name]],
    fallback_on_timeout: get(`settings-${name}-fallback`).checked,
    timeout: positive(`settings-${name}-timeout`, `${agentName(name)} 超时`),
    extra_args: get(`settings-${name}-extra-args`).value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
  });
  return {
    group_chat_default_agent: get('settings-group-chat-default-agent').value,
    group_chat_execution: get('settings-group-chat-execution').checked,
    group_chat_identities: {
      agent_a: get('settings-group-chat-agent-a-identity').value.trim(),
      agent_b: get('settings-group-chat-agent-b-identity').value.trim(),
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
      stream_model_text: get('settings-stream-model-text').checked,
      browser_notifications: get('settings-browser-notifications').checked,
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
  if (!partial || Object.hasOwn(ui, 'stream_model_text')) {
    state.streamModelText = Boolean(ui.stream_model_text);
    // Turning streaming off should drop any half-rendered preview immediately.
    if (!state.streamModelText && state.streamBuffers.size) {
      state.streamBuffers.clear();
      if (state.currentId) void loadDetail(state.currentId);
    }
  }
  if (!partial || Object.hasOwn(ui, 'browser_notifications')) {
    state.notificationsEnabled = Boolean(ui.browser_notifications);
    if (state.notificationsEnabled && 'Notification' in window && Notification.permission === 'default') {
      void Notification.requestPermission();
    }
  }
}

function currentInterfaceSettings() {
  return {
    theme: normalizeTheme(document.body.dataset.theme),
    show_archived: state.showArchived,
    compact_sidebar: document.body.classList.contains('compact-sidebar'),
    stream_model_text: Boolean(state.streamModelText),
    browser_notifications: Boolean(state.notificationsEnabled),
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
  el.taskDefaultsSummary.textContent = `${workspace} · Claude Code + Codex · 群聊协作 · 可定向执行`;
}

function updateNewTaskMode() {
  el.taskInput.required = false;
  el.taskInputLabel.textContent = '第一条消息（可选）';
  el.taskInput.placeholder = '可以留空，创建群聊后再从底部发送第一条消息…';
  el.taskSubmit.textContent = '创建群聊';
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
  state.mainView = 'chat';
  el.chatView.classList.toggle('hidden', state.mainView !== 'chat');
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
  state.detailView = ['overview', 'agent'].includes(view) ? view : 'overview';
  el.detailOverview.classList.toggle('hidden', state.detailView !== 'overview');
  el.detailAgentPanel.classList.toggle('hidden', state.detailView !== 'agent');
  document.querySelectorAll('[data-detail-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.detailView === state.detailView);
  });
  const labels = {
    overview: ['运行详情', '状态、耗时与活动记录'],
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
  state.draftTaskMode = null;
  updateNewTaskMode();
  renderTaskFiles();
  restoreNewTaskDraft();
  el.newTaskDialog.showModal();
  window.setTimeout(() => {
    (selectedTaskMode() === 'group_chat' ? el.taskSubmit : el.taskInput).focus();
  }, 0);
}

function addTaskFiles(fileList, target = 'task') {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return;
  const files = target === 'composer' ? state.composerFiles : state.newTaskFiles;
  const errors = [];
  incoming.forEach((file) => {
    const extension = file.name.includes('.') ? file.name.split('.').at(-1).toLowerCase() : '';
    if (!UPLOAD_EXTENSIONS.has(extension)) {
      errors.push(`${file.name} 不是支持的文档或图片格式`);
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
    const duplicate = files.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified);
    if (duplicate) return;
    if (files.length >= MAX_DOCUMENT_FILES) {
      errors.push(`每条消息最多添加 ${MAX_DOCUMENT_FILES} 个附件`);
      return;
    }
    const total = files.reduce((sum, item) => sum + item.size, 0) + file.size;
    if (total > MAX_DOCUMENT_TOTAL_BYTES) {
      errors.push('文档合计大小不能超过 20 MB');
      return;
    }
    files.push(file);
  });
  renderTaskFiles();
  if (target === 'task') {
    if (errors.length) showFormError(el.formError, errors[0]);
    else hideFormError(el.formError);
  } else if (errors.length) {
    showToast(errors[0], true);
  }
}

function isImageFile(file) {
  if (!file) return false;
  if (typeof file.type === 'string' && file.type.toLowerCase().startsWith('image/')) return true;
  const name = String(file.name || '').toLowerCase();
  const extension = name.includes('.') ? name.split('.').at(-1) : '';
  return IMAGE_EXTENSIONS.has(extension);
}

function previewUrlFor(file) {
  if (!file || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return '';
  let url = state.previewUrls.get(file);
  if (!url) {
    url = URL.createObjectURL(file);
    state.previewUrls.set(file, url);
  }
  return url;
}

function releasePreviewUrl(file) {
  if (!file || typeof URL === 'undefined' || typeof URL.revokeObjectURL !== 'function') return;
  const url = state.previewUrls.get(file);
  if (!url) return;
  URL.revokeObjectURL(url);
  state.previewUrls.delete(file);
}

function appendImageThumbnail(parent, file, className, alt) {
  const preview = document.createElement('img');
  preview.className = className;
  preview.alt = alt || file.name || '图片附件';
  preview.src = previewUrlFor(file);
  preview.addEventListener('error', () => {
    preview.remove();
    releasePreviewUrl(file);
    if (parent.classList.contains('selected-document-icon')) parent.textContent = '▧';
    parent.classList.add('image-preview-failed');
  }, { once: true });
  parent.append(preview);
  return preview;
}

function renderTaskFiles() {
  el.documentList.replaceChildren();
  el.documentList.classList.toggle('hidden', !state.newTaskFiles.length);
  state.newTaskFiles.forEach((file, index) => {
    const row = document.createElement('div');
    row.className = 'selected-document';
    const icon = document.createElement('span');
    icon.className = 'selected-document-icon';
    if (isImageFile(file)) appendImageThumbnail(icon, file, 'selected-document-thumb', file.name);
    else icon.textContent = '▧';
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
  el.composerAttachmentList.replaceChildren();
  el.composerAttachmentList.classList.toggle('hidden', !state.composerFiles.length);
  state.composerFiles.forEach((file, index) => {
    const chip = document.createElement('span');
    chip.className = 'composer-attachment';
    const image = isImageFile(file);
    if (image) {
      chip.classList.add('is-image');
      chip.title = `${file.name} · ${formatBytes(file.size)}`;
      appendImageThumbnail(chip, file, 'composer-attachment-thumb', file.name);
    }
    if (!image) {
      const label = document.createElement('strong');
      label.textContent = file.name;
      label.title = `${file.name} · ${formatBytes(file.size)}`;
      chip.append(label);
    }
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.dataset.removeComposerDocument = String(index);
    remove.setAttribute('aria-label', `移除 ${file.name}`);
    remove.title = '移除附件';
    remove.textContent = '×';
    chip.append(label, remove);
    el.composerAttachmentList.append(chip);
  });
}

async function encodeTaskFiles(target = 'task') {
  const files = target === 'composer' ? state.composerFiles : state.newTaskFiles;
  return Promise.all(files.map(async (file) => ({
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

function clearTaskFiles(target = 'all') {
  if (target === 'all' || target === 'task') {
    state.newTaskFiles.forEach(releasePreviewUrl);
    state.newTaskFiles = [];
    el.documentInput.value = '';
  }
  if (target === 'all' || target === 'composer') {
    state.composerFiles.forEach(releasePreviewUrl);
    state.composerFiles = [];
    el.composerFileInput.value = '';
  }
  renderTaskFiles();
}

async function submitTask() {
  let task = el.taskInput.value.trim();
  if (!task && state.newTaskFiles.length) task = '请查看并分析附件。';
  setButtonBusy(el.taskSubmit, true, '正在启动…');
  hideFormError(el.formError);
  try {
    const attachments = await encodeTaskFiles('task');
    await startTask({
      task,
      attachments,
      ...taskSettingsPayload(),
    });
    el.newTaskDialog.close();
    el.taskInput.value = '';
    try { localStorage.removeItem(`${NEW_TASK_DRAFT_KEY}:${state.health?.workspace || 'default'}`); } catch {}
    el.quickTaskInput.value = '';
    clearTaskFiles('task');
  } catch (error) {
    showFormError(el.formError, error.message);
  } finally {
    setButtonBusy(el.taskSubmit, false, '创建群聊');
  }
}

async function submitQuickTask() {
  let task = el.quickTaskInput.value.trim();
  if (!task && !state.composerFiles.length) {
    el.quickTaskInput.focus();
    return;
  }
  if (!task && state.composerFiles.length) task = '请查看并分析附件。';
  const inGroupChat = Boolean(state.currentId);
  const editedMessage = state.editingMessageId ? findGroupChatMessage(state.editingMessageId) : null;
  const options = editedMessage
    ? {
      edited_from: editedMessage.id,
      recipients: Array.isArray(editedMessage.recipients)
        ? editedMessage.recipients.filter((recipient) => recipient !== 'user')
        : [],
    }
    : {};
  setButtonBusy(el.quickTaskSubmit, true, '正在发送…');
  try {
    const attachments = await encodeTaskFiles('composer');
    if (inGroupChat) {
      await sendGroupChatMessage(task, attachments, options);
    } else {
      await startTask({
        task,
        attachments,
        ...taskSettingsPayload(),
      });
    }
    el.quickTaskInput.value = '';
    state.editingMessageId = null;
    resizeComposer();
    clearTaskFiles('composer');
    clearDraft(state.currentId);
    hideMentionMenu();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(el.quickTaskSubmit, false, inGroupChat ? '发送消息' : '发送');
  }
}

function selectedTaskMode() {
  return 'group_chat';
}

function taskSettingsPayload() {
  const values = state.settings?.values || {};
  return {
    workspace: state.settings?.workspace || state.health?.workspace || '',
    config: state.settings?.source_path || '',
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
  showToast(hasInitialMessage
    ? '群聊已创建，正在等待 Agent 回复。'
    : '群聊已创建，可以发送第一条消息。');
  await refreshAll();
}

async function sendGroupChatMessage(message, attachments = [], options = {}) {
  if (!state.currentId) throw new Error('没有选中的群聊对话。');
  const runId = state.currentId;
  const pending = queuePendingChatMessage(runId, message, attachments, options);
  renderDetail();
  scrollChatToBottom();
  try {
    await api(`/api/sessions/${encodeURIComponent(runId)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message, attachments, ...options }),
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

function queuePendingChatMessage(runId, content, attachments = [], options = {}) {
  const chat = state.detail?.session?.group_chat || state.detail?.record?.group_chat || {};
  const serverMessageCount = Array.isArray(chat.messages) ? chat.messages.length : 0;
  const pending = {
    client_id: `pending-${Date.now()}-${state.pendingChatSequence += 1}`,
    sender: 'user',
    role: 'user',
    content,
    attachments: attachments.map((item) => ({
      name: item.name,
      size: item.size,
      pending: true,
    })),
    recipients: [],
    created_at: new Date().toISOString(),
    action: 'discuss',
    optimistic: true,
    delivery_status: 'sending',
    server_message_count: serverMessageCount,
    expected_recipients: optimisticChatRecipients(content),
    server_user_id: '',
    hidden: Boolean(options.retry_of),
    retry_of: options.retry_of || '',
    retry_mode: options.retry_mode || '',
  };
  if (options.retry_of) {
    pending.server_user_id = options.reply_to || options.retry_of;
    pending.expected_recipients = options.agent ? [options.agent] : optimisticChatRecipients(content);
  }
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
        && (!optimistic.retry_of || message.retry_of === optimistic.retry_of)
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

function groupChatMessageKey(message) {
  return String(message?.id || message?.client_id || '');
}

// Keep each Agent reply beside the user message that caused it. The server
// appends concurrent replies as they finish, while optimistic loading cards
// are created locally; rendering the two lists separately therefore makes a
// later question appear before the earlier reply. Parent-aware ordering keeps
// user messages in send order and inserts all replies immediately after their
// parent, regardless of completion order.
function orderGroupChatMessages(serverMessages, pendingUsers, pendingReplies) {
  const source = [
    ...serverMessages,
    ...pendingUsers,
    ...pendingReplies,
  ].map((message, index) => ({ message, index }));
  const itemsByKey = new Map();
  source.forEach((item) => {
    const key = groupChatMessageKey(item.message);
    if (key) itemsByKey.set(key, item);
  });
  const repliesByParent = new Map();
  source.forEach((item) => {
    const parent = String(item.message.reply_to || '');
    if (!parent || !itemsByKey.has(parent)) return;
    const replies = repliesByParent.get(parent) || [];
    replies.push(item);
    repliesByParent.set(parent, replies);
  });
  const emitted = new Set();
  const ordered = [];
  const emit = (item) => {
    if (!item || emitted.has(item.index)) return;
    emitted.add(item.index);
    ordered.push(item.message);
  };
  source.forEach((item) => {
    if (emitted.has(item.index)) return;
    const parent = String(item.message.reply_to || '');
    if (parent && itemsByKey.has(parent)) return;
    emit(item);
    const key = groupChatMessageKey(item.message);
    (repliesByParent.get(key) || []).forEach(emit);
  });
  source.forEach(emit);
  return ordered;
}

function scrollChatToBottom() {
  window.requestAnimationFrame(() => {
    el.artifactFeed.scrollTop = el.artifactFeed.scrollHeight;
    state.feedPinnedToBottom = true;
    updateFeedJumpButton();
  });
}

// Pull a message into the composer as a markdown quote so follow-up questions
// carry the context the agents need without retyping it.
function quoteMessage(feedKey) {
  const text = state.messageTexts.get(feedKey) || '';
  if (!text) {
    showToast('这条消息没有可引用的内容。', true);
    return;
  }
  const lines = text.trim().split(/\r?\n/).slice(0, 12);
  const truncated = text.trim().split(/\r?\n/).length > lines.length;
  const quoted = lines.map((line) => `> ${line}`).join('\n');
  const existing = el.quickTaskInput.value.trimEnd();
  const block = `${quoted}${truncated ? '\n> …' : ''}\n\n`;
  el.quickTaskInput.value = existing ? `${existing}\n\n${block}` : block;
  el.quickTaskInput.focus();
  el.quickTaskInput.setSelectionRange(
    el.quickTaskInput.value.length,
    el.quickTaskInput.value.length,
  );
}

function findGroupChatMessage(messageId) {
  const chat = state.detail?.session?.group_chat || state.detail?.record?.group_chat || {};
  return (Array.isArray(chat.messages) ? chat.messages : []).find(
    (message) => String(message.id || '') === String(messageId),
  ) || null;
}

function editMessage(messageId) {
  const message = findGroupChatMessage(messageId);
  if (!message || message.sender !== 'user') return;
  state.editingMessageId = message.id;
  el.quickTaskInput.value = message.content || '';
  resizeComposer();
  el.quickTaskInput.focus();
  el.quickTaskInput.setSelectionRange(el.quickTaskInput.value.length, el.quickTaskInput.value.length);
  showToast('已载入消息编辑内容；发送后会创建新的尝试，原消息仍会保留。');
}

async function retryMessage(messageId, retryMode = 'regenerate') {
  const message = findGroupChatMessage(messageId);
  if (!message || message.role !== 'assistant' || !state.currentId) return;
  const parent = findGroupChatMessage(message.reply_to);
  if (!parent) {
    showToast('找不到这条回复对应的用户问题。', true);
    return;
  }
  const options = {
    retry_of: message.id,
    retry_mode: retryMode,
    agent: message.sender,
    reply_to: parent.id,
  };
  const pending = queuePendingChatMessage(state.currentId, parent.content, [], options);
  renderDetail();
  scrollChatToBottom();
  try {
    await api(`/api/sessions/${encodeURIComponent(state.currentId)}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message: parent.content, attachments: [], ...options }),
    });
    pending.delivery_status = 'accepted';
    renderDetail();
    showToast(retryMode === 'continue' ? '已要求 Agent 继续回复。' : '已要求 Agent 重新生成回复。');
    await refreshAll();
  } catch (error) {
    removePendingChatMessage(state.currentId, pending.client_id);
    renderDetail();
    showToast(error.message, true);
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
      if (update.type === 'native_interaction' && update.run_id) {
        const isCurrent = !state.currentId || state.currentId === update.run_id;
        if (isCurrent) {
          state.currentId = update.run_id;
          state.detail = null;
          state.mainView = 'chat';
          showToast('Agent 正在等待你的权限决定或补充信息。');
          scheduleRefresh(0);
        } else {
          selectRun(update.run_id);
          showToast('已切换到需要权限决定或补充信息的任务。');
        }
        notifyBrowser('Agent 需要你的操作', '请在页面中处理权限或补充问题。', update.run_id);
        markRunUnread(update.run_id, 'Agent 需要你的操作', '请在页面中处理权限或补充问题。');
        return;
      }
      if (update.type === 'event' && update.stream_text) {
        handleStreamUpdate(update);
        // Appending text locally avoids a refetch per token, but status, plan
        // gate and timeline still come from the detail endpoint.
        scheduleStreamRefresh();
        return;
      }
      if (update.type === 'chat_message' || update.type === 'finished') {
        clearStreamBuffers(update.run_id);
        if (update.run_id && update.run_id !== state.currentId) {
          const title = update.type === 'finished' ? '任务已完成' : '收到新的 Agent 回复';
          markRunUnread(update.run_id, title, 'MultiAgent 有新的协作状态更新。');
        }
        if (update.type === 'finished' && (update.run_id === state.currentId || document.hidden)) {
          notifyBrowser('任务状态更新', '当前任务已完成或停止。', update.run_id);
        }
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

// Throttle rather than debounce: a steady token stream would keep resetting a
// debounce timer and starve the refresh entirely, freezing status and timeline.
function scheduleStreamRefresh(interval = 1500) {
  if (state.streamRefreshTimer) return;
  const elapsed = Date.now() - state.streamRefreshAt;
  const wait = elapsed >= interval ? 0 : interval - elapsed;
  state.streamRefreshTimer = window.setTimeout(() => {
    state.streamRefreshTimer = null;
    state.streamRefreshAt = Date.now();
    void refreshAll();
  }, wait);
}

// Stream updates carry raw model text when the interface toggle is on. Buffer
// them per (run, agent) and append into the matching loading card without a
// full refresh; the real message still lands via the normal chat_message path.
function handleStreamUpdate(update) {
  if (!state.streamModelText) return;
  const runId = String(update.run_id || '');
  if (!runId || runId !== String(state.currentId || '')) return;
  const agent = agentKeyFromName(update.source) || update.source;
  const step = String(update.step_id || agent).replace(/[^A-Za-z0-9_-]/g, '-');
  const key = `${runId}:${agent}:${step}`;
  const previous = state.streamBuffers.get(key) || '';
  state.streamBuffers.set(key, previous + String(update.stream_text));
  appendStreamToFeed(runId);
}

function clearStreamBuffers(runId) {
  if (!runId) return;
  const prefix = `${runId}:`;
  Array.from(state.streamBuffers.keys()).forEach((key) => {
    if (key.startsWith(prefix)) state.streamBuffers.delete(key);
  });
}

function appendStreamToFeed(runId) {
  if (String(feedCacheRunId) !== String(runId)) return;
  const latest = new Map();
  state.streamBuffers.forEach((text, key) => {
    const [run, agent, step] = key.split(':');
    if (run !== String(runId) || !text) return;
    latest.set(`${agent}:${step || agent}`, { agent, text });
  });
  latest.forEach(({ agent, text }, streamKey) => {
    const feedKey = `stream-${runId}-${streamKey.replace(':', '-')}`;
    const existing = el.artifactFeed.querySelector(`[data-feed-key="${feedKey}"]`);
    const html = streamCardMarkup(feedKey, agent, text);
    if (existing) {
      const replacement = nodeFromMarkup(html);
      if (replacement) existing.replaceWith(replacement);
    } else {
      const node = nodeFromMarkup(html);
      if (node) el.artifactFeed.appendChild(node);
    }
    feedHtmlCache.set(feedKey, html);
  });
  if (state.feedPinnedToBottom) scrollChatToBottom();
}

function streamCardMarkup(feedKey, agent, text) {
  const name = agentName(agent);
  return `<article class="message-row message-${escapeHtml(agent)} message-loading message-streaming" data-feed-key="${escapeHtml(feedKey)}">
    ${avatarMarkup(agent)}
    <div class="message-main">
      <div class="message-header">
        <strong>${escapeHtml(name)}</strong>
        <span class="message-role">正在生成 · 流式预览</span>
        <span class="message-time"></span>
      </div>
      <span class="message-tag">实时</span>
      <div class="markdown-body">${renderMarkdown(normalizeContent(text))}</div>
    </div>
  </article>`;
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
    clearRunUnread(runId);
    state.detail = detail;
    restoreDraft(runId);
    renderDetail();
  } catch (error) {
    if (state.currentId === runId) showToast(error.message, true);
  }
}

function selectRun(runId) {
  closeRunContextMenu();
  saveDraftNow(state.currentId);
  el.quickTaskInput.value = '';
  clearTaskFiles('composer');
  resizeComposer();
  state.currentId = runId;
  state.draftLoadedRunId = null;
  clearRunUnread(runId);
  state.detail = null;
  state.mainView = 'chat';
  renderRunList();
  void loadDetail(runId);
}

const DRAFT_STORAGE_KEY = 'multiagent.composer-drafts.v1';

function draftStorageKey(runId = state.currentId) {
  const workspace = state.health?.workspace || 'default';
  return `${DRAFT_STORAGE_KEY}:${workspace}:${runId || 'new'}`;
}

function saveDraftNow(runId = state.currentId) {
  if (typeof localStorage === 'undefined') return;
  const value = el.quickTaskInput?.value || '';
  if (!value.trim() && !state.composerFiles.length && !runId) return;
  const key = draftStorageKey(runId);
  const payload = { text: value, saved_at: new Date().toISOString() };
  try {
    if (value || state.composerFiles.length) localStorage.setItem(key, JSON.stringify(payload));
    else localStorage.removeItem(key);
  } catch {
    // Drafts are a convenience feature; quota errors must not block sending.
  }
}

function clearDraft(runId = state.currentId) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.removeItem(draftStorageKey(runId));
  } catch {
    // Ignore storage failures; this is only a convenience cache.
  }
}

const NEW_TASK_DRAFT_KEY = 'multiagent.new-task-draft.v1';

function scheduleNewTaskDraftSave() {
  window.clearTimeout(state.draftSaveTimer);
  state.draftSaveTimer = window.setTimeout(() => {
    try {
      const text = el.taskInput.value || '';
      if (text) localStorage.setItem(`${NEW_TASK_DRAFT_KEY}:${state.health?.workspace || 'default'}`, text);
      else localStorage.removeItem(`${NEW_TASK_DRAFT_KEY}:${state.health?.workspace || 'default'}`);
    } catch {
      // Ignore local storage failures.
    }
  }, 250);
}

function restoreNewTaskDraft() {
  try {
    if (el.taskInput.value.trim()) return;
    const text = localStorage.getItem(`${NEW_TASK_DRAFT_KEY}:${state.health?.workspace || 'default'}`) || '';
    if (text) el.taskInput.value = text;
  } catch {
    // Ignore local storage failures.
  }
}

function scheduleDraftSave() {
  window.clearTimeout(state.draftSaveTimer);
  state.draftSaveTimer = window.setTimeout(() => saveDraftNow(), 250);
}

function restoreDraft(runId = state.currentId) {
  if (state.draftLoadedRunId === String(runId || 'new')) return;
  state.draftLoadedRunId = String(runId || 'new');
  try {
    const draft = JSON.parse(localStorage.getItem(draftStorageKey(runId)) || 'null');
    if (draft && typeof draft.text === 'string' && !el.quickTaskInput.value.trim()) {
      el.quickTaskInput.value = draft.text;
      resizeComposer();
    }
  } catch {
    // Ignore malformed or unavailable local drafts.
  }
}

const UNREAD_STORAGE_KEY = 'multiagent.unread-runs.v1';

function loadUnreadRuns(workspace) {
  try {
    const raw = JSON.parse(localStorage.getItem(`${UNREAD_STORAGE_KEY}:${workspace}`) || '[]');
    state.unreadRuns = new Set(Array.isArray(raw) ? raw.filter((id) => typeof id === 'string') : []);
  } catch {
    state.unreadRuns = new Set();
  }
}

function persistUnreadRuns() {
  const workspace = state.health?.workspace || '';
  if (!workspace) return;
  localStorage.setItem(`${UNREAD_STORAGE_KEY}:${workspace}`, JSON.stringify([...state.unreadRuns]));
}

function markRunUnread(runId, title, body) {
  if (!runId || runId === state.currentId) return;
  state.unreadRuns.add(String(runId));
  persistUnreadRuns();
  renderRunList();
  notifyBrowser(title, body, runId);
}

function clearRunUnread(runId) {
  if (!runId || !state.unreadRuns.delete(String(runId))) return;
  persistUnreadRuns();
  renderRunList();
}

function notifyBrowser(title, body, runId = '') {
  if (!state.notificationsEnabled || !('Notification' in window) || Notification.permission !== 'granted') return;
  const key = `${runId}:${title}:${body}`;
  if (state.notifiedEvents.has(key)) return;
  state.notifiedEvents.add(key);
  const notification = new Notification(title, { body, tag: runId || 'multiagent' });
  notification.onclick = () => {
    window.focus();
    if (runId) selectRun(runId);
    notification.close();
  };
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
  if (state.unreadRuns.has(String(run.id))) {
    const unread = document.createElement('span');
    unread.className = 'run-item-unread';
    unread.textContent = '新';
    unread.title = '有新的协作更新';
    meta.append(unread);
  }
  button.classList.toggle('has-unread', state.unreadRuns.has(String(run.id)));
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
  const stop = el.contextMenu.querySelector('[data-context-action="stop"]');
  const archive = el.contextMenu.querySelector('[data-context-action="archive"]');
  const deleteButton = el.contextMenu.querySelector('[data-context-action="delete"]');
  const archiveLabel = archive.querySelector('span:last-child');
  const active = run.live === true;

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
  if (!window.confirm(`永久删除“${title}”？\n\n任务记录和上传文档都会被删除，且无法恢复。`)) return;
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
  if (!window.confirm('确定停止当前群聊中的 Agent 吗？')) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(runId)}/stop`, {
      method: 'POST',
      body: JSON.stringify({}),
    });
    showToast('正在停止 Claude Code 和 Codex…');
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
  el.statusBadge.textContent = '等待任务';
  el.statusBadge.dataset.status = 'waiting';
  document.title = 'MultiAgent 工作台';
  closeNativeInteractionDialog();
  restoreDraft(null);
}

function renderDetail() {
  const record = state.detail?.record || {};
  const session = state.detail?.session || null;
  const recordedStatus = session?.status || record.status || 'unknown';
  const detached = !session && ACTIVE_RUN_STATUSES.has(String(recordedStatus).toLowerCase());
  const status = detached ? 'interrupted' : recordedStatus;
  const workspace = session?.workspace || record.workspace || state.health?.workspace || '';
  const task = session?.task || record.display_task || record.task || '未命名任务';
  const groupChat = true;

  el.emptyState.classList.add('hidden');
  el.runView.classList.remove('hidden');
  el.workspaceChip.textContent = workspace;
  el.workspaceChip.title = workspace;
  el.sidebarWorkspace.textContent = workspaceFolderName(workspace);
  el.statusBadge.textContent = statusLabel(status);
  el.statusBadge.dataset.status = statusKey(status);
  document.title = `${task} · MultiAgent`;
  el.quickTaskInput.placeholder = groupChat
    ? '@Claude 或 @Codex 定向交流；单独点名时由该 Agent 判断是否需要修改代码'
    : '在协作大厅输入新需求，让 Claude Code 与 Codex 开始协作';
  el.quickTaskSubmit.textContent = groupChat ? '发送消息' : '发送';
  el.quickAttach.title = groupChat ? '为这条消息添加附件' : '为新任务添加附件';
  el.quickAttach.setAttribute('aria-label', el.quickAttach.title);
  if (!groupChat) hideMentionMenu();

  const error = detached
    ? '任务已不在当前 UI 服务中运行；上次服务可能退出。'
    : session?.error || record.error || '';
  el.errorBanner.textContent = error;
  el.errorBanner.classList.toggle('notice-banner', Boolean(error) && ['interrupted', 'cancelled'].includes(status));
  el.errorBanner.classList.toggle('hidden', !error);

  renderAgentStatus(el.claudeStatus, el.claudeNavDot, 'claude', 'Claude Code', session, status);
  renderAgentStatus(el.codexStatus, el.codexNavDot, 'codex', 'Codex', session, status);
  renderArtifacts(record, session);
  renderOverview(record, session, status);
  renderTimeline(record, session);
  renderNativeInteraction(session);
  renderAgentProfile();
  setMainView(state.mainView);

  const canStop = ['starting', 'running', 'awaiting_interaction', 'stopping'].includes(status);
  el.stopTaskButton.classList.toggle('hidden', !canStop);
  el.stopTaskButton.disabled = status === 'stopping';
  el.stopTaskButton.textContent = status === 'stopping' ? '■ 正在停止…' : '■ 停止';
}

function renderAgentStatus(container, navDot, key, name, session, runStatus) {
  const event = session?.agent_events?.[key];
  const interaction = (session?.native_interactions || []).find(
    (request) => agentKeyFromName(request.source) === key,
  );
  const stopping = runStatus === 'stopping';
  const eventStatus = stopping
    ? 'stopping'
    : interaction ? 'waiting_user'
      : runStatus === 'awaiting_interaction' ? event?.status || 'waiting'
        : event?.status || (runStatus === 'complete' ? 'complete' : runStatus);
  const detail = stopping
    ? fallbackAgentDetail(name, runStatus)
    : interaction ? `${name} 正在等待你的权限决定或补充信息`
      : runStatus === 'awaiting_interaction' && !event ? `${name} 当前未被此请求阻塞`
        : event?.safe_summary || fallbackAgentDetail(name, runStatus);
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
  if (status === 'awaiting_interaction') return '正在等待你的权限决定或补充信息';
  if (status === 'stopping') return '正在安全停止当前任务';
  if (['failed', 'cancelled', 'interrupted'].includes(status)) return '当前任务已停止';
  return `等待 ${name} 的安全进度事件`;
}

function renderNativeInteraction(session) {
  const requests = Array.isArray(session?.native_interactions)
    ? session.native_interactions
    : [];
  const request = requests[0];
  if (!request) {
    closeNativeInteractionDialog();
    return;
  }
  if (state.nativeInteractionRunId && state.nativeInteractionRunId !== state.currentId) {
    closeNativeInteractionDialog();
  }
  if (
    state.nativeInteractionId === request.id
    && state.nativeInteractionRunId === state.currentId
    && el.nativeInteractionDialog.open
  ) return;

  state.nativeInteractionId = request.id;
  state.nativeInteractionRunId = state.currentId;
  el.nativeInteractionSource.textContent = `${request.source || 'Agent'} · 原生交互请求`;
  el.nativeInteractionTitle.textContent = request.title || '等待你的确认';
  el.nativeInteractionQueue.textContent = requests.length > 1
    ? `还有 ${requests.length - 1} 个请求正在排队`
    : '';
  el.nativeInteractionQueue.classList.toggle('hidden', requests.length <= 1);
  el.nativeInteractionMessage.textContent = request.message || '';
  el.nativeInteractionMessage.classList.toggle('hidden', !request.message);
  const command = String(request.command || '');
  el.nativeInteractionCommand.querySelector('pre').textContent = command;
  el.nativeInteractionCommand.classList.toggle('hidden', !command);
  const cwd = String(request.cwd || '');
  el.nativeInteractionCwd.textContent = cwd ? `工作目录：${cwd}` : '';
  el.nativeInteractionCwd.classList.toggle('hidden', !cwd);
  el.nativeInteractionQuestions.innerHTML = (request.questions || []).map((question) => {
    const id = String(question.id || 'question');
    const options = Array.isArray(question.options) ? question.options : [];
    const control = options.length
      ? `<select data-native-question="${escapeHtml(id)}">
          <option value="">请选择…</option>
          ${options.map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join('')}
          ${question.allow_other ? '<option value="__other__">其他…</option>' : ''}
        </select>
        ${question.allow_other ? `<input class="native-interaction-other hidden" data-native-other="${escapeHtml(id)}" type="${question.secret ? 'password' : 'text'}" placeholder="请输入其他回答" autocomplete="off" />` : ''}`
      : `<input data-native-question="${escapeHtml(id)}" type="${question.secret ? 'password' : 'text'}" autocomplete="off" placeholder="请输入回答" />`;
    return `<label class="native-interaction-question">
      <span>${escapeHtml(question.header || '需要你的回答')}</span>
      <strong>${escapeHtml(question.question || '请提供信息')}</strong>
      ${control}
    </label>`;
  }).join('');
  el.nativeInteractionQuestions.querySelectorAll('select[data-native-question]').forEach((select) => {
    select.addEventListener('change', () => {
      const other = el.nativeInteractionQuestions.querySelector(`[data-native-other="${cssEscape(select.dataset.nativeQuestion)}"]`);
      other?.classList.toggle('hidden', select.value !== '__other__');
      if (select.value === '__other__') other?.focus();
    });
  });
  el.nativeInteractionActions.innerHTML = (request.options || []).map((option) => {
    const action = String(option.value || '');
    const tone = action === 'approve' || action === 'submit'
      ? 'primary-button'
      : action === 'cancel' ? 'danger-button' : 'secondary-button';
    return `<button class="native-action ${tone}" type="button" data-native-action="${escapeHtml(action)}" title="${escapeHtml(option.description || '')}">${escapeHtml(option.label || action)}</button>`;
  }).join('');
  hideFormError(el.nativeInteractionError);
  if (!el.nativeInteractionDialog.open) el.nativeInteractionDialog.showModal();
}

async function submitNativeInteraction(action, button = null) {
  const runId = state.nativeInteractionRunId;
  const interactionId = state.nativeInteractionId;
  if (!runId || !interactionId) return;
  const answers = {};
  el.nativeInteractionQuestions.querySelectorAll('[data-native-question]').forEach((control) => {
    const questionId = control.dataset.nativeQuestion;
    let value = control.value;
    if (value === '__other__') {
      value = el.nativeInteractionQuestions.querySelector(`[data-native-other="${cssEscape(questionId)}"]`)?.value || '';
    }
    answers[questionId] = value ? [value] : [];
  });
  hideFormError(el.nativeInteractionError);
  if (button) setButtonBusy(button, true, '正在提交…');
  try {
    await api(`/api/sessions/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}`, {
      method: 'POST',
      body: JSON.stringify({ action, answers }),
    });
    closeNativeInteractionDialog();
    showToast(action === 'deny' || action === 'cancel' ? '已拒绝原生请求。' : '已发送决定，Agent 将继续处理。');
    await refreshAll();
  } catch (error) {
    showFormError(el.nativeInteractionError, error.message);
  } finally {
    if (button?.isConnected) setButtonBusy(button, false, button.dataset.originalLabel || button.textContent);
  }
}

async function declineNativeInteraction() {
  const cancel = el.nativeInteractionActions.querySelector('[data-native-action="cancel"]');
  const deny = el.nativeInteractionActions.querySelector('[data-native-action="deny"]');
  const button = cancel || deny;
  if (button) await submitNativeInteraction(button.dataset.nativeAction, button);
}

function closeNativeInteractionDialog() {
  if (el.nativeInteractionDialog.open) el.nativeInteractionDialog.close();
  state.nativeInteractionId = null;
  state.nativeInteractionRunId = null;
  hideFormError(el.nativeInteractionError);
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value || ''));
  return String(value || '').replace(/[^A-Za-z0-9_-]/g, '\\$&');
}

function renderArtifacts(record, session) {
  renderGroupChat(record, session);
}

function renderGroupChat(record, session) {
  const chat = session?.group_chat || record.group_chat || {};
  const serverMessages = Array.isArray(chat.messages) ? chat.messages : [];
  const pendingTurns = reconcilePendingChatMessages(
    record.id || state.currentId || 'current',
    serverMessages,
    session?.status || record.status,
  );
  const pendingUsers = pendingTurns.filter((message) => !message.server_user_id && !message.hidden);
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
      reply_to: turn.server_user_id || turn.client_id,
    }))
  ));
  const messages = orderGroupChatMessages(serverMessages, pendingUsers, pendingReplies)
    .filter((message) => !message.hidden);
  const runId = record.id || state.currentId || 'current';
  state.messageTexts.clear();
  const entries = messages.map((message, index) => {
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
    const failureReason = String(message.failure_reason || '');
    const failedReply = message.status === 'failed' || Boolean(failureReason);
    const feedKey = `msg-${message.id || message.client_id || `${sender}-${index}`}`;
    const role = loadingReply
      ? '正在回复 · 等待内容'
      : failedReply
      ? `${failureReason === 'timeout' ? '响应超时' : '回复失败'} · 共享给所有成员`
      : optimistic
      ? message.delivery_status === 'accepted' ? '已发送 · 等待 Agent 回复' : '正在发送到群聊'
      : user
      ? `${message.edited_from ? '编辑后重新发送' : execution ? '执行请求' : '讨论消息'} · 发送给 ${recipientNames || '群聊'}`
      : message.retry_of
      ? `${message.retry_mode === 'continue' ? '继续回复' : '重新生成'} · 共享给所有成员`
      : execution ? '目标工作区执行结果 · 共享给所有成员' : '群聊回复 · 共享给所有成员';
    const html = messageCard(
      user ? '你' : agentName(sender),
      role,
      {
        final_text: message.content || '',
        attachments: message.attachments || [],
        duration_seconds: message.duration_seconds || 0,
        created_at: message.created_at || '',
        workspace: message.workspace || '',
        changes: message.changes || null,
        changes_key: `chat-${runId}-${message.id || 'message'}`,
        message_id: message.id || '',
        retry_of: message.retry_of || '',
        pending: optimistic,
        loading: loadingReply,
        failed: failedReply,
        feed_key: feedKey,
        quotable: !optimistic && !loadingReply && !failedReply,
        run_id: runId,
      },
      user ? 'user' : sender,
      loadingReply
        ? '回复中'
        : failedReply
        ? failureReason === 'timeout' ? '超时' : '失败'
        : optimistic
        ? message.delivery_status === 'accepted' ? '已发送' : '发送中'
        : execution ? (user ? '执行' : '执行结果') : (user ? '消息' : '回复'),
    );
    return { key: feedKey, html };
  });
  patchFeed(runId, entries, `<div class="chat-empty">
    <span class="chat-empty-mark" aria-hidden="true">@</span>
    <strong>发送第一条消息开始群聊</strong>
    <small>输入 @ 可选择响应者，也可以直接发送给默认成员；附件支持点击、拖放或粘贴图片。</small>
  </div>`);
  appendStreamToFeed(runId);
}

const feedHtmlCache = new Map();
let feedCacheRunId = null;

// Keyed incremental patch of the message feed. Rebuilding the whole subtree on
// every SSE tick dropped text selection, collapsed <details> state and reset the
// scroll position, so only nodes whose markup actually changed are replaced.
function patchFeed(runId, entries, emptyMarkup = '') {
  const container = el.artifactFeed;
  if (feedCacheRunId !== runId) {
    feedCacheRunId = runId;
    feedHtmlCache.clear();
    container.replaceChildren();
    state.feedPinnedToBottom = true;
  }
  const pinned = state.feedPinnedToBottom || isFeedNearBottom();

  if (!entries.length) {
    feedHtmlCache.clear();
    container.innerHTML = emptyMarkup;
    updateFeedJumpButton();
    return;
  }

  const existing = new Map();
  Array.from(container.children).forEach((node) => {
    const key = node instanceof HTMLElement ? node.dataset.feedKey : '';
    if (key) existing.set(key, node);
    else node.remove();
  });

  const keys = new Set(entries.map((entry) => entry.key));
  let previous = null;
  entries.forEach((entry) => {
    let node = existing.get(entry.key) || null;
    if (node && feedHtmlCache.get(entry.key) !== entry.html) {
      const replacement = nodeFromMarkup(entry.html);
      if (replacement) {
        node.replaceWith(replacement);
        node = replacement;
      }
    }
    if (!node) node = nodeFromMarkup(entry.html);
    if (!node) return;
    feedHtmlCache.set(entry.key, entry.html);
    existing.set(entry.key, node);
    const anchor = previous ? previous.nextSibling : container.firstChild;
    if (node !== anchor) container.insertBefore(node, anchor);
    previous = node;
  });

  existing.forEach((node, key) => {
    if (keys.has(key)) return;
    node.remove();
    feedHtmlCache.delete(key);
  });

  if (pinned) scrollChatToBottom();
  else updateFeedJumpButton();
}

function nodeFromMarkup(html) {
  const template = document.createElement('template');
  template.innerHTML = String(html || '').trim();
  return template.content.firstElementChild;
}

function isFeedNearBottom(threshold = 140) {
  const container = el.artifactFeed;
  return container.scrollHeight - container.scrollTop - container.clientHeight <= threshold;
}

function updateFeedJumpButton() {
  if (!el.feedJump) return;
  el.feedJump.classList.toggle('hidden', isFeedNearBottom());
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
  const failed = result.failed === true;
  const feedKey = String(result.feed_key || '');
  if (feedKey && text) state.messageTexts.set(feedKey, String(text));
  const tools = loading || !text ? '' : messageToolsMarkup(feedKey, result.quotable === true, result.message_id || '');
  return `<article class="message-row message-${speaker}${highlight ? ' message-highlight' : ''}${pending ? ' message-pending' : ''}${loading ? ' message-loading' : ''}${failed ? ' message-failed' : ''}"${feedKey ? ` data-feed-key="${escapeHtml(feedKey)}"` : ''}>
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
      ${attachmentMarkup(attachments, result.run_id)}
      ${details ? '<div class="message-actions"><button class="thread-button" data-open-detail="overview" type="button">▢ 查看详情</button></div>' : ''}
      ${tools}
    </div>
  </article>`;
}

function messageToolsMarkup(feedKey, quotable, messageId = '') {
  if (!feedKey) return '';
  const message = findGroupChatMessage(messageId || feedKey.replace(/^msg-/, ''));
  const quote = quotable
    ? `<button class="message-tool" data-message-quote="${escapeHtml(feedKey)}" type="button" title="引用这条消息回复">❝ 引用</button>`
    : '';
  const edit = message?.sender === 'user' && !message.hidden
    ? `<button class="message-tool" data-message-edit="${escapeHtml(message.id)}" type="button" title="编辑后重新发送">✎ 编辑</button>`
    : '';
  const retry = message?.role === 'assistant' && !message.hidden
    ? `<button class="message-tool" data-message-retry="${escapeHtml(message.id)}" data-retry-mode="regenerate" type="button" title="重新生成这条回复">↻ 重试</button>
       <button class="message-tool" data-message-retry="${escapeHtml(message.id)}" data-retry-mode="continue" type="button" title="继续生成这条回复">⋯ 继续</button>`
    : '';
  return `<div class="message-tools">
    <button class="message-tool" data-message-copy="${escapeHtml(feedKey)}" type="button" title="复制这条消息的原文">⧉ 复制</button>
    ${edit}
    ${retry}
    ${quote}
  </div>`;
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

function attachmentMarkup(attachments, runId) {
  if (!attachments.length) return '';
  return `<div class="message-attachments">${attachments.map((item) => {
    const name = item.name || '文档';
    const size = escapeHtml(formatBytes(item.size || 0));
    const title = escapeHtml(item.path || name);
    const label = `<span aria-hidden="true">▧</span><strong>${escapeHtml(name)}</strong><small>${size}</small>`;
    if (item.pending || !runId || !item.name) {
      return `<span class="message-attachment" title="${title}">${label}</span>`;
    }
    const href = `/api/runs/${encodeURIComponent(runId)}/attachments/${encodeURIComponent(item.name)}`;
    const extension = item.name.includes('.') ? item.name.split('.').at(-1).toLowerCase() : '';
    if (IMAGE_EXTENSIONS.has(extension)) {
      // ?inline=1 is served with a content type derived from the validated
      // extension, so rendering it in an <img> cannot smuggle active content.
      return `<figure class="message-attachment message-attachment-image" title="${title}">
        <a class="message-attachment-preview" href="${escapeHtml(href)}?inline=1" target="_blank" rel="noopener" aria-label="预览 ${escapeHtml(name)}"><img src="${escapeHtml(href)}?inline=1" alt="${escapeHtml(name)}" loading="lazy" /></a>
        <figcaption><span><strong>${escapeHtml(name)}</strong><small>${size}</small></span><a href="${escapeHtml(href)}" download="${escapeHtml(name)}">下载</a></figcaption>
      </figure>`;
    }
    return `<a class="message-attachment message-attachment-link" href="${escapeHtml(href)}" download="${escapeHtml(name)}" title="${title}">${label}</a>`;
  }).join('')}</div>`;
}

function avatarMarkup(agent) {
  const key = agent === 'codex' ? 'codex' : agent === 'user' ? 'user' : agent === 'claude' ? 'claude' : 'system';
  const initials = { claude: 'CL', codex: 'CX', user: 'YO', system: 'MA' }[key];
  return `<span class="avatar avatar-${key}">${initials}</span>`;
}

function renderOverview(record, session, status) {
  const summary = record.summary || {};
  const rows = [
    ['状态', statusLabel(status)],
    ['工作区', compactPath(session?.workspace || record.workspace || '')],
    ['协作模式', '群聊协作'],
    ['执行方式', '按消息中的 @ 动态指定'],
    ['流程细节', '讨论只读 · 单 Agent 写目标工作区 · 全员共享上下文'],
    ['累计耗时', formatDuration(summary.elapsed_seconds || 0)],
    ['运行次数', String(record.attempts || 1)],
    ['输入令牌数', formatNumber(summary.input_tokens || 0)],
    ['输出令牌数', formatNumber(summary.output_tokens || 0)],
    ['上传文档', `${Array.isArray(session?.attachments || record.attachments) ? (session?.attachments || record.attachments).length : 0} 个`],
  ];
  rows.push(['执行轮次', `${summary.execution_turns || 0} 次`]);
  el.overview.innerHTML = rows.map(([label, value]) => `<div class="info-row"><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join('');
}

function renderTimeline(record, session) {
  const combined = [...(Array.isArray(record.events) ? record.events : []), ...(Array.isArray(session?.events) ? session.events : [])];
  const seen = new Set();
  const events = combined.filter((event) => {
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
  el.eventTimeline.innerHTML = visibleEvents.map(renderActivityEvent).join('');
}

function renderActivityEvent(event) {
  const activity = event.activity && typeof event.activity === 'object' ? event.activity : null;
  if (!activity) {
    return `<div class="event-item event-kind-${escapeHtml(String(event.kind || 'status'))}">
      <div class="event-item-head"><span class="status-dot status-${statusKey(event.status)}"></span><strong>${escapeHtml(eventSourceLabel(event.source))}</strong></div>
      <div>${escapeHtml(event.safe_summary || event.text || eventKindLabel(event.kind))}</div>
      <small>${escapeHtml(stepLabel(event.step_id) || eventKindLabel(event.kind, ''))} · ${escapeHtml(formatEventTime(event.timestamp))}</small>
    </div>`;
  }
  const type = ['command', 'file_change', 'read', 'search', 'tool'].includes(activity.type) ? activity.type : 'tool';
  const detail = String(activity.detail || '').trim();
  const open = detail && ['working', 'starting', 'waiting_model'].includes(String(event.status || '')) ? ' open' : '';
  const detailMarkup = detail
    ? `<div class="activity-card-body"><span>${escapeHtml(activity.detail_label || '详情')}</span><pre><code>${escapeHtml(detail)}</code></pre></div>`
    : '';
  const toolName = activity.tool_name ? `<span class="activity-tool-name">${escapeHtml(activity.tool_name)}</span>` : '';
  return `<details class="activity-card activity-${type} ${detail ? 'activity-expandable' : 'activity-static'}"${open}>
    <summary>
      <span class="activity-icon" aria-hidden="true">${activityIcon(type)}</span>
      <span class="activity-heading"><strong>${escapeHtml(activity.title || eventKindLabel(event.kind))}</strong><small>${escapeHtml(eventSourceLabel(event.source))} · ${escapeHtml(formatEventTime(event.timestamp))}</small></span>
      ${toolName}
      <span class="activity-state"><span class="status-dot status-${statusKey(event.status)}"></span>${escapeHtml(activityStatusLabel(event.status))}</span>
    </summary>
    ${detailMarkup}
  </details>`;
}

function activityIcon(type) {
  return { command: '&gt;_', file_change: '±', read: '▤', search: '⌕', tool: '◇' }[type] || '◇';
}

function activityStatusLabel(status) {
  const value = String(status || '').toLowerCase();
  if (['working', 'starting', 'waiting_model', 'in_progress'].includes(value)) return '进行中';
  if (['failed', 'error'].includes(value)) return '失败';
  if (['warning', 'waiting_user'].includes(value)) return '需处理';
  return '已完成';
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
  const role = key === 'claude'
    ? '需求分析与协作伙伴'
    : '工程实现与验证协作伙伴';
  const coordinator = '按消息点名参与，写权限随本轮动态授予';
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

function normalizeContent(text) {
  return String(text || '').trim();
}

function renderMarkdown(source) {
  const codeBlocks = [];
  const text = String(source || '').replace(/```([^\n]*)\n([\s\S]*?)```/g, (_match, language, code) => {
    const token = `@@CODEBLOCK${codeBlocks.length}@@`;
    const label = language.trim();
    codeBlocks.push(`<figure class="code-block">
      <figcaption class="code-block-head"><span class="code-block-lang">${escapeHtml(label || '代码')}</span><button class="code-copy" type="button" data-code-copy>⧉ 复制</button></figcaption>
      <pre><code data-language="${escapeHtml(label)}">${escapeHtml(code.trimEnd())}</code></pre>
    </figure>`);
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
  if (value.includes('complete') || value === 'ready' || value === 'done' || value === 'resolved') return 'complete';
  if (value.includes('fail') || value.includes('error') || value === 'open') return 'failed';
  if (value.includes('interrupt')) return 'interrupted';
  if (value.includes('cancel') || value === 'blocked') return 'cancelled';
  if (value === 'awaiting_interaction' || value === 'waiting_user') return 'awaiting_interaction';
  if (value.includes('await')) return 'working';
  if (value === 'stopping') return 'cancelled';
  if (value.includes('run') || value.includes('work') || value.includes('progress') || value === 'starting') return 'running';
  return 'waiting';
}

function statusLabel(status) {
  const labels = {
    starting: '正在启动',
    running: '协作中',
    ready: '等待消息',
    awaiting_interaction: '等待你的操作',
    waiting_user: '等待你的操作',
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
    skipped: '已跳过',
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
    tool: '工具活动',
    tool_result: '工具结果',
    lifecycle: '运行状态',
    interaction_request: '权限请求',
    interaction_response: '交互结果',
  };
  return labels[String(kind || '').toLowerCase()] || fallback;
}

function stepLabel(step) {
  const value = String(step || '');
  const labels = {
    tool: '工具活动',
  };
  if (labels[value]) return labels[value];
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
