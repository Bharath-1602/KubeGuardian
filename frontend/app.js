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
  // Enter key to send
  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Fetch initial cluster status
  fetchClusterStatus();

  // Refresh cluster status every 30 seconds
  statusInterval = setInterval(fetchClusterStatus, 30000);
});


// ══════════════════════════════════════
// Message Sending
// ══════════════════════════════════════

/**
 * Send the current input message to the backend.
 */
async function sendMessage() {
  const text = messageInput.value.trim();
  if (!text || isLoading) return;

  // Hide welcome screen on first message
  if (welcomeScreen) {
    welcomeScreen.style.display = 'none';
  }

  // Add user bubble
  appendMessage('user', text);
  messageInput.value = '';
  setLoading(true);

  // Show thinking indicator
  const thinkingEl = showThinking();

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: SESSION_ID }),
    });

    const data = await response.json();

    // Remove thinking indicator
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
 * Send an example prompt when a chip is clicked.
 */
function sendExamplePrompt(chipEl) {
  messageInput.value = chipEl.textContent;
  sendMessage();
}


// ══════════════════════════════════════
// Message Rendering
// ══════════════════════════════════════

/**
 * Append a message bubble to the chat area.
 *
 * @param {'user'|'agent'} role - Message sender role.
 * @param {string} content - Message content (may contain markdown).
 * @param {boolean} isError - If true, renders with error styling.
 */
function appendMessage(role, content, isError = false) {
  const messageDiv = document.createElement('div');
  messageDiv.className = `message ${role}`;

  const avatarDiv = document.createElement('div');
  avatarDiv.className = 'message-avatar';
  avatarDiv.textContent = role === 'user' ? '👤' : '🛡️';

  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = `message-bubble${isError ? ' error' : ''}`;

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


/**
 * Render markdown-style formatting to HTML.
 *
 * Supports: **bold**, `inline code`, ```code blocks```,
 * tables, bullet/numbered lists, headings, horizontal rules.
 *
 * @param {string} text - Raw text with markdown.
 * @returns {string} - HTML string.
 */
function renderMarkdown(text) {
  if (!text) return '';

  let html = escapeHtml(text);

  // Code blocks (``` ... ```)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (match, lang, code) => {
    const id = 'code-' + Math.random().toString(36).substring(2, 8);
    return `<pre id="${id}"><code>${code.trim()}</code><button class="copy-btn" onclick="copyCode('${id}')">Copy</button></pre>`;
  });

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Italic
  html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');

  // Headings (### h3, ## h2, # h1) — process in reverse size
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Horizontal rule
  html = html.replace(/^---$/gm, '<hr>');

  // Tables
  html = renderTables(html);

  // Lists
  html = renderLists(html);

  // Status badges
  html = html.replace(/\b(Running|Healthy|SAFE|Ready)\b/g, '<span class="badge badge-running">$1</span>');
  html = html.replace(/\b(Failed|Error|NOT_SAFE|NotReady)\b/g, '<span class="badge badge-failed">$1</span>');
  html = html.replace(/\b(Pending|Warning|CONDITIONAL)\b/g, '<span class="badge badge-pending">$1</span>');

  // Line breaks — but not inside pre tags
  html = html.replace(/\n/g, '<br>');

  // Clean up excessive line breaks after block elements
  html = html.replace(/(<\/h[1-4]>)<br>/g, '$1');
  html = html.replace(/(<\/pre>)<br>/g, '$1');
  html = html.replace(/(<\/table>)<br>/g, '$1');
  html = html.replace(/(<\/ul>)<br>/g, '$1');
  html = html.replace(/(<\/ol>)<br>/g, '$1');
  html = html.replace(/<hr><br>/g, '<hr>');

  return html;
}


/**
 * Render markdown tables to HTML tables.
 */
function renderTables(html) {
  const lines = html.split('\n');
  let result = [];
  let inTable = false;
  let tableRows = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();

    // Check if line looks like a table row
    if (line.startsWith('|') && line.endsWith('|')) {
      // Check if it's a separator row
      if (/^\|[\s\-:|]+\|$/.test(line)) {
        continue; // Skip separator
      }
      if (!inTable) {
        inTable = true;
        tableRows = [];
      }
      const cells = line.split('|').filter(c => c.trim() !== '');
      tableRows.push(cells.map(c => c.trim()));
    } else {
      if (inTable) {
        result.push(buildTable(tableRows));
        inTable = false;
        tableRows = [];
      }
      result.push(lines[i]);
    }
  }
  if (inTable) {
    result.push(buildTable(tableRows));
  }

  return result.join('\n');
}


