/* Azure Advisor web UI.
 *
 * Plain JavaScript, no build step. Everything shown is put on the page as text
 * (createElement + textContent), never as markup: Advisor text and model answers
 * are untrusted, and the page's Content-Security-Policy requires Trusted Types.
 */
(function () {
  'use strict';

  const JIRA_KEY = /^[A-Z][A-Z0-9_]+-[1-9][0-9]*$/;
  const JIRA_KEY_MAX = 60;
  const MAX_LOG_LINES = 500;
  const APPROVAL_ENABLE_DELAY_MS = 600;
  const RECONNECT_MS = 3000;
  const BANNER_MS = 10000;
  const STICK_TO_BOTTOM_PX = 60;

  // The filters the server applies (they are sent with the request).
  const CATEGORIES = ['Cost', 'Security', 'Performance', 'OperationalExcellence', 'HighAvailability'];
  const IMPACTS = ['High', 'Medium', 'Low'];
  const STATUSES = ['New', 'Dismissed', 'Postponed'];

  const COLUMNS = [
    { key: 'impact', label: 'Impact' },
    { key: 'category', label: 'Category' },
    { key: 'problem', label: 'Recommendation' },
    { key: 'resource_name', label: 'Resource' },
    { key: 'resource_group', label: 'Resource group' },
    { key: 'last_updated', label: 'Updated' },
    { key: 'jira', label: 'Jira' },
  ];
  const IMPACT_RANK = { high: 0, medium: 1, low: 2 };
  const ROLE_LABEL = { user: 'You', assistant: 'Agent', error: 'Error' };

  const state = {
    signedIn: false,
    stream: null,
    reconnectTimer: null,
    snapshotSeen: false,
    jiraBase: null,

    azure: { state: 'unknown', reason: null, retry: null },
    subscription: '',
    filters: { status: '', category: '', impact: '', group: '', text: '' },
    sort: { key: 'impact', dir: 1 },
    rows: [],
    truncated: false,
    fetchedAt: '',
    loading: false,
    loadError: '',
    loadTicket: 0,
    selectedId: null,

    rawOpen: false,
    linkDraft: '',
    linkBusy: false,
    linkError: '',

    chat: { enabled: false, reason: null, wedged: false, transcript: [], run: null, progress: [] },
    sending: false,
    pendingMessage: null,
    chatError: '',

    approvals: [],
    shownApproval: null,
    approvalBusy: false,
    approvalTimer: null,
    approvalEnableTimer: null,
    bannerTimer: null,
  };

  // -- small helpers ------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  function el(tag, options, children) {
    const node = document.createElement(tag);
    const opts = options || {};
    if (opts.class) node.className = opts.class;
    if (opts.text !== undefined) node.textContent = opts.text;
    if (opts.attrs) {
      for (const [name, value] of Object.entries(opts.attrs)) node.setAttribute(name, value);
    }
    for (const child of children || []) node.append(child);
    return node;
  }

  function lower(value) {
    return typeof value === 'string' ? value.toLowerCase() : '';
  }

  function formatTime(text) {
    if (!text) return '';
    const parsed = Date.parse(text);
    return Number.isNaN(parsed) ? text : new Date(parsed).toLocaleString();
  }

  function newMessageId() {
    const bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  class ApiFailure extends Error {
    constructor(status, message, data) {
      super(message);
      this.status = status;
      this.data = data || null;
    }
  }

  async function api(method, path, body) {
    const options = { method, credentials: 'same-origin', headers: {} };
    if (method !== 'GET') {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body || {});
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new ApiFailure(0, 'Cannot reach the server.');
    }
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      const message = data && typeof data.error === 'string' ? data.error : 'The request failed (' + response.status + ').';
      if (response.status === 401 && state.signedIn) {
        showSignin('Your session is no longer valid. Open the address the server printed.');
      }
      throw new ApiFailure(response.status, message, data);
    }
    return data;
  }

  function notify(text, bad) {
    const banner = $('banner');
    banner.textContent = text;
    banner.className = bad ? 'banner bad' : 'banner';
    banner.hidden = false;
    if (state.bannerTimer) clearTimeout(state.bannerTimer);
    state.bannerTimer = setTimeout(() => {
      banner.hidden = true;
    }, BANNER_MS);
  }

  function setOptions(select, values, firstLabel) {
    const current = select.value;
    const options = [];
    const first = el('option', { text: firstLabel });
    first.value = '';
    options.push(first);
    for (const item of values) {
      const value = typeof item === 'string' ? item : item.value;
      const label = typeof item === 'string' ? item : item.label;
      const option = el('option', { text: label });
      option.value = value;
      options.push(option);
    }
    select.replaceChildren(...options);
    select.value = values.some((item) => (typeof item === 'string' ? item : item.value) === current) ? current : '';
  }

  // -- signing in and the event stream ------------------------------------------------

  function showSignin(message) {
    state.signedIn = false;
    closeStream();
    $('app').hidden = true;
    $('signin').hidden = false;
    if (message) $('signin-text').textContent = message;
    $('connection').textContent = '';
    const dialog = $('approval-dialog');
    state.approvals = [];
    if (dialog.open) dialog.close();
  }

  function showApp() {
    state.signedIn = true;
    $('signin').hidden = true;
    $('app').hidden = false;
  }

  function closeStream() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
  }

  function setConnection(text) {
    $('connection').textContent = text;
  }

  async function reconnectLater() {
    state.reconnectTimer = null;
    try {
      await api('GET', '/api/session');
    } catch (error) {
      if (error.status === 401) {
        showSignin('Your session is no longer valid. Open the address the server printed.');
        return;
      }
      setConnection('Offline');
      state.reconnectTimer = setTimeout(reconnectLater, RECONNECT_MS);
      return;
    }
    connect();
  }

  function connect() {
    closeStream();
    setConnection('Connecting...');
    const stream = new EventSource('/api/events');
    state.stream = stream;
    stream.onopen = () => {
      if (state.stream === stream) setConnection('Live');
    };
    stream.onerror = () => {
      if (state.stream !== stream) return;
      setConnection('Reconnecting...');
      if (stream.readyState === EventSource.CLOSED) {
        // The server refused the stream (not signed in, or shutting down):
        // the browser will not retry by itself.
        closeStream();
        state.reconnectTimer = setTimeout(reconnectLater, RECONNECT_MS);
      }
    };
    const handlers = {
      snapshot: onSnapshot,
      azure_status: onAzureStatus,
      chat_started: onChatStarted,
      chat_progress: onChatProgress,
      chat_finished: onChatFinished,
      chat_state: onChatState,
      chat_reset: onChatReset,
      approval_requested: onApprovalRequested,
      approval_resolved: onApprovalResolved,
      link_changed: onLinkChanged,
      shutdown: onShutdown,
    };
    for (const [name, handler] of Object.entries(handlers)) {
      stream.addEventListener(name, (event) => {
        if (state.stream !== stream) return;
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        handler(data);
      });
    }
  }

  function onShutdown() {
    closeStream();
    setConnection('Server stopped');
    notify('The server has stopped. Start it again and open the new address it prints.', true);
    state.approvals = [];
    renderApprovals();
  }

  function onSnapshot(data) {
    const reconnected = state.snapshotSeen;
    state.snapshotSeen = true;
    state.jiraBase = typeof data.jira_base_url === 'string' ? data.jira_base_url : null;
    state.chat = normalizeChat(data.chat);
    state.approvals = (Array.isArray(data.approvals) ? data.approvals : []).map(withDeadline);
    const wasReady = state.azure.state === 'ready';
    applyAzure(data.azure);
    renderChat();
    renderApprovals();
    if (state.azure.state === 'ready' && (reconnected || !wasReady)) {
      loadRecommendations(false);
      loadSubscriptions();
    }
  }

  // -- Azure ---------------------------------------------------------------------------

  function applyAzure(status) {
    const value = status && typeof status === 'object' ? status : {};
    state.azure = {
      state: typeof value.state === 'string' ? value.state : 'unknown',
      reason: typeof value.reason === 'string' ? value.reason : null,
      retry: typeof value.retry === 'string' ? value.retry : null,
    };
    renderAzure();
    renderToolbar();
    renderTable();
  }

  function onAzureStatus(status) {
    const wasReady = state.azure.state === 'ready';
    applyAzure(status);
    if (state.azure.state === 'ready' && !wasReady) {
      loadRecommendations(false);
      loadSubscriptions();
    }
  }

  function renderAzure() {
    const azure = state.azure;
    const box = $('azure-status');
    let text;
    if (azure.state === 'ready') {
      text = 'Azure: connected';
    } else if (azure.state === 'failed') {
      text = 'Azure: unavailable' + (azure.reason ? ' - ' + azure.reason : '');
      if (azure.retry === 'auto') text += ' (retrying automatically)';
    } else {
      text = 'Azure: starting (the first start can take about a minute)';
    }
    box.className = 'status ' + (azure.state === 'ready' ? 'ready' : azure.state === 'failed' ? 'failed' : 'starting');
    $('azure-text').textContent = text;
    $('azure-retry').hidden = azure.state !== 'failed';
  }

  function renderToolbar() {
    const ready = state.azure.state === 'ready';
    $('refresh').disabled = !ready || state.loading;
    $('subscription').disabled = !ready;
  }

  async function retryAzure() {
    $('azure-retry').disabled = true;
    try {
      await api('POST', '/api/azure/retry', {});
    } catch (error) {
      notify(error.message, true);
    } finally {
      $('azure-retry').disabled = false;
    }
  }

  async function loadSubscriptions() {
    try {
      const data = await api('GET', '/api/subscriptions');
      const list = Array.isArray(data.subscriptions) ? data.subscriptions : [];
      setOptions(
        $('subscription'),
        list.map((item) => ({ value: String(item.id), label: String(item.name || item.id) })),
        'Default subscription'
      );
      state.subscription = $('subscription').value;
    } catch (error) {
      // Not essential: the default subscription is still available.
    }
  }

  // -- the table -------------------------------------------------------------------------

  async function loadRecommendations(refresh) {
    state.loadTicket += 1;
    const ticket = state.loadTicket;
    state.loading = true;
    state.loadError = '';
    renderToolbar();
    renderTable();

    const params = new URLSearchParams();
    for (const name of ['status', 'category', 'impact']) {
      if (state.filters[name]) params.set(name, state.filters[name]);
    }
    if (state.subscription) params.set('subscription', state.subscription);
    if (refresh) params.set('refresh', '1');

    let data = null;
    let failure = '';
    try {
      data = await api('GET', '/api/recommendations?' + params.toString());
    } catch (error) {
      failure = error.message;
    }
    if (ticket !== state.loadTicket) return; // a newer request replaced this one
    state.loading = false;
    if (data) {
      state.rows = Array.isArray(data.rows) ? data.rows : [];
      state.truncated = data.truncated === true;
      state.fetchedAt = typeof data.fetched_at === 'string' ? data.fetched_at : '';
      if (state.selectedId && !state.rows.some((row) => row.row_id === state.selectedId)) {
        state.selectedId = null;
      }
    } else {
      state.loadError = failure;
    }
    renderToolbar();
    renderGroupFilter();
    renderTable();
    renderDetail();
  }

  function renderGroupFilter() {
    const groups = Array.from(new Set(state.rows.map((row) => row.resource_group).filter(Boolean)));
    groups.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
    const select = $('f-group');
    select.value = state.filters.group;
    setOptions(select, groups, 'All');
    state.filters.group = select.value;
  }

  function sortValue(row, key) {
    switch (key) {
      case 'impact': {
        const rank = IMPACT_RANK[lower(row.impact)];
        return rank === undefined ? 3 : rank;
      }
      case 'problem':
        return rowTitle(row).toLowerCase();
      case 'last_updated': {
        const parsed = Date.parse(row.last_updated);
        return Number.isNaN(parsed) ? null : parsed;
      }
      case 'jira':
        return row.link ? row.link.jira_key : null;
      default:
        return lower(row[key]) || null;
    }
  }

  function compareRows(a, b) {
    const { key, dir } = state.sort;
    const left = sortValue(a, key);
    const right = sortValue(b, key);
    // Rows without a value go last, whichever way the column is sorted.
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    if (left < right) return -dir;
    if (left > right) return dir;
    return 0;
  }

  function rowTitle(row) {
    return row.problem || row.description || row.solution || '(no description)';
  }

  function visibleRows() {
    const needle = state.filters.text.trim().toLowerCase();
    const group = state.filters.group;
    const rows = state.rows.filter((row) => {
      if (group && row.resource_group !== group) return false;
      if (!needle) return true;
      const haystack = [
        row.problem,
        row.solution,
        row.description,
        row.resource_name,
        row.resource_type,
        row.resource_group,
        row.category,
        row.impact,
        row.link ? row.link.jira_key : '',
      ]
        .join('\n')
        .toLowerCase();
      return haystack.includes(needle);
    });
    rows.sort(compareRows);
    return rows;
  }

  function jiraUrl(key) {
    if (!state.jiraBase || typeof key !== 'string' || !JIRA_KEY.test(key)) return null;
    try {
      const url = new URL(state.jiraBase + '/browse/' + encodeURIComponent(key));
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
    } catch (error) {
      return null;
    }
  }

  function jiraKeyNode(key) {
    const url = jiraUrl(key);
    if (!url) return el('span', { class: 'tag', text: key });
    const anchor = el('a', { class: 'tag', text: key });
    anchor.href = url;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    return anchor;
  }

  function buildRow(row) {
    const tr = el('tr', { attrs: { tabindex: '0' } });
    tr.dataset.rowId = row.row_id;
    tr.setAttribute('aria-selected', row.row_id === state.selectedId ? 'true' : 'false');
    const impactClass = IMPACT_RANK[lower(row.impact)] !== undefined ? 'impact-' + lower(row.impact) : '';
    tr.append(
      el('td', { class: 'nowrap ' + impactClass, text: row.impact || '' }),
      el('td', { class: 'nowrap', text: row.category || '' }),
      el('td', { class: 'problem', text: rowTitle(row) }),
      el('td', { text: row.resource_name || '' }),
      el('td', { text: row.resource_group || '' }),
      el('td', { class: 'nowrap', text: formatTime(row.last_updated) })
    );
    const jira = el('td', { class: 'nowrap' });
    if (row.link && row.link.jira_key) jira.append(jiraKeyNode(row.link.jira_key));
    tr.append(jira);
    return tr;
  }

  function renderHead() {
    const cells = COLUMNS.map((column) => {
      const th = el('th', { attrs: { scope: 'col' } });
      const active = state.sort.key === column.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir === 1 ? 'ascending' : 'descending') : 'none');
      const arrow = active ? (state.sort.dir === 1 ? ' ↑' : ' ↓') : '';
      const button = el('button', { text: column.label + arrow, attrs: { type: 'button' } });
      button.dataset.key = column.key;
      th.append(button);
      return th;
    });
    $('table-head').replaceChildren(...cells);
  }

  function renderTable() {
    const rows = visibleRows();
    $('table-body').replaceChildren(...rows.map(buildRow));

    let message = '';
    if (state.azure.state !== 'ready' && state.rows.length === 0) {
      message =
        state.azure.state === 'failed'
          ? 'Azure is unavailable' + (state.azure.reason ? ': ' + state.azure.reason : '.')
          : 'Waiting for the Azure MCP server to start...';
    } else if (state.loading && state.rows.length === 0) {
      message = 'Loading recommendations...';
    } else if (state.loadError) {
      message = 'Could not load the recommendations: ' + state.loadError;
    } else if (state.rows.length === 0 && state.fetchedAt) {
      message = 'Azure Advisor returned no recommendations for these filters.';
    } else if (rows.length === 0 && state.rows.length > 0) {
      message = 'No recommendation matches the search or the resource group.';
    }
    $('table-message').textContent = message;

    let count = '';
    if (state.rows.length > 0) {
      count = rows.length === state.rows.length ? rows.length + ' shown' : rows.length + ' of ' + state.rows.length + ' shown';
      if (state.loading) count += ' (refreshing...)';
    }
    $('row-count').textContent = count;

    const truncated = $('truncated');
    if (state.truncated) {
      truncated.textContent =
        'Only the first ' + state.rows.length + ' recommendations are shown. Narrow the list with Category, Impact or Status to see the rest.';
      truncated.hidden = false;
    } else {
      truncated.hidden = true;
    }
    renderHead();
  }

  function markSelected() {
    for (const tr of $('table-body').children) {
      tr.setAttribute('aria-selected', tr.dataset.rowId === state.selectedId ? 'true' : 'false');
    }
  }

  function selectedRow() {
    return state.rows.find((row) => row.row_id === state.selectedId) || null;
  }

  function selectRow(rowId) {
    if (state.selectedId === rowId) return;
    state.selectedId = rowId;
    state.linkDraft = '';
    state.linkError = '';
    markSelected();
    renderDetail();
  }

  // -- the detail panel and Jira links ------------------------------------------------

  function renderDetail() {
    const box = $('detail');
    const row = selectedRow();
    if (!row) {
      box.replaceChildren(el('p', { class: 'muted', text: 'Select a recommendation to see the details.' }));
      return;
    }
    const facts = [
      ['Problem', row.problem],
      ['Solution', row.solution],
      ['Description', row.description],
      ['Resource', row.resource_name],
      ['Resource type', row.resource_type],
      ['Resource group', row.resource_group],
      ['Category', row.category],
      ['Impact', row.impact],
      ['Status', row.status],
      ['Updated', formatTime(row.last_updated)],
      ['Subscription', row.subscription_id],
      ['Resource ID', row.resource_id],
      ['Recommendation type', row.recommendation_type_id],
      ['Advisor ID', row.advisor_id],
    ];
    const list = el('dl', { class: 'facts' });
    for (const [label, value] of facts) {
      if (!value) continue;
      list.append(el('dt', { text: label }), el('dd', { text: String(value) }));
    }

    const raw = el('details', { class: 'raw' });
    raw.open = state.rawOpen;
    raw.addEventListener('toggle', () => {
      state.rawOpen = raw.open;
    });
    raw.append(el('summary', { text: 'Raw data (JSON)' }), el('pre', { text: JSON.stringify(row.raw, null, 2) }));

    box.replaceChildren(el('h3', { class: 'detail-title', text: rowTitle(row) }), list, raw, jiraSection(row));
  }

  function jiraSection(row) {
    const section = el('div', { class: 'jira' });
    section.append(el('h3', { text: 'Jira' }));

    if (!row.linkable) {
      section.append(
        el('p', {
          class: 'muted',
          text: 'This finding has no recommendation type or resource ID, so it cannot be linked to a Jira issue.',
        })
      );
      return section;
    }

    if (row.link && row.link.jira_key) {
      const line = el('p', {}, [document.createTextNode('Linked to '), jiraKeyNode(row.link.jira_key)]);
      if (row.link.status) line.append(el('span', { class: 'muted', text: ' (mapping status: ' + row.link.status + ')' }));
      const unlink = el('button', { text: 'Unlink', attrs: { type: 'button' } });
      unlink.disabled = state.linkBusy;
      unlink.addEventListener('click', () => unlinkRow(row));
      section.append(line, el('div', { class: 'row' }, [unlink]));
    } else {
      section.append(el('p', { class: 'muted', text: 'Not linked. Enter the key of the Jira issue that tracks this finding.' }));
      const input = el('input', {
        attrs: { type: 'text', maxlength: String(JIRA_KEY_MAX), placeholder: 'ABC-123', autocomplete: 'off', 'aria-label': 'Jira issue key' },
      });
      input.value = state.linkDraft;
      input.disabled = state.linkBusy;
      input.addEventListener('input', () => {
        state.linkDraft = input.value;
      });
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          linkRow(row, input.value);
        }
      });
      const link = el('button', { class: 'primary', text: 'Link', attrs: { type: 'button' } });
      link.disabled = state.linkBusy;
      link.addEventListener('click', () => linkRow(row, input.value));
      section.append(el('div', { class: 'row' }, [input, link]));
    }
    if (state.linkError) section.append(el('p', { class: 'error', text: state.linkError, attrs: { role: 'alert' } }));
    return section;
  }

  function sameFinding(row, link) {
    return (
      !!link &&
      lower(row.recommendation_type_id) === lower(link.recommendation_type_id) &&
      lower(row.resource_id) === lower(link.resource_id)
    );
  }

  function applyLink(link, removed) {
    let selectedChanged = false;
    for (const row of state.rows) {
      if (!sameFinding(row, link)) continue;
      row.link = removed ? null : link;
      if (row.row_id === state.selectedId) selectedChanged = true;
    }
    renderTable();
    markSelected();
    if (selectedChanged) renderDetail();
  }

  function onLinkChanged(data) {
    if (data && data.link) applyLink(data.link, data.removed === true);
  }

  async function linkRow(row, rawKey) {
    if (state.linkBusy) return;
    const key = String(rawKey).trim().toUpperCase();
    if (!JIRA_KEY.test(key) || key.length > JIRA_KEY_MAX) {
      state.linkError = 'That is not a Jira issue key (expected something like ABC-123).';
      renderDetail();
      focusLinkInput();
      return;
    }
    state.linkBusy = true;
    state.linkError = '';
    state.linkDraft = key;
    renderDetail();
    let failed = false;
    try {
      const data = await api('PUT', '/api/links', {
        recommendation_type_id: row.recommendation_type_id,
        resource_id: row.resource_id,
        jira_key: key,
        category: row.category,
        impact: row.impact,
        advisor_id: row.advisor_id,
        subscription_id: row.subscription_id,
      });
      state.linkDraft = '';
      applyLink(data.link, false);
    } catch (error) {
      failed = true;
      state.linkError = error.message;
    }
    state.linkBusy = false;
    renderDetail();
    if (failed) focusLinkInput();
  }

  function focusLinkInput() {
    const input = $('detail').querySelector('input');
    if (input) input.focus();
  }

  async function unlinkRow(row) {
    if (state.linkBusy || !row.link) return;
    if (!window.confirm('Unlink ' + row.link.jira_key + ' from this finding? The Jira issue itself is not changed.')) return;
    state.linkBusy = true;
    state.linkError = '';
    renderDetail();
    try {
      const data = await api('DELETE', '/api/links', {
        recommendation_type_id: row.recommendation_type_id,
        resource_id: row.resource_id,
      });
      applyLink(data.link, true);
    } catch (error) {
      state.linkError = error.message;
    }
    state.linkBusy = false;
    renderDetail();
  }

  // -- chat -----------------------------------------------------------------------------

  function normalizeChat(chat) {
    const value = chat && typeof chat === 'object' ? chat : {};
    return {
      enabled: value.enabled === true,
      reason: typeof value.reason === 'string' ? value.reason : null,
      wedged: value.wedged === true,
      transcript: Array.isArray(value.transcript) ? value.transcript.slice() : [],
      run: value.run && typeof value.run === 'object' ? value.run : null,
      progress: Array.isArray(value.progress) ? value.progress.map(String) : [],
    };
  }

  function entryNode(entry) {
    const role = entry.role === 'user' || entry.role === 'assistant' || entry.role === 'error' ? entry.role : 'note';
    const node = el('div', { class: 'entry entry-' + role });
    if (ROLE_LABEL[role]) node.append(el('div', { class: 'entry-role', text: ROLE_LABEL[role] }));
    node.append(el('div', { class: 'entry-text', text: String(entry.text) }));
    return node;
  }

  function nearBottom(node) {
    return node.scrollHeight - node.scrollTop - node.clientHeight < STICK_TO_BOTTOM_PX;
  }

  function renderTranscript(forceBottom) {
    const box = $('transcript');
    const stick = forceBottom || nearBottom(box);
    box.replaceChildren(...state.chat.transcript.map(entryNode));
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function renderProgress() {
    const lines = state.chat.progress.map((line) => el('li', { text: line }));
    $('progress').replaceChildren(...lines);
    const box = $('progress-box');
    box.scrollTop = box.scrollHeight;
  }

  function renderChatControls() {
    const chat = state.chat;
    const usable = chat.enabled && !chat.wedged;
    const running = chat.run !== null;
    const input = $('chat-input');
    input.disabled = !usable || state.sending;
    $('chat-send').disabled = !usable || running || state.sending || input.value.trim() === '';
    const cancel = $('chat-cancel');
    cancel.disabled = !running || (chat.run && chat.run.state === 'cancelling');
    cancel.textContent = chat.run && chat.run.state === 'cancelling' ? 'Cancelling...' : 'Cancel';
    $('chat-reset').disabled = !chat.enabled || running;
    $('progress-box').hidden = !running;
    $('run-state').textContent = chat.run && chat.run.state === 'cancelling' ? '(cancelling)' : '';

    let note = state.chatError;
    if (!chat.enabled) {
      note = 'Chat is not available' + (chat.reason ? ': ' + chat.reason : '.');
    } else if (chat.wedged) {
      note = 'The previous run did not stop after a cancel. Restart the server to chat again.';
    }
    const noteBox = $('chat-note');
    noteBox.textContent = note;
    noteBox.hidden = note === '';
  }

  function renderChat() {
    renderTranscript(true);
    renderProgress();
    renderChatControls();
  }

  function addEntry(entry) {
    if (!entry || typeof entry !== 'object') return;
    if (state.chat.transcript.some((item) => item.id === entry.id)) return;
    state.chat.transcript.push(entry);
    renderTranscript(entry.role === 'user');
  }

  function onChatStarted(data) {
    state.chat.run = data.run || { id: 0, state: 'running' };
    state.chat.progress = [];
    state.chatError = '';
    addEntry(data.entry);
    renderProgress();
    renderChatControls();
  }

  function onChatProgress(data) {
    if (!state.chat.run || state.chat.run.id !== data.run_id) return;
    state.chat.progress.push(String(data.line));
    if (state.chat.progress.length > MAX_LOG_LINES) state.chat.progress.shift();
    const list = $('progress');
    list.append(el('li', { text: String(data.line) }));
    while (list.children.length > MAX_LOG_LINES) list.firstElementChild.remove();
    const box = $('progress-box');
    box.scrollTop = box.scrollHeight;
  }

  function onChatFinished(data) {
    if (state.chat.run && state.chat.run.id === data.run_id) state.chat.run = null;
    state.chat.wedged = false;
    addEntry(data.entry);
    renderChatControls();
  }

  function onChatState(data) {
    if (data.run) state.chat.run = data.run;
    if (data.wedged === true) state.chat.wedged = true;
    renderChatControls();
  }

  function onChatReset() {
    state.chat.transcript = [];
    state.chat.progress = [];
    state.chat.run = null;
    state.chatError = '';
    renderChat();
  }

  async function sendChat() {
    const input = $('chat-input');
    const text = input.value.trim();
    if (!text || state.sending || state.chat.run) return;
    // A retry of the same text keeps its id, so the server can tell it is the same message.
    if (!state.pendingMessage || state.pendingMessage.text !== text) {
      state.pendingMessage = { id: newMessageId(), text };
    }
    state.sending = true;
    state.chatError = '';
    renderChatControls();
    try {
      await api('POST', '/api/chat', { text, client_msg_id: state.pendingMessage.id });
      state.pendingMessage = null;
      if (input.value.trim() === text) input.value = '';
    } catch (error) {
      state.chatError = error.message;
    }
    state.sending = false;
    renderChatControls();
    if (!input.disabled) input.focus();
  }

  async function cancelChat() {
    try {
      await api('POST', '/api/chat/cancel', {});
    } catch (error) {
      state.chatError = error.message;
      renderChatControls();
    }
  }

  async function resetChat() {
    try {
      await api('POST', '/api/chat/reset', {});
    } catch (error) {
      state.chatError = error.message;
      renderChatControls();
    }
  }

  // -- approvals ------------------------------------------------------------------------

  function withDeadline(approval) {
    const seconds = Number(approval.expires_in);
    return Object.assign({}, approval, { deadline: performance.now() + (Number.isFinite(seconds) ? seconds : 0) * 1000 });
  }

  function onApprovalRequested(data) {
    if (state.approvals.some((item) => item.id === data.id)) return;
    state.approvals.push(withDeadline(data));
    renderApprovals();
  }

  function onApprovalResolved(data) {
    const index = state.approvals.findIndex((item) => item.id === data.id);
    if (index === -1) return;
    const [removed] = state.approvals.splice(index, 1);
    if (data.outcome !== 'approved' && data.outcome !== 'denied') {
      notify(
        'A Jira approval request for ' + removed.server + ':' + removed.tool + ' ended without a decision (' + String(data.outcome) + '). The call was denied.',
        true
      );
    }
    renderApprovals();
  }

  function setApprovalButtons(enabled) {
    $('approval-deny').disabled = !enabled;
    $('approval-approve').disabled = !enabled;
  }

  function formatRemaining(milliseconds) {
    const total = Math.max(0, Math.ceil(milliseconds / 1000));
    const minutes = Math.floor(total / 60);
    return minutes + ':' + String(total % 60).padStart(2, '0');
  }

  function updateApprovalTimer() {
    const current = state.approvals[0];
    if (!current) return;
    const left = current.deadline - performance.now();
    $('approval-timer').textContent =
      left > 0 ? 'Denied automatically if there is no answer in ' + formatRemaining(left) + '.' : 'Time is up. The call is being denied.';
  }

  function stopApprovalTimers() {
    if (state.approvalTimer) clearInterval(state.approvalTimer);
    if (state.approvalEnableTimer) clearTimeout(state.approvalEnableTimer);
    state.approvalTimer = null;
    state.approvalEnableTimer = null;
  }

  function renderApprovals() {
    const dialog = $('approval-dialog');
    const current = state.approvals[0];
    document.title = current ? '(!) Approval needed - Azure Advisor' : 'Azure Advisor';
    if (!current) {
      stopApprovalTimers();
      state.shownApproval = null;
      state.approvalBusy = false;
      if (dialog.open) dialog.close();
      return;
    }

    const counter = $('approval-counter');
    counter.hidden = state.approvals.length < 2;
    counter.textContent = state.approvals.length + ' requests are waiting. This is the first.';

    const isNew = state.shownApproval !== current.id;
    if (isNew) {
      stopApprovalTimers();
      state.shownApproval = current.id;
      state.approvalBusy = false;
      $('approval-tool').textContent = String(current.server) + ':' + String(current.tool);
      let text;
      try {
        text = JSON.stringify(current.arguments, null, 2);
      } catch (error) {
        text = String(current.arguments);
      }
      $('approval-args').textContent = text === undefined ? '(no arguments)' : text;
      $('approval-error').hidden = true;
      // Disabled for a moment so that a click aimed at something else cannot approve.
      setApprovalButtons(false);
      state.approvalEnableTimer = setTimeout(() => {
        state.approvalEnableTimer = null;
        if (!state.approvalBusy) setApprovalButtons(true);
      }, APPROVAL_ENABLE_DELAY_MS);
      updateApprovalTimer();
      state.approvalTimer = setInterval(updateApprovalTimer, 500);
    }
    if (!dialog.open) dialog.showModal();
    if (isNew) $('approval-deny').focus();
  }

  async function decideApproval(approve) {
    const current = state.approvals[0];
    if (!current || state.approvalBusy) return;
    state.approvalBusy = true;
    setApprovalButtons(false);
    $('approval-error').hidden = true;
    let keepDisabled = true;
    try {
      await api('POST', '/api/approvals/' + encodeURIComponent(current.id), { decision: approve ? 'approve' : 'deny' });
      $('approval-timer').textContent = 'Your answer was sent.';
    } catch (error) {
      if (error.status === 404 || error.status === 409) {
        const outcome = error.data && typeof error.data.outcome === 'string' ? ' (' + error.data.outcome + ')' : '';
        state.approvals = state.approvals.filter((item) => item.id !== current.id);
        notify('That request had already ended' + outcome + '.', true);
        renderApprovals();
      } else {
        keepDisabled = false;
        const box = $('approval-error');
        box.textContent = error.message;
        box.hidden = false;
      }
    }
    state.approvalBusy = false;
    if (!keepDisabled) setApprovalButtons(true);
  }

  // -- wiring ---------------------------------------------------------------------------

  function wire() {
    setOptions($('f-category'), CATEGORIES, 'All');
    setOptions($('f-impact'), IMPACTS, 'All');
    setOptions($('f-status'), STATUSES, 'Default (New)');

    for (const [id, name] of [['f-category', 'category'], ['f-impact', 'impact'], ['f-status', 'status']]) {
      $(id).addEventListener('change', (event) => {
        state.filters[name] = event.target.value;
        loadRecommendations(false);
      });
    }
    $('f-group').addEventListener('change', (event) => {
      state.filters.group = event.target.value;
      renderTable();
      markSelected();
    });
    $('f-text').addEventListener('input', (event) => {
      state.filters.text = event.target.value;
      renderTable();
    });
    $('subscription').addEventListener('change', (event) => {
      state.subscription = event.target.value;
      loadRecommendations(false);
    });
    $('refresh').addEventListener('click', () => loadRecommendations(true));
    $('azure-retry').addEventListener('click', retryAzure);

    $('table-head').addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button || !button.dataset.key) return;
      state.sort = { key: button.dataset.key, dir: state.sort.key === button.dataset.key ? -state.sort.dir : 1 };
      renderTable();
    });
    const body = $('table-body');
    body.addEventListener('click', (event) => {
      if (event.target.closest('a')) return;
      const tr = event.target.closest('tr');
      if (tr && tr.dataset.rowId) selectRow(tr.dataset.rowId);
    });
    body.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const tr = event.target.closest('tr');
      if (tr && tr.dataset.rowId && event.target === tr) {
        event.preventDefault();
        selectRow(tr.dataset.rowId);
      }
    });

    const input = $('chat-input');
    input.addEventListener('input', renderChatControls);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendChat();
      }
    });
    $('chat-send').addEventListener('click', sendChat);
    $('chat-cancel').addEventListener('click', cancelChat);
    $('chat-reset').addEventListener('click', resetChat);

    const dialog = $('approval-dialog');
    $('approval-approve').addEventListener('click', () => decideApproval(true));
    $('approval-deny').addEventListener('click', () => decideApproval(false));
    // Escape means "no": denying is always the safe answer.
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      decideApproval(false);
    });
    // A dialog that was closed while a request is still waiting is shown again.
    dialog.addEventListener('close', () => {
      if (state.approvals.length > 0) setTimeout(renderApprovals, 0);
    });
  }

  async function boot() {
    wire();
    renderAzure();
    renderToolbar();
    renderTable();
    renderDetail();
    renderChatControls();

    let loginFailure = '';
    if (location.hash) {
      const match = /^#t=([A-Za-z0-9_-]{1,200})$/.exec(location.hash);
      if (match) {
        try {
          await api('POST', '/api/login', { token: match[1] });
        } catch (error) {
          loginFailure = error.message;
        }
      }
      // Whatever it was, the token must not stay in the address bar or the history.
      history.replaceState(null, '', location.pathname + location.search);
    }
    try {
      await api('GET', '/api/session');
    } catch (error) {
      showSignin(loginFailure || (error.status === 401 ? null : error.message));
      return;
    }
    showApp();
    connect();
  }

  boot();
})();
