/* ContractIQ UI logic */
const API = (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')
  ? `http://${window.location.hostname}:3000/api`
  : '/api';

let selectedFile    = null;
let activeContractId = null;
let activeListFilter = 'all';
const reviewDrafts = {};

const dropZone  = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('over');
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => { if (e.target.files.length) setFile(e.target.files[0]); });

function setFile(f) {
  selectedFile = f;
  const el = document.getElementById('fileChosen');
  el.textContent = f.name;
  el.style.display = 'block';
}

document.getElementById('uploadBtn').addEventListener('click', async () => {
  if (!selectedFile) { showUploadMsg('Please select a file first.', 'err'); return; }
  const reviewerEmail = (document.getElementById('reviewerEmail').value || '').trim();
  if (!reviewerEmail) { showUploadMsg('Please enter reviewer email.', 'err'); return; }

  const btn = document.getElementById('uploadBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span><span>Uploading…</span>';

  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('reviewer_email', reviewerEmail);

  try {
    const res = await fetch(`${API}/contracts`, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');

    showUploadMsg(`✓ Started — ID: ${data.contract_id}`, 'ok');
    selectedFile = null;
    const fc = document.getElementById('fileChosen');
    fc.textContent = '';
    fc.style.display = 'none';
    fileInput.value = '';

    await loadContracts();
    openContract(data.contract_id);
  } catch (e) {
    showUploadMsg(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>Upload & Start Review</span>';
  }
});

function showUploadMsg(msg, type) {
  const el = document.getElementById('uploadMsg');
  el.className = `upload-msg ${type}`;
  el.textContent = msg;
  if (type !== 'err') setTimeout(() => { el.className = 'upload-msg'; }, 7000);
}

async function loadContracts() {
  try {
    const res = await fetch(`${API}/contracts`);
    if (!res.ok) return;
    const { contracts } = await res.json();
    renderContractList(contracts);
    if (activeContractId) await refreshDetail(activeContractId);
  } catch (e) { console.warn('Poll error:', e); }
}

setInterval(loadContracts, 3000);

// Auto-open contract from URL param: ?contract=<id>
// This makes email/Slack review links work correctly.
(async () => {
  const params = new URLSearchParams(window.location.search);
  const urlContractId = params.get('contract');
  if (urlContractId) {
    activeContractId = urlContractId;
  }
  await loadContracts();
  if (urlContractId) {
    await openContract(urlContractId);
  }
})();

function renderContractList(contracts) {
  const el = document.getElementById('contractList');
  if (!contracts || !contracts.length) {
    el.innerHTML = '<p style="color:var(--text3);font-size:12px;line-height:1.8;">No contracts yet — upload one to begin.</p>';
    return;
  }
  const filtered = contracts.filter(c => matchesFilter(c.status));
  if (!filtered.length) {
    el.innerHTML = '<p style="color:var(--text3);font-size:12px;line-height:1.8;">No contracts match selected filter.</p>';
    return;
  }
  el.innerHTML = filtered.map(c => {
    const accentClass = c.status === 'approved' ? 'approved-card' : c.status === 'escalated' ? 'escalated-card' : ['pending_review','revision_requested','ingesting','analyzing'].includes(c.status) ? 'pending-card' : '';
    return `
    <div class="contract-card ${accentClass} ${activeContractId === c.contract_id ? 'active' : ''}"
         onclick="openContract('${c.contract_id}')">
      <div class="cc-name">${escHtml(c.name)}</div>
      <div class="cc-meta">
        <span class="badge ${statusBadgeClass(c.status)}">${statusLabel(c.status)}</span>
        ${c.risk_score != null ? `<span class="cc-score">${c.risk_score}/100</span>` : ''}
      </div>
    </div>`;
  }).join('');
}

function setContractFilter(nextFilter) {
  activeListFilter = nextFilter;
  ['all', 'review', 'approved', 'escalated'].forEach(f => {
    document.getElementById(`filter-${f}`)?.classList.toggle('active', f === nextFilter);
  });
  loadContracts();
}

function matchesFilter(status) {
  if (activeListFilter === 'all') return true;
  if (activeListFilter === 'approved') return status === 'approved';
  if (activeListFilter === 'escalated') return status === 'escalated';
  if (activeListFilter === 'review') return ['pending_review', 'revision_requested'].includes(status);
  return true;
}

function statusBadgeClass(s) {
  return {
    ingesting:          'badge-ingesting',
    analyzing:          'badge-analyzing',
    pending_review:     'badge-pending',
    approved:           'badge-approved',
    revision_requested: 'badge-revision',
    escalated:          'badge-escalated',
  }[s] || 'badge-ingesting';
}

function statusLabel(s) {
  return {
    ingesting:          'Ingesting',
    analyzing:          'AI Analyzing',
    pending_review:     'Needs Review',
    approved:           'Approved ✓',
    revision_requested: 'Revision Req.',
    escalated:          'Escalated ⚠',
  }[s] || s;
}

async function openContract(id) {
  activeContractId = id;
  try {
    const res = await fetch(`${API}/contracts`);
    const { contracts } = await res.json();
    renderContractList(contracts);
  } catch (_) {}
  await refreshDetail(id);
}

async function refreshDetail(id) {
  try {
    const res = await fetch(`${API}/contracts/${id}`);
    if (!res.ok) return;
    const c = await res.json();
    renderDetail(c);
  } catch (e) { console.warn('Detail refresh error:', e); }
}

const STEPS = [
  { id: 'upload',        label: 'Upload',      icon: '↑' },
  { id: 'ingest',        label: 'Ingest',       icon: '⟳' },
  { id: 'ai_analyze',   label: 'AI Analysis',  icon: '◈' },
  { id: 'human_review', label: 'User Review',  icon: '◉' },
  { id: 'decision',     label: 'Decision',      icon: '✓' },
];

function getPipelineStepClass(stepId, status) {
  const doneMap = {
    ingesting:          ['upload'],
    analyzing:          ['upload','ingest'],
    pending_review:     ['upload','ingest','ai_analyze'],
    approved:           ['upload','ingest','ai_analyze','human_review','decision'],
    revision_requested: ['upload','ingest','ai_analyze'],
    escalated:          ['upload','ingest','ai_analyze','human_review','decision'],
  };
  const activeMap = {
    ingesting:          'ingest',
    analyzing:          'ai_analyze',
    pending_review:     'human_review',
    approved:           null,
    revision_requested: 'human_review',
    escalated:          null,
  };
  const done   = doneMap[status] || [];
  const active = activeMap[status];
  if (done.includes(stepId)) return 'done';
  if (active === stepId) return 'active';
  if (status === 'escalated' && stepId === 'decision') return 'failed';
  return '';
}

function renderPipelineSteps(status) {
  return `
    <div class="pipeline-steps">
      <div class="steps-title">Pipeline Progress</div>
      <div class="steps-row">
        ${STEPS.map(s => {
          const cls = getPipelineStepClass(s.id, status);
          const icon = cls === 'done' ? '✓' : cls === 'failed' ? '!' : s.icon;
          return `
            <div class="step ${cls}">
              <div class="step-dot">${icon}</div>
              <div class="step-label">${s.label}</div>
            </div>`;
        }).join('')}
      </div>
    </div>`;
}

function renderRiskCard(c) {
  const score  = c.risk_score;
  const r = 38;
  const circ   = 2 * Math.PI * r;
  const offset = circ - (score / 100) * circ;
  const color  = score <= 30 ? 'var(--ok)' : score <= 60 ? 'var(--warn)' : 'var(--danger)';
  const lvl    = score <= 30 ? 'Low Risk' : score <= 60 ? 'Medium Risk' : 'High Risk';
  const cls    = score <= 30 ? 'risk-low' : score <= 60 ? 'risk-medium' : 'risk-high';
  const highN  = (c.clauses || []).filter(x => x.risk === 'high').length;
  const medN   = (c.clauses || []).filter(x => x.risk === 'medium').length;
  const lowN   = (c.clauses || []).filter(x => x.risk === 'low').length;

  return `
  <div class="risk-card">
    <div class="risk-gauge-side">
      <div class="risk-gauge">
        <svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
          <circle class="gauge-bg" cx="50" cy="50" r="${r}" />
          <circle class="gauge-fill" cx="50" cy="50" r="${r}"
            stroke="${color}"
            stroke-dasharray="${circ.toFixed(2)}"
            stroke-dashoffset="${offset.toFixed(2)}" />
        </svg>
        <div class="risk-num">${score}</div>
      </div>
    </div>
    <div class="risk-info-side">
      <div class="risk-level ${cls}">${lvl}</div>
      <div class="risk-chips">
        ${highN ? `<span class="risk-chip risk-chip-high">${highN} high</span>` : ''}
        ${medN  ? `<span class="risk-chip risk-chip-medium">${medN} medium</span>` : ''}
        ${lowN  ? `<span class="risk-chip risk-chip-low">${lowN} low</span>` : ''}
        <span class="risk-chip risk-chip-clauses">${(c.clauses||[]).length} clauses total</span>
      </div>
      <div class="risk-meta">Reviewer: ${escHtml(c.reviewer_email)}</div>
    </div>
  </div>`;
}

function renderReviewPanel(c) {
  const draft = reviewDrafts[c.contract_id] || {};
  const reviewerNameValue = draft.reviewerName || '';
  const reviewNotesValue = draft.reviewNotes || '';
  const revisionLimitReached = c.retry_count >= 3;
  const retryNote = c.retry_count > 0
    ? `<div class="retry-note">${revisionLimitReached ? '⛔ No more revision attempts — escalation required on next non-approve decision.' : `⟳ Revision loop: attempt ${c.retry_count} of 3`}</div>`
    : '';

  return `
  <div class="review-panel" id="reviewPanel">
    <div class="review-panel-header">
      <div>
        <div class="review-panel-title">
          <div class="review-icon">👤</div>
          User Review Required
        </div>
        <div class="review-panel-sub">AI analysis complete. Review the risk score and clause breakdown, then make your decision.</div>
      </div>
    </div>
    <div class="review-panel-body">
      ${retryNote}
      <div class="rv-field">
        <label>Your Name</label>
        <input type="text" id="reviewerName" value="${escHtml(reviewerNameValue)}" placeholder="e.g. Jane Smith" />
      </div>
      <div class="rv-field">
        <label>Review Notes & Instructions</label>
        <textarea id="reviewNotes" placeholder="Add your review notes, comments, or revision instructions…">${escHtml(reviewNotesValue)}</textarea>
      </div>
      <div class="action-row">
        <button class="btn-action btn-approve"  onclick="submitDecision('approve')">✅ Approve</button>
        <button class="btn-action btn-revise"   onclick="submitDecision('revise')" ${revisionLimitReached ? 'disabled' : ''}>🔄 ${revisionLimitReached ? 'No More Attempts' : 'Request Revision'}</button>
        <button class="btn-action btn-escalate" onclick="submitDecision('escalate')">⚠ Escalate to Legal</button>
      </div>
    </div>
  </div>`;
}

function dotClass(text) {
  const t = (text || '').toLowerCase();
  if (t.includes('approv')) return 'ok-dot';
  if (t.includes('escal') || t.includes('fail')) return 'danger-dot';
  if (t.includes('revis') || t.includes('analyz') || t.includes('ingest')) return 'warn-dot';
  return 'neutral-dot';
}

function renderDetail(c) {
  document.getElementById('emptyState').style.display = 'none';
  const panel = document.getElementById('detailPanel');
  panel.style.display = 'block';

  const existingReviewer = document.getElementById('reviewerName');
  const existingNotes = document.getElementById('reviewNotes');
  if (existingReviewer || existingNotes) {
    reviewDrafts[c.contract_id] = {
      reviewerName: existingReviewer?.value || '',
      reviewNotes: existingNotes?.value || '',
    };
  }

  const prevTab = panel.dataset.activeTab || 'analysis';
  const st = c.status;
  const isProcessing = ['ingesting','analyzing'].includes(st);
  const needsReview  = st === 'pending_review' || (st === 'revision_requested' && c.risk_score != null);
  const isApproved   = st === 'approved';
  const isEscalated  = st === 'escalated';
  const isRevision   = st === 'revision_requested';
  const hasClauses   = c.clauses && c.clauses.length > 0;

  panel.innerHTML = `
    <div class="detail-header">
      <div class="detail-header-stripe"></div>
      <div class="detail-header-top">
        <div>
          <div class="detail-title">${escHtml(c.name)}</div>
          <div class="detail-id">${c.contract_id}</div>
        </div>
        <div class="status-area">
          <span class="badge ${statusBadgeClass(st)}" style="font-size:11px;padding:5px 14px;">${statusLabel(st)}</span>
          ${c.retry_count > 0 ? `<span class="badge badge-revision">${c.retry_count >= 3 ? 'No more retries' : `Retry ${c.retry_count}/3`}</span>` : ''}
        </div>
      </div>
      <div class="detail-header-meta">
        <span class="meta-item"><span class="meta-item-icon">🕐</span>${formatDate(c.upload_date)}</span>
        ${c.risk_score != null ? `<span class="meta-item"><span class="meta-item-icon">◎</span>Risk score: ${c.risk_score}/100</span>` : ''}
        <span class="meta-item"><span class="meta-item-icon">✉</span>${escHtml(c.reviewer_email)}</span>
      </div>
    </div>

    ${renderPipelineSteps(st)}

    ${isProcessing ? `
    <div class="analyzing-banner">
      <div class="pulse-ring"></div>
      <span>${st === 'ingesting' ? 'Ingesting contract file…' : 'AI is extracting clauses and scoring risk…'} This takes a few seconds.</span>
    </div>` : ''}

    ${c.risk_score != null ? renderRiskCard(c) : ''}

    ${c.ai_summary ? `
    <div class="ai-summary">
      <div class="ai-summary-header">
        <div class="ai-dot"></div>
        <div class="ai-label">AI Summary</div>
      </div>
      <div class="ai-summary-body">${escHtml(c.ai_summary)}</div>
    </div>` : ''}

    ${isApproved ? `
    <div class="result-banner approved">
      <div class="result-banner-accent"></div>
      <div class="result-banner-body">
        <div class="rb-icon">✅</div>
        <div class="rb-body">
          <div class="rb-title">Contract Approved</div>
          <div class="rb-desc">Reviewed and approved by a user reviewer. All records have been updated.</div>
        </div>
      </div>
    </div>` : ''}

    ${isEscalated ? `
    <div class="result-banner escalated">
      <div class="result-banner-accent"></div>
      <div class="result-banner-body">
        <div class="rb-icon">⚠️</div>
        <div class="rb-body">
          <div class="rb-title">Escalated to Legal Counsel</div>
          <div class="rb-desc">Contract escalated. Jira ticket LEGAL-${(c.contract_id||'').slice(-4).toUpperCase()} created. Legal team notified.</div>
        </div>
      </div>
    </div>` : ''}

    ${isRevision && !needsReview ? `
    <div class="result-banner revision">
      <div class="result-banner-accent"></div>
      <div class="result-banner-body">
        <div class="rb-icon">🔄</div>
        <div class="rb-body">
          <div class="rb-title">Revision Requested — Re-analyzing</div>
          <div class="rb-desc">The reviewer requested changes. AI is re-processing the contract (attempt ${c.retry_count}).</div>
        </div>
      </div>
    </div>` : ''}

    ${needsReview ? renderReviewPanel(c) : ''}

    ${hasClauses ? `
    <div class="tabs">
      <div class="tab ${prevTab==='analysis' ? 'active':''}" onclick="switchTab('analysis')">Clause Analysis (${c.clauses.length})</div>
      <div class="tab ${prevTab==='timeline' ? 'active':''}" onclick="switchTab('timeline')">Audit Trail</div>
      ${c.reviews && c.reviews.length ? `<div class="tab ${prevTab==='reviews' ? 'active':''}" onclick="switchTab('reviews')">Review History (${c.reviews.length})</div>` : ''}
    </div>

    <div class="tab-content ${prevTab==='analysis' ? 'active':''}" id="tab-analysis">
      <div class="clauses-grid">
        ${c.clauses.map(cl => `
          <div class="clause-card risk-${cl.risk}">
            <div class="clause-name">${escHtml(cl.name)}</div>
            <div class="clause-type">${cl.type}</div>
            <div class="clause-summary">${escHtml(cl.summary)}</div>
            <span class="clause-risk-badge ${cl.risk}">${cl.risk} risk</span>
          </div>
        `).join('')}
      </div>
    </div>

    <div class="tab-content ${prevTab==='timeline' ? 'active':''}" id="tab-timeline">
      <div class="timeline-wrap">
        ${(c.timeline || []).slice().reverse().map(t => `
          <div class="tl-item">
            <div class="tl-left">
              <div class="tl-dot-new ${dotClass(t.detail)}"></div>
            </div>
            <div class="tl-body">
              <div class="tl-detail">${escHtml(t.detail)}</div>
              <div class="tl-ts">${formatDate(t.ts)}</div>
            </div>
          </div>
        `).join('')}
      </div>
    </div>

    ${c.reviews && c.reviews.length ? `
    <div class="tab-content ${prevTab==='reviews' ? 'active':''}" id="tab-reviews">
      <div class="review-history-wrap">
        ${c.reviews.slice().reverse().map(r => {
          const initials = (r.reviewer_name || 'RV').split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();
          return `
          <div class="rh-item">
            <div class="rh-top">
              <div class="rh-avatar">${initials}</div>
              <div>
                <div class="rh-reviewer">${escHtml(r.reviewer_name)}</div>
                <div class="rh-ts">${formatDate(r.timestamp)}</div>
              </div>
              <span class="badge ${r.action==='approve' ? 'badge-approved' : r.action==='revise' ? 'badge-revision' : 'badge-escalated'}" style="margin-left:auto;">${r.action}</span>
            </div>
            <div class="rh-notes">${escHtml(r.notes)}</div>
          </div>`;
        }).join('')}
      </div>
    </div>` : ''}
    ` : ''}
  `;

  panel.dataset.activeTab     = prevTab;
  panel.dataset.contractId    = c.contract_id;
  panel.dataset.wfRunId       = c.workflow_run_id;
  panel.dataset.reviewerEmail = c.reviewer_email || '';

  const reviewerInput = document.getElementById('reviewerName');
  const notesInput = document.getElementById('reviewNotes');
  if (reviewerInput && notesInput) {
    reviewerInput.addEventListener('input', (e) => {
      reviewDrafts[c.contract_id] = reviewDrafts[c.contract_id] || {};
      reviewDrafts[c.contract_id].reviewerName = e.target.value;
    });
    notesInput.addEventListener('input', (e) => {
      reviewDrafts[c.contract_id] = reviewDrafts[c.contract_id] || {};
      reviewDrafts[c.contract_id].reviewNotes = e.target.value;
    });
  }
}

async function submitDecision(action) {
  const panel    = document.getElementById('detailPanel');
  const wfRunId  = panel.dataset.wfRunId;
  const contractId = panel.dataset.contractId;

  if (!wfRunId) { alert('Workflow run ID not found.'); return; }

  const reviewer = (document.getElementById('reviewerName')?.value || '').trim();
  const notes    = (document.getElementById('reviewNotes')?.value  || '').trim() || '(no notes)';
  const reviewerEmail = panel.dataset.reviewerEmail || '';

  if (!reviewer) { alert('Please enter your name to proceed.'); return; }

  const btns = document.querySelectorAll('.btn-action');
  btns.forEach(b => { b.disabled = true; });

  try {
    const res = await fetch(`${API}/review/${wfRunId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, notes, reviewer_name: reviewer, reviewer_email: reviewerEmail }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Signal failed');
    delete reviewDrafts[contractId];
    await refreshDetail(contractId);
  } catch (e) {
    alert(`Error: ${e.message}`);
    btns.forEach(b => { b.disabled = false; });
    await refreshDetail(contractId);
  }
}

function switchTab(name) {
  const panel = document.getElementById('detailPanel');
  panel.dataset.activeTab = name;
  document.querySelectorAll('.tab').forEach(t => {
    const matches = (name === 'analysis' && t.textContent.startsWith('Clause'))
                 || (name === 'timeline' && t.textContent.startsWith('Audit'))
                 || (name === 'reviews'  && t.textContent.startsWith('Review History'));
    t.classList.toggle('active', matches);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === `tab-${name}`);
  });
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}