/**
 * Build an HTML table from parsed rows.
 */
function buildTable(rows) {
  if (rows.length === 0) return '';

  let html = '<table>';

  // First row is header
  html += '<thead><tr>';
  rows[0].forEach(cell => {
    html += `<th>${cell}</th>`;
  });
  html += '</tr></thead>';

  // Remaining rows
  if (rows.length > 1) {
    html += '<tbody>';
    for (let i = 1; i < rows.length; i++) {
      html += '<tr>';
      rows[i].forEach(cell => {
        html += `<td>${cell}</td>`;
      });
      html += '</tr>';
    }
    html += '</tbody>';
  }

  html += '</table>';
  return html;
}


/**
 * Render markdown lists to HTML lists.
 */
function renderLists(html) {
  const lines = html.split('\n');
  let result = [];
  let inUl = false;
  let inOl = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const ulMatch = line.match(/^[\s]*[-*]\s+(.+)$/);
    const olMatch = line.match(/^[\s]*\d+\.\s+(.+)$/);

    if (ulMatch) {
      if (!inUl) {
        inUl = true;
        result.push('<ul>');
      }
      result.push(`<li>${ulMatch[1]}</li>`);
    } else if (olMatch) {
      if (!inOl) {
        inOl = true;
        result.push('<ol>');
      }
      result.push(`<li>${olMatch[1]}</li>`);
    } else {
      if (inUl) {
        result.push('</ul>');
        inUl = false;
      }
      if (inOl) {
        result.push('</ol>');
        inOl = false;
      }
      result.push(line);
    }
  }
  if (inUl) result.push('</ul>');
  if (inOl) result.push('</ol>');

  return result.join('\n');
}


/**
 * Escape HTML special characters.
 */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}


/**
 * Copy code block content to clipboard.
 */
function copyCode(id) {
  const pre = document.getElementById(id);
  if (!pre) return;
  const code = pre.querySelector('code');
  if (!code) return;

  navigator.clipboard.writeText(code.textContent).then(() => {
    const btn = pre.querySelector('.copy-btn');
    const original = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = original; }, 1500);
  });
}


// ══════════════════════════════════════
// Approval Gate
// ══════════════════════════════════════

/**
 * Render an approval card in the chat area.
 *
 * @param {Object} actionPlan - The pre-check result from the backend.
 * @param {string} actionId - Unique ID for this pending action.
 */
function renderApprovalCard(actionPlan, actionId) {
  currentActionId = actionId;

  const riskClass = {
    'LOW': 'badge-running',
    'MEDIUM': 'badge-pending',
    'HIGH': 'badge-failed',
  }[actionPlan.risk_level] || 'badge-pending';

  const risksHtml = (actionPlan.pre_check_results || [])
    .map(r => `<li>${escapeHtml(r)}</li>`)
    .join('');

  const cardHtml = `
    <div class="approval-card" id="approval-${actionId}">
      <div class="approval-header">
        <span class="approval-icon">⚡</span>
        <span class="approval-title">Action Requires Approval</span>
        <span class="approval-risk">
          <span class="badge ${riskClass}">${actionPlan.risk_level || 'MEDIUM'} RISK</span>
        </span>
      </div>

      <div class="approval-details">
        <div class="approval-row">
          <span class="approval-label">Operation</span>
          <span class="approval-value"><strong>${escapeHtml(actionPlan.operation || '')}</strong></span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Target</span>
          <span class="approval-value"><code>${escapeHtml(actionPlan.target || '')}</code></span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Namespace</span>
          <span class="approval-value"><code>${escapeHtml(actionPlan.namespace || 'cluster')}</code></span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Current State</span>
          <span class="approval-value">${escapeHtml(actionPlan.current_state || 'N/A')}</span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Proposed Change</span>
          <span class="approval-value">${escapeHtml(actionPlan.proposed_change || '')}</span>
        </div>
        <div class="approval-row">
          <span class="approval-label">Impact</span>
          <span class="approval-value">${escapeHtml(actionPlan.impact || '')}</span>
        </div>
      </div>

      ${risksHtml ? `
        <div style="margin-bottom: 14px;">
          <span class="approval-label" style="display: block; margin-bottom: 6px;">Pre-Check Results</span>
          <ul class="approval-checklist">${risksHtml}</ul>
        </div>
      ` : ''}

      <div class="approval-actions">
        <button class="btn-approve" id="btn-approve-${actionId}" onclick="approveAction('${actionId}')">
          ✅ Approve
        </button>
        <button class="btn-cancel" id="btn-cancel-${actionId}" onclick="cancelAction('${actionId}')">
          ❌ Cancel
        </button>
      </div>
    </div>
  `;

  // Insert as a message-like element
  const wrapper = document.createElement('div');
  wrapper.className = 'message agent';
  
  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  avatar.textContent = '🛡️';
  
  wrapper.appendChild(avatar);

  const content = document.createElement('div');
  content.innerHTML = cardHtml;
  wrapper.appendChild(content.firstElementChild);

  chatArea.appendChild(wrapper);
  scrollToBottom();
}


