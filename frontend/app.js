/**
 * frontend/app.js
 * ────────────────────────────────────────
 * KubeGuardian — Vanilla JavaScript chat application.
 * Handles messaging, markdown rendering, approval gate flow,
 * and live cluster status updates.
 */

// ══════════════════════════════════════
// State
// ══════════════════════════════════════

let isLoading = false;
let currentActionId = null;
const SESSION_ID = 'session-' + Math.random().toString(36).substring(2, 10);
let statusInterval = null;


// ══════════════════════════════════════
// DOM References
// ══════════════════════════════════════

const chatArea = document.getElementById('chat-area');
const messageInput = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const welcomeScreen = document.getElementById('welcome-screen');
const statusDot = document.getElementById('status-dot');
const statusNodes = document.getElementById('status-nodes');
const statusPods = document.getElementById('status-pods');
const statusHealth = document.getElementById('status-health');


// ══════════════════════════════════════
// Initialization
// ══════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  // Enter key sends message (Shift+Enter does nothing special here)
  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Initial cluster status fetch
  fetchClusterStatus();

  // Refresh every 30 seconds
  statusInterval = setInterval(fetchClusterStatus, 30000);
});


// ══════════════════════════════════════
// Message Sending
// ══════════════════════════════════════

/**
 * Read the input, POST to /api/chat, handle the response.
 * Response is either a plain message or an APPROVAL_REQUIRED card.
 */
async function sendMessage() {
  const text = messageInput.value.trim();
  if (!text || isLoading) return;

  // Hide welcome screen on first message
  if (welcomeScreen) {
    welcomeScreen.style.display = 'none';
  }

  appendMessage('user', text);
  messageInput.value = '';
  setLoading(true);

  const thinkingEl = showThinking();

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: SESSION_ID }),
    });

    if (!response.ok) {
      throw new Error(`Server error ${response.status}: ${response.statusText}`);
    }

    const data = await response.json();
    removeThinking(thinkingEl);

    if (data.type === 'APPROVAL_REQUIRED') {
      renderApprovalCard(data.action_plan, data.action_id);
    } else {
      appendMessage('agent', data.message || 'No response received.');
    }

  } catch (error) {
    removeThinking(thinkingEl);
    appendMessage('agent', `❌ **Connection error:** ${error.message}`, true);
  }

  setLoading(false);
}


/**
 * Fill the input with a chip's text and immediately send.
 * @param {HTMLElement} chipEl - The clicked chip button.
 */
function sendExamplePrompt(chipEl) {
  messageInput.value = chipEl.textContent.trim();
  sendMessage();
}


// ══════════════════════════════════════
// Message Rendering
// ══════════════════════════════════════

/**
 * Append a message bubble to the chat area.
 *
 * @param {'user'|'agent'} role
 * @param {string}         content  - Raw text (agent messages may contain markdown).
 * @param {boolean}        isError  - Apply error styling if true.
 */
function appendMessage(role, content, isError = false) {
  const messageDiv = document.createElement('div');
  messageDiv.className = `message ${role}`;

  const avatarDiv = document.createElement('div');
  avatarDiv.className = 'message-avatar';
  avatarDiv.textContent = role === 'user' ? '👤' : '🛡️';

  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = `message-bubble${isError ? ' error' : ''}`;

  // Agent responses get full markdown rendering;
  // user messages are plain text (XSS safe via textContent).
  if (role === 'agent') {
    bubbleDiv.innerHTML = renderMarkdown(content);
  } else {
    bubbleDiv.textContent = content;
  }

  messageDiv.appendChild(avatarDiv);
  messageDiv.appendChild(bubbleDiv);
  chatArea.appendChild(messageDiv);
  scrollToBottom();
}


// ══════════════════════════════════════
// Markdown Renderer
// ══════════════════════════════════════