/**
 * Approve a pending action.
 *
 * @param {string} actionId - UUID of the action to approve.
 */
async function approveAction(actionId) {
  const approveBtn = document.getElementById(`btn-approve-${actionId}`);
  const cancelBtn = document.getElementById(`btn-cancel-${actionId}`);

  if (approveBtn) {
    approveBtn.disabled = true;
    approveBtn.innerHTML = '⏳ Executing…';
  }
  if (cancelBtn) cancelBtn.disabled = true;

  try {
    const response = await fetch('/api/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: actionId, session_id: SESSION_ID }),
    });

    const data = await response.json();
    appendMessage('agent', data.message || 'Action executed.');
  } catch (error) {
    appendMessage('agent', `❌ **Approval error:** ${error.message}`, true);
  }

  if (approveBtn) approveBtn.innerHTML = '✅ Approved';
  currentActionId = null;
}


/**
 * Cancel a pending action.
 *
 * @param {string} actionId - UUID of the action to cancel.
 */
async function cancelAction(actionId) {
  const approveBtn = document.getElementById(`btn-approve-${actionId}`);
  const cancelBtn = document.getElementById(`btn-cancel-${actionId}`);

  if (approveBtn) approveBtn.disabled = true;
  if (cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.innerHTML = '🚫 Cancelling…';
  }

  try {
    const response = await fetch('/api/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: actionId, session_id: SESSION_ID }),
    });

    const data = await response.json();
    appendMessage('agent', data.message || 'Operation cancelled.');
  } catch (error) {
    appendMessage('agent', `❌ **Cancel error:** ${error.message}`, true);
  }

  if (cancelBtn) cancelBtn.innerHTML = '❌ Cancelled';
  currentActionId = null;
}


// ══════════════════════════════════════
// Cluster Status (Header Pill)
// ══════════════════════════════════════

/**
 * Fetch quick cluster status and update the header pill.
 * Called on load and every 30 seconds.
 */
async function fetchClusterStatus() {
  try {
    const response = await fetch('/api/cluster/quick-status');
    const data = await response.json();

    statusNodes.textContent = `${data.nodes || '—'} Nodes`;
    statusPods.textContent = `${data.pods || '—'} Pods`;

    const health = data.health || 'Unknown';
    statusHealth.textContent = `Status: ${health}`;

    // Update status dot
    statusDot.className = 'status-dot';
    if (health === 'Healthy') {
      // Default green — no extra class needed
    } else if (health === 'Warning') {
      statusDot.classList.add('warning');
    } else {
      statusDot.classList.add('error');
    }
  } catch (error) {
    // Non-blocking — keep last known state
    console.warn('Cluster status fetch failed:', error);
  }
}


// ══════════════════════════════════════
// UI Helpers
// ══════════════════════════════════════

/**
 * Show the thinking indicator in the chat area.
 * @returns {HTMLElement} The thinking element for later removal.
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
 * Remove a thinking indicator element.
 * @param {HTMLElement} el - The thinking element to remove.
 */
function removeThinking(el) {
  if (el && el.parentNode) {
    el.parentNode.removeChild(el);
  }
}


/**
 * Toggle loading state (disable/enable input).
 * @param {boolean} loading - Whether loading is active.
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
 * Scroll the chat area to the bottom smoothly.
 */
function scrollToBottom() {
  requestAnimationFrame(() => {
    chatArea.scrollTop = chatArea.scrollHeight;
  });
}