/**
 * Convert markdown-ish text to safe HTML.
 *
 * Pipeline (order is critical):
 *   1. Extract ``` code blocks → placeholders  (BEFORE escaping)
 *   2. escapeHtml() the remaining text
 *   3. Restore code block placeholders
 *   4. Inline `code`
 *   5. **bold** and *italic*
 *   6. # Headings
 *   7. --- horizontal rule
 *   8. | Tables |
 *   9. - / 1. Lists
 *  10. Status badges
 *  11. \n → <br>
 *  12. Clean up double <br> after block elements
 *
 * @param {string} text
 * @returns {string} Safe HTML string.
 */
function renderMarkdown(text) {
  if (!text) return '';

  // ── 1. Extract fenced code blocks BEFORE any escaping ──────────
  // This prevents < > & inside code from being double-escaped,
  // and stops badge/bold regexes from touching code content.
  const codeBlocks = [];

  let processed = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_match, _lang, code) => {
    const id = 'code-' + Math.random().toString(36).substring(2, 8);
    const idx = codeBlocks.length;
    // Escape the code content individually so < > show correctly
    codeBlocks.push(
      `<pre id="${id}">` +
      `<code>${escapeHtml(code.trim())}</code>` +
      `<button class="copy-btn" onclick="copyCode('${id}')">Copy</button>` +
      `</pre>`
    );
    // Placeholder is plain ASCII — safe to run escapeHtml around it
    return `\x00CODEBLOCK${idx}\x00`;
  });

  // ── 2. Escape HTML in everything except placeholders ───────────
  processed = escapeHtml(processed);

  // ── 3. Restore code blocks ─────────────────────────────────────
  codeBlocks.forEach((block, i) => {
    // escapeHtml will have encoded \x00 as &#0; in some browsers —
    // use the literal null-byte placeholder pattern carefully.
    // Safer: use a string that escapeHtml won't touch (no < > & " ')
    processed = processed.replace(`\x00CODEBLOCK${i}\x00`, block);
  });

  // ── 4. Inline code ─────────────────────────────────────────────
  processed = processed.replace(/`([^`\n]+)`/g, '<code>$1</code>');

  // ── 5. Bold and italic ─────────────────────────────────────────
  processed = processed.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  processed = processed.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');

  // ── 6. Headings ────────────────────────────────────────────────
  processed = processed.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  processed = processed.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  processed = processed.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  processed = processed.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // ── 7. Horizontal rule ─────────────────────────────────────────
  processed = processed.replace(/^---$/gm, '<hr>');

  // ── 8. Tables ──────────────────────────────────────────────────
  processed = renderTables(processed);

  // ── 9. Lists ───────────────────────────────────────────────────
  processed = renderLists(processed);

  // ── 10. Status badges ──────────────────────────────────────────
  // Negative lookahead (?![^<]*>) prevents matching inside HTML tags.
  processed = processed.replace(
    /\b(Running|Healthy|SAFE|Ready)\b(?![^<]*>)/g,
    '<span class="badge badge-running">$1</span>'
  );
  processed = processed.replace(
    /\b(Failed|Error|NOT_SAFE|NotReady)\b(?![^<]*>)/g,
    '<span class="badge badge-failed">$1</span>'
  );
  processed = processed.replace(
    /\b(Pending|Warning|CONDITIONAL)\b(?![^<]*>)/g,
    '<span class="badge badge-pending">$1</span>'
  );

  // ── 11. Newlines → <br> ────────────────────────────────────────
  processed = processed.replace(/\n/g, '<br>');

  // ── 12. Remove redundant <br> immediately after block elements ─
  processed = processed.replace(
    /(<\/(?:h[1-4]|pre|table|ul|ol|hr|div)>)<br>/g,
    '$1'
  );
  processed = processed.replace(
    /<br>(<(?:table|ul|ol|pre|h[1-4]|hr)[\s>])/g,
    '$1'
  );
  processed = processed.replace(/<hr><br>/g, '<hr>');

  return processed;
}


// ══════════════════════════════════════
// Table Renderer
// ══════════════════════════════════════

/**
 * Find pipe-delimited table blocks in HTML text and convert them
 * to <table> elements.
 *
 * @param {string} html
 * @returns {string}
 */
function renderTables(html) {
  const lines = html.split('\n');
  const result = [];
  let inTable = false;
  let tableRows = [];

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();

    if (line.startsWith('|') && line.endsWith('|')) {
      // Skip separator rows: |---|---|  or  |:---:|:---:|
      if (/^\|[\s\-:|]+\|$/.test(line)) {
        continue;
      }

      if (!inTable) {
        inTable = true;
        tableRows = [];
      }

      const cells = line
        .split('|')
        .filter(c => c.trim() !== '')
        .map(c => c.trim());
      tableRows.push(cells);

    } else {
      if (inTable) {
        result.push(buildTable(tableRows));
        inTable = false;
        tableRows = [];
      }
      // Use raw (not trimmed) to preserve indentation
      result.push(raw);
    }
  }

  // Flush any table still open at end of text
  if (inTable && tableRows.length > 0) {
    result.push(buildTable(tableRows));
  }

  return result.join('\n');
}


/**
 * Build an HTML <table> from a 2-D array of cell strings.
 * First row becomes <thead>, remaining rows become <tbody>.
 *
 * @param {string[][]} rows
 * @returns {string} HTML string.
 */
function buildTable(rows) {
  if (rows.length === 0) return '';

  let html = '<table><thead><tr>';
  rows[0].forEach(cell => { html += `<th>${cell}</th>`; });
  html += '</tr></thead>';

  if (rows.length > 1) {
    html += '<tbody>';
    for (let i = 1; i < rows.length; i++) {
      html += '<tr>';
      rows[i].forEach(cell => { html += `<td>${cell}</td>`; });
      html += '</tr>';
    }
    html += '</tbody>';
  }

  return html + '</table>';
}


// ══════════════════════════════════════
// List Renderer
// ══════════════════════════════════════

/**
 * Convert markdown bullet (- * •) and numbered (1.) lists to
 * <ul> / <ol> HTML.  Handles switching between list types cleanly.
 *
 * @param {string} html
 * @returns {string}
 */
function renderLists(html) {
  const lines = html.split('\n');
  const result = [];
  let inUl = false;
  let inOl = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Unordered: lines starting with -, *, or •
    const ulMatch = line.match(/^(\s*)[-*•]\s+(.+)$/);
    // Ordered: lines starting with a digit followed by a period
    const olMatch = line.match(/^(\s*)\d+\.\s+(.+)$/);

    if (ulMatch) {
      // Close open ordered list before starting unordered
      if (inOl) { result.push('</ol>'); inOl = false; }
      if (!inUl) { result.push('<ul>'); inUl = true; }
      result.push(`<li>${ulMatch[2]}</li>`);

    } else if (olMatch) {
      // Close open unordered list before starting ordered
      if (inUl) { result.push('</ul>'); inUl = false; }
      if (!inOl) { result.push('<ol>'); inOl = true; }
      result.push(`<li>${olMatch[2]}</li>`);

    } else {
      // Non-list line — close any open list first
      if (inUl) { result.push('</ul>'); inUl = false; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      result.push(line);
    }
  }

  // Flush any still-open list at end of text
  if (inUl) result.push('</ul>');
  if (inOl) result.push('</ol>');

  return result.join('\n');
}


// ══════════════════════════════════════
// HTML Utilities
// ══════════════════════════════════════

/**
 * Escape < > & " ' to HTML entities.
 * Uses the browser's own serialiser — guaranteed correct.
 *
 * @param {string} text
 * @returns {string}
 */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}


/**
 * Copy the text content of a <pre> block to the clipboard.
 * Updates the button label briefly to confirm the copy.
 *
 * @param {string} id - The id attribute of the <pre> element.
 */
function copyCode(id) {
  const pre = document.getElementById(id);
  if (!pre) return;

  const code = pre.querySelector('code');
  if (!code) return;

  navigator.clipboard.writeText(code.textContent).then(() => {
    const btn = pre.querySelector('.copy-btn');
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = original; }, 1500);
  }).catch(() => {
    // Clipboard API unavailable (HTTP, old browser) — silent fail
  });
}


// ══════════════════════════════════════
// Approval Gate
// ══════════════════════════════════════

/**
 * Render a write-operation approval card into the chat area.
 *
 * The card shows the operation details, risk level, impact,
 * pre-check results, and two buttons: Approve and Cancel.
 *
 * @param {Object} actionPlan - Structured data from the backend pre-check.
 * @param {string} actionId   - UUID identifying this pending action.
 */
function renderApprovalCard(actionPlan, actionId) {
  currentActionId = actionId;

  // Map risk level to badge CSS class
  const riskClassMap = {
    LOW: 'badge-running',
    MEDIUM: 'badge-pending',
    HIGH: 'badge-failed',
  };
  const riskClass = riskClassMap[actionPlan.risk_level] || 'badge-pending';
  const riskLabel = (actionPlan.risk_level || 'MEDIUM') + ' RISK';

  // Build pre-check results list (risks)
  const risksHtml = (actionPlan.pre_check_results || [])
    .map(r => `<li>${escapeHtml(r)}</li>`)
    .join('');

  const cardHtml = `
    <div class="approval-card" id="approval-${actionId}">

      <div class="approval-header">
        <span class="approval-icon">⚡</span>
        <span class="approval-title">Action Requires Approval</span>
        <span class="approval-risk">
          <span class="badge ${riskClass}">${escapeHtml(riskLabel)}</span>
        </span>
      </div>

      <div class="approval-details">
        <div class="approval-row">
          <span class="approval-label">Operation</span>
          <span class="approval-value">
            <strong>${escapeHtml(actionPlan.operation || '')}</strong>
          </span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Target</span>
          <span class="approval-value">
            <code>${escapeHtml(actionPlan.target || '')}</code>
          </span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Namespace</span>
          <span class="approval-value">
            <code>${escapeHtml(actionPlan.namespace || 'cluster')}</code>
          </span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Current State</span>
          <span class="approval-value">
            ${escapeHtml(actionPlan.current_state || 'N/A')}
          </span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Proposed Change</span>
          <span class="approval-value">
            ${escapeHtml(actionPlan.proposed_change || '')}
          </span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Impact</span>
          <span class="approval-value">
            ${escapeHtml(actionPlan.impact || '')}
          </span>
        </div>
      </div>

      ${risksHtml ? `
        <div style="margin-bottom: 14px;">
          <span class="approval-label"
                style="display:block; margin-bottom:6px;">
            Pre-Check Results
          </span>
          <ul class="approval-checklist">${risksHtml}</ul>
        </div>
      ` : ''}

      <div class="approval-actions">
        <button
          class="btn-approve"
          id="btn-approve-${actionId}"
          onclick="approveAction('${actionId}')"
        >✅ Approve</button>
        <button
          class="btn-cancel"
          id="btn-cancel-${actionId}"
          onclick="cancelAction('${actionId}')"
        >❌ Cancel</button>
      </div>

    </div>
  `;

  // Wrap in a standard agent message row (avatar + card)
  const wrapper = document.createElement('div');
  wrapper.className = 'message agent';

  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.textContent = '🛡️';
  wrapper.appendChild(avatar);

  // Parse cardHtml — use firstElementChild to skip whitespace text nodes
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml;
  wrapper.appendChild(tmp.firstElementChild);

  chatArea.appendChild(wrapper);
  scrollToBottom();
}


/**
 * User clicked Approve — POST to /api/approve and show result.
 *
 * @param {string} actionId
 */
async function approveAction(actionId) {
  const approveBtn = document.getElementById(`btn-approve-${actionId}`);
  const cancelBtn = document.getElementById(`btn-cancel-${actionId}`);

  // Disable both buttons immediately to prevent double-click
  if (approveBtn) { approveBtn.disabled = true; approveBtn.innerHTML = '⏳ Executing…'; }
  if (cancelBtn) { cancelBtn.disabled = true; }

  try {
    const response = await fetch('/api/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: actionId, session_id: SESSION_ID }),
    });

    if (!response.ok) {
      throw new Error(`Server error ${response.status}`);
    }

    const data = await response.json();
    appendMessage('agent', data.message || '✅ Action executed.');

  } catch (error) {
    appendMessage('agent', `❌ **Approval error:** ${error.message}`, true);
  }

  // Update button label to reflect final state
  if (approveBtn) approveBtn.innerHTML = '✅ Approved';
  currentActionId = null;
}


/**
 * User clicked Cancel — POST to /api/cancel and show result.
 *
 * @param {string} actionId
 */
async function cancelAction(actionId) {
  const approveBtn = document.getElementById(`btn-approve-${actionId}`);
  const cancelBtn = document.getElementById(`btn-cancel-${actionId}`);

  // Disable both buttons immediately to prevent double-click
  if (approveBtn) { approveBtn.disabled = true; }
  if (cancelBtn) { cancelBtn.disabled = true; cancelBtn.innerHTML = '🚫 Cancelling…'; }

  try {
    const response = await fetch('/api/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: actionId, session_id: SESSION_ID }),
    });

    if (!response.ok) {
      throw new Error(`Server error ${response.status}`);
    }

    const data = await response.json();
    appendMessage('agent', data.message || '🚫 Operation cancelled.');

  } catch (error) {
    appendMessage('agent', `❌ **Cancel error:** ${error.message}`, true);
  }

  if (cancelBtn) cancelBtn.innerHTML = '❌ Cancelled';
  currentActionId = null;
}


// ══════════════════════════════════════
// Cluster Status Pill
// ══════════════════════════════════════

/**
 * Fetch /api/cluster/quick-status and update the header pill.
 * Called on page load and every 30 s.
 * Failures are silent — the pill keeps its last known value.
 */
async function fetchClusterStatus() {
  try {
    const response = await fetch('/api/cluster/quick-status');
    if (!response.ok) throw new Error('non-ok response');

    const data = await response.json();

    statusNodes.textContent = `${data.nodes || '—'} Nodes`;
    statusPods.textContent = `${data.pods || '—'} Pods`;

    const health = data.health || 'Unknown';
    statusHealth.textContent = `Status: ${health}`;

    // Reset dot class, then apply state-specific modifier
    statusDot.className = 'status-dot';
    if (health === 'Warning') {
      statusDot.classList.add('warning');
    } else if (health !== 'Healthy') {
      statusDot.classList.add('error');
    }
    // 'Healthy' keeps the default green pulse — no extra class needed

  } catch {
    // Non-blocking — keep last known state visible
    console.warn('Cluster status fetch failed (will retry in 30s)');
  }
}


// ══════════════════════════════════════
// UI Helpers
// ══════════════════════════════════════

/**
 * Insert an animated "thinking" indicator into the chat.
 * Call removeThinking() with the returned element when done.
 *
 * @returns {HTMLElement}
 */
function showThinking() {
  const el = document.createElement('div');
  el.className = 'message agent';
  el.innerHTML = `
    <div class="message-avatar">🛡️</div>
    <div class="thinking-indicator">
      <div class="thinking-dots">
        <span></span><span></span><span></span>
      </div>
    </div>
  `;
  chatArea.appendChild(el);
  scrollToBottom();
  return el;
}


/**
 * Remove a thinking indicator previously created by showThinking().
 *
 * @param {HTMLElement} el
 */
function removeThinking(el) {
  if (el && el.parentNode) {
    el.parentNode.removeChild(el);
  }
}


/**
 * Disable or re-enable the message input and send button.
 *
 * @param {boolean} loading
 */
function setLoading(loading) {
  isLoading = loading;
  messageInput.disabled = loading;
  sendBtn.disabled = loading;
  if (!loading) {
    messageInput.focus();
  }
}


/**
 * Scroll the chat area to the very bottom on the next animation frame.
 * Using rAF avoids layout thrashing.
 */
function scrollToBottom() {
  requestAnimationFrame(() => {
    chatArea.scrollTop = chatArea.scrollHeight;
  });
}