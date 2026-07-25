/* AgencyOps Orchestrator console
 * ---------------------------------------------------------------------------
 * Talks to the FastAPI surface, nothing else. No state lives here that the
 * server does not already own — a refresh re-reads everything from /runs, so
 * the console can never disagree with the orchestrator about what was sent.
 *
 * Accessibility decisions worth keeping:
 *   - Routes are real links; the router mirrors them into aria-current.
 *   - Every navigation moves focus to <main> so keyboard and screen-reader
 *     users land on the new view instead of the top of the document.
 *   - Errors render as a summary block with in-page links to the offending
 *     field, and the field itself gets aria-invalid + aria-describedby.
 *   - Status is announced through one polite live region, never a toast that
 *     disappears before it can be read.
 *   - Effect status is always carried by text, never by colour alone.
 * ------------------------------------------------------------------------- */
'use strict';

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape for interpolation into HTML. Everything user- or server-supplied
 *  goes through this before it reaches innerHTML. */
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const nf = new Intl.NumberFormat('en-AE');
const nf2 = new Intl.NumberFormat('en-AE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const num = (v) => nf.format(Math.round(Number(v) || 0));
const num2 = (v) => nf2.format(Number(v) || 0);
const money = (v, cur = 'AED') => `${cur} ${num(v)}`;
const pct = (v) => `${Number(v) >= 0 ? '+' : ''}${num2(v)}%`;

function dir(v) {
  const n = Number(v) || 0;
  return n > 0.001 ? 'up' : n < -0.001 ? 'down' : 'flat';
}

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// ---------------------------------------------------------------- API layer
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const isJson = (res.headers.get('content-type') || '').includes('application/json');
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = isJson && body && body.detail ? body.detail : `Request failed (${res.status})`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

const API = {
  health: () => api('/health'),
  clients: () => api('/clients'),
  runs: () => api('/runs'),
  run: (id) => api(`/runs/${encodeURIComponent(id)}`),
  report: (id) => api(`/runs/${encodeURIComponent(id)}/report`),
  clientReport: (payload) =>
    api('/workflows/client-report', { method: 'POST', body: JSON.stringify(payload) }),
  creative: (payload) =>
    api('/workflows/creative', { method: 'POST', body: JSON.stringify(payload) }),
  decide: (id, payload) =>
    api(`/runs/${encodeURIComponent(id)}/decision`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
};

// -------------------------------------------------------------- app state
const state = {
  health: null,
  clients: [],
  runs: [],
};

// ---------------------------------------------------------- status region
function announce(message, tone = 'info') {
  const bar = $('#statusbar');
  bar.hidden = !message;
  bar.dataset.tone = tone;
  bar.textContent = message || '';
}

// ------------------------------------------------------------- vocabulary
const RUN_STATUS = {
  running: ['Running', 'info'],
  blocked_on_approval: ['Awaiting approval', 'warn'],
  completed: ['Completed', 'ok'],
  completed_with_errors: ['Completed with errors', 'danger'],
  rejected: ['Rejected', 'neutral'],
  failed: ['Failed', 'danger'],
};

const EFFECT_STATUS = {
  proposed: ['Staged, not sent', 'warn'],
  approved: ['Approved', 'info'],
  executed: ['Sent', 'ok'],
  rejected: ['Rejected', 'neutral'],
  failed: ['Failed', 'danger'],
};

const SEVERITY = {
  high: ['High', 'danger'],
  medium: ['Medium', 'warn'],
  info: ['Informational', 'info'],
};

const WORKFLOW_LABEL = {
  client_report: 'Weekly client report',
  creative_pipeline: 'Creative production',
};

const ENGINE_LABEL = {
  ollama: ['Ollama', 'ok'],
  gemini: ['Gemini', 'ok'],
  offline: ['Offline engine', 'info'],
};

function badge(map, key) {
  const [label, tone] = map[key] || [String(key), 'neutral'];
  return `<span class="badge badge--${tone}">${esc(label)}</span>`;
}

// ------------------------------------------------------- markdown renderer
/** Renders the subset of markdown the report assembler emits. Input is
 *  escaped first, so no markup can survive from the source document. */
function renderMarkdown(md) {
  const lines = esc(md).split('\n');
  const out = [];
  let i = 0;

  const inline = (t) =>
    t
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|\s)_([^_]+)_/g, '$1<em>$2</em>');

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    const heading = line.match(/^(#{1,5})\s+(.*)$/);
    if (heading) {
      // Demote by one: the console already owns the page-level <h1>.
      const level = Math.min(heading[1].length + 1, 6);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (line.startsWith('|')) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith('|')) rows.push(lines[i++]);
      out.push(renderMarkdownTable(rows, inline));
      continue;
    }

    if (line.startsWith('&gt;')) {
      const quote = [];
      while (i < lines.length && lines[i].startsWith('&gt;')) {
        quote.push(lines[i++].replace(/^&gt;\s?/, ''));
      }
      out.push(`<blockquote>${inline(quote.join(' '))}</blockquote>`);
      continue;
    }

    if (/^\s*-\s+/.test(line)) {
      const [html, next] = renderMarkdownList(lines, i, inline);
      out.push(html);
      i = next;
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !/^[|#]|^\s*-\s|^&gt;/.test(lines[i])) {
      para.push(lines[i++]);
    }
    out.push(`<p>${inline(para.join(' '))}</p>`);
  }
  return out.join('\n');
}

function renderMarkdownTable(rows, inline) {
  const cells = (row) => row.split('|').slice(1, -1).map((c) => c.trim());
  const header = cells(rows[0]);
  const aligns = rows[1] && /^\|[\s:-]+\|/.test(rows[1])
    ? cells(rows[1]).map((c) => (c.endsWith(':') ? 'num' : ''))
    : header.map(() => '');
  const body = rows.slice(aligns.some(Boolean) || /^\|[\s:-]+\|/.test(rows[1] || '') ? 2 : 1);

  const th = header
    .map((c, n) => `<th scope="col" class="${aligns[n]}">${inline(c)}</th>`)
    .join('');
  const tr = body
    .map((row) => {
      const tds = cells(row)
        .map((c, n) => `<td class="${aligns[n]}">${inline(c)}</td>`)
        .join('');
      return `<tr>${tds}</tr>`;
    })
    .join('');
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>`;
}

function renderMarkdownList(lines, start, inline) {
  const indentOf = (l) => (l.match(/^\s*/) || [''])[0].length;
  const base = indentOf(lines[start]);
  let i = start;
  const items = [];

  while (i < lines.length && /^\s*-\s+/.test(lines[i])) {
    const indent = indentOf(lines[i]);
    if (indent < base) break;
    if (indent > base) {
      const [nested, next] = renderMarkdownList(lines, i, inline);
      items[items.length - 1] = `${items[items.length - 1]}${nested}`;
      i = next;
      continue;
    }
    items.push(inline(lines[i].replace(/^\s*-\s+/, '')));
    i++;
  }
  return [`<ul>${items.map((t) => `<li>${t}</li>`).join('')}</ul>`, i];
}

// ------------------------------------------------------- confirm dialog
function confirmDialog({ title, body, confirmLabel = 'Send now' }) {
  return new Promise((resolve) => {
    const dlg = $('#confirm-dialog');
    const form = dlg.querySelector('form');
    $('#confirm-title').textContent = title;
    $('#confirm-body').innerHTML = body;
    $('#confirm-ok').textContent = confirmLabel;

    // Resolved from three events because engines disagree about which ones a
    // method="dialog" submit produces — some fire `submit` and close without
    // ever firing `close`. Waiting on `close` alone leaves the promise
    // pending forever, and the approval silently does nothing.
    let settled = false;
    const finish = (approved) => {
      if (settled) return;
      settled = true;
      form.removeEventListener('submit', onSubmit);
      dlg.removeEventListener('cancel', onCancel);
      dlg.removeEventListener('close', onClose);
      if (dlg.open) dlg.close();
      resolve(approved);
    };
    const onSubmit = (event) => finish(!!event.submitter && event.submitter.value === 'confirm');
    const onCancel = () => finish(false);           // Escape key
    const onClose = () => finish(dlg.returnValue === 'confirm');

    form.addEventListener('submit', onSubmit);
    dlg.addEventListener('cancel', onCancel);
    dlg.addEventListener('close', onClose);

    dlg.returnValue = 'cancel';
    dlg.showModal(); // native modal: focus trap and Escape handling for free
  });
}

// ------------------------------------------------------------- form errors
/** Renders a linked error summary above a form and marks the bad fields. */
function showFormErrors(form, errors) {
  $$('.field__error', form).forEach((n) => n.remove());
  $$('[aria-invalid]', form).forEach((n) => {
    n.removeAttribute('aria-invalid');
    n.removeAttribute('aria-describedby');
  });
  const existing = $('.error-summary', form);
  if (existing) existing.remove();

  if (!errors.length) return;

  const list = errors
    .map((e) => `<li><a href="#${esc(e.field)}">${esc(e.message)}</a></li>`)
    .join('');
  const summary = document.createElement('div');
  summary.className = 'error-summary';
  summary.setAttribute('role', 'alert');
  summary.tabIndex = -1;
  summary.innerHTML = `
    <h3>${errors.length} ${errors.length === 1 ? 'problem' : 'problems'} to fix</h3>
    <ul>${list}</ul>`;
  form.prepend(summary);

  errors.forEach((e) => {
    const input = document.getElementById(e.field);
    if (!input) return;
    input.setAttribute('aria-invalid', 'true');
    const errId = `${e.field}-error`;
    const msg = document.createElement('span');
    msg.className = 'field__error';
    msg.id = errId;
    msg.textContent = e.message;
    input.insertAdjacentElement('afterend', msg);
    const describedBy = [input.dataset.hintId, errId].filter(Boolean).join(' ');
    input.setAttribute('aria-describedby', describedBy);
  });

  summary.focus();
}

function busy(button, isBusy, labelWhenBusy = 'Working…') {
  if (isBusy) {
    button.dataset.label = button.textContent;
    button.disabled = true;
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span>${esc(labelWhenBusy)}`;
  } else {
    button.disabled = false;
    button.textContent = button.dataset.label || button.textContent;
  }
}

// ================================================================== VIEWS
const view = () => $('#view');

function setView(html) {
  view().innerHTML = html;
  view().setAttribute('aria-busy', 'false');
}

// ----------------------------------------------------------- dashboard
function renderDashboard() {
  const h = state.health || {};
  const pending = state.runs.filter((r) => r.pending_approval);
  const staged = pending.reduce(
    (n, r) => n + r.effects.filter((e) => e.status === 'proposed').length,
    0
  );
  const sent = state.runs.reduce(
    (n, r) => n + r.effects.filter((e) => e.status === 'executed').length,
    0
  );

  setView(`
    <div class="page-head">
      <h1>Operations overview</h1>
      <p>
        Two workflows are wired: weekly client reporting and creative production.
        Both stage their outbound writes and stop here for a human decision.
      </p>
    </div>

    <div class="grid grid--3" role="group" aria-label="Key figures">
      ${statCard('Runs recorded', num(state.runs.length), 'Since the server started')}
      ${statCard('Awaiting approval', num(staged), `${pending.length} run${pending.length === 1 ? '' : 's'} paused`)}
      ${statCard('Writes released', num(sent), 'Effects actually dispatched')}
    </div>

    <div class="card" style="margin-top:1rem">
      <div class="card__head"><h2>Environment</h2></div>
      <div class="table-wrap">
        <table>
          <caption>How this instance is currently configured.</caption>
          <thead>
            <tr><th scope="col">Setting</th><th scope="col">Value</th><th scope="col">What it means</th></tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Connector mode</th>
              <td>${badge({ mock: ['Mock fixtures', 'info'], live: ['Live accounts', 'danger'] }, h.connector_mode)}</td>
              <td class="muted">${h.connector_mode === 'live'
                ? 'Approved effects hit real Meta, Harvest, Slack and Trello accounts.'
                : 'Approved effects are recorded, not sent. Safe to demo.'}</td>
            </tr>
            <tr>
              <th scope="row">LLM engine</th>
              <td>${badge(ENGINE_LABEL, h.llm_engine)}</td>
              <td class="muted">${h.llm_engine === 'offline'
                ? 'Deterministic, data-grounded generation. No API key needed.'
                : `Live generation via ${esc(h.llm_model || h.llm_engine)}. Figures are still computed in Python, never by the model. A provider outage degrades to the offline engine rather than failing the run.`}</td>
            </tr>
            <tr>
              <th scope="row">Approval gate</th>
              <td>${h.human_approval_required
                ? badge({ on: ['Enabled', 'ok'] }, 'on')
                : badge({ off: ['Disabled', 'danger'] }, 'off')}</td>
              <td class="muted">${h.human_approval_required
                ? 'Workflows halt before anything leaves the building.'
                : 'Workflows dispatch straight through. Not recommended outside tests.'}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid grid--2" style="margin-top:1rem">
      <div class="card">
        <h2>Weekly client report</h2>
        <p class="muted">
          Pulls Meta Ads performance and Harvest time tracking, computes
          week-on-week deltas in Python, has the model write commentary about
          those figures, then stages a Slack post plus a Trello card for every
          high-severity finding.
        </p>
        <p><a class="btn btn--sm" href="#/run">Run a report</a></p>
      </div>
      <div class="card">
        <h2>Creative production</h2>
        <p class="muted">
          Generates ad copy, scores it against the client's brand rules in code,
          sends failures back with their specific violations, and escalates
          anything still failing after two rounds to a human copywriter.
        </p>
        <p><a class="btn btn--sm" href="#/run">Generate copy</a></p>
      </div>
    </div>

    ${pending.length
      ? `<div class="callout callout--warn" style="margin-top:1rem">
           <h2>${staged} write${staged === 1 ? '' : 's'} waiting on you</h2>
           <p>Nothing has been sent. Review and release from the approvals queue.</p>
           <p><a class="btn btn--sm" href="#/approvals">Go to approvals</a></p>
         </div>`
      : ''}
  `);
}

function statCard(label, value, hint) {
  return `
    <div class="card stat">
      <span class="stat__label">${esc(label)}</span>
      <span class="stat__value">${esc(value)}</span>
      <span class="small muted">${esc(hint)}</span>
    </div>`;
}

// ------------------------------------------------------------ run forms
function renderRunView() {
  const reportClients = state.clients;
  const creativeClients = state.clients.filter((c) => c.creative_enabled);

  setView(`
    <div class="page-head">
      <h1>Run a workflow</h1>
      <p>
        Workflows run synchronously against the configured connectors. Whatever
        they produce is staged for approval — submitting a form here does not
        message a client.
      </p>
    </div>

    <div class="grid grid--2">
      <section class="card" aria-labelledby="report-form-title">
        <h2 id="report-form-title">Weekly client report</h2>
        <form id="report-form" novalidate>
          <div class="field">
            <label for="report-client">Client</label>
            <select id="report-client" name="client" required>
              ${reportClients
                .map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`)
                .join('')}
            </select>
          </div>

          <div class="field">
            <label for="report-window">Reporting window (days)</label>
            <span class="field__hint" id="report-window-hint">Between 1 and 90. Compared against the preceding window of the same length.</span>
            <input type="number" id="report-window" name="window_days" value="7" min="1" max="90"
                   inputmode="numeric" data-hint-id="report-window-hint" aria-describedby="report-window-hint">
          </div>

          <div class="field">
            <label for="report-channel">Slack channel <span class="muted">(optional)</span></label>
            <span class="field__hint" id="report-channel-hint">Leave blank to use the server default.</span>
            <input type="text" id="report-channel" name="channel" placeholder="#nova-retail-reporting"
                   data-hint-id="report-channel-hint" aria-describedby="report-channel-hint">
          </div>

          <button type="submit" class="btn">Generate report</button>
        </form>
      </section>

      <section class="card" aria-labelledby="creative-form-title">
        <h2 id="creative-form-title">Creative production</h2>
        <form id="creative-form" novalidate>
          <div class="field">
            <label for="creative-client">Client</label>
            <span class="field__hint" id="creative-client-hint">Only clients with brand rules on file can be run.</span>
            <select id="creative-client" name="client" required
                    data-hint-id="creative-client-hint" aria-describedby="creative-client-hint">
              ${creativeClients
                .map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`)
                .join('')}
            </select>
          </div>

          <div class="field">
            <label for="creative-product">Product or offer</label>
            <input type="text" id="creative-product" name="product" required
                   placeholder="Atlas Annual Membership">
          </div>

          <div class="field">
            <label for="creative-audience">Audience</label>
            <input type="text" id="creative-audience" name="audience" required
                   placeholder="first-time gym joiners in Dubai">
          </div>

          <div class="field">
            <label for="creative-benefit">Key benefit</label>
            <input type="text" id="creative-benefit" name="key_benefit" required
                   placeholder="a coached start, not a cold treadmill">
          </div>

          <div class="grid grid--2" style="gap:1rem">
            <div class="field">
              <label for="creative-cta">Call to action</label>
              <input type="text" id="creative-cta" name="cta" value="Start today">
            </div>
            <div class="field">
              <label for="creative-count">Variants</label>
              <input type="number" id="creative-count" name="variant_count" value="4" min="1" max="10"
                     inputmode="numeric">
            </div>
          </div>

          <button type="submit" class="btn">Generate copy</button>
        </form>
      </section>
    </div>

    <div id="run-result" aria-live="polite"></div>
  `);

  $('#report-form').addEventListener('submit', onReportSubmit);
  $('#creative-form').addEventListener('submit', onCreativeSubmit);
}

async function onReportSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);
  const client = $('#report-client').value;
  const windowDays = Number($('#report-window').value);
  const channel = $('#report-channel').value.trim();

  const errors = [];
  if (!client) errors.push({ field: 'report-client', message: 'Choose a client.' });
  if (!Number.isInteger(windowDays) || windowDays < 1 || windowDays > 90) {
    errors.push({ field: 'report-window', message: 'Reporting window must be a whole number between 1 and 90.' });
  }
  if (channel && !channel.startsWith('#')) {
    errors.push({ field: 'report-channel', message: 'Channel must start with #, or be left blank.' });
  }
  showFormErrors(form, errors);
  if (errors.length) return;

  busy(button, true, 'Running workflow…');
  announce('Running the weekly report workflow…');
  try {
    const run = await API.clientReport({
      client,
      window_days: windowDays,
      channel: channel || null,
    });
    await refreshRuns();
    const staged = run.effects.length;
    announce(
      `Report generated for ${run.label}. ${staged} write${staged === 1 ? '' : 's'} staged — nothing sent yet.`,
      'ok'
    );
    renderReportResult(run);
  } catch (err) {
    showFormErrors(form, [{ field: 'report-client', message: err.message }]);
    announce(`Report failed: ${err.message}`, 'danger');
  } finally {
    busy(button, false);
  }
}

async function onCreativeSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $('button[type="submit"]', form);

  const values = {
    client: $('#creative-client').value,
    product: $('#creative-product').value.trim(),
    audience: $('#creative-audience').value.trim(),
    key_benefit: $('#creative-benefit').value.trim(),
    cta: $('#creative-cta').value.trim() || 'Shop now',
    variant_count: Number($('#creative-count').value),
  };

  const errors = [];
  if (!values.client) errors.push({ field: 'creative-client', message: 'Choose a client.' });
  if (!values.product) errors.push({ field: 'creative-product', message: 'Name the product or offer.' });
  if (!values.audience) errors.push({ field: 'creative-audience', message: 'Describe the audience.' });
  if (!values.key_benefit) errors.push({ field: 'creative-benefit', message: 'State the key benefit.' });
  if (!Number.isInteger(values.variant_count) || values.variant_count < 1 || values.variant_count > 10) {
    errors.push({ field: 'creative-count', message: 'Ask for between 1 and 10 variants.' });
  }
  showFormErrors(form, errors);
  if (errors.length) return;

  busy(button, true, 'Generating…');
  announce('Generating and brand-checking ad copy…');
  try {
    const run = await API.creative(values);
    await refreshRuns();
    announce(
      `${run.approved_variants.length} variant(s) passed the brand check, ` +
        `${run.rejected_variants.length} rejected after ${run.revision_rounds} revision round(s).`,
      'ok'
    );
    renderCreativeResult(run);
  } catch (err) {
    showFormErrors(form, [{ field: 'creative-client', message: err.message }]);
    announce(`Creative run failed: ${err.message}`, 'danger');
  } finally {
    busy(button, false);
  }
}

function renderReportResult(run) {
  const t = run.totals || {};
  const d = run.deltas || {};
  const cur = t.currency || 'AED';
  const target = $('#run-result');

  target.innerHTML = `
    <section class="card" style="margin-top:1.5rem" aria-labelledby="result-title" tabindex="-1" id="result-panel">
      <div class="card__head">
        <h2 id="result-title">${esc(run.label)} — report drafted</h2>
        ${badge(RUN_STATUS, run.status)}
      </div>

      <div class="grid grid--3">
        ${metricStat('Spend', money(t.spend, cur), d.spend_pct)}
        ${metricStat('Revenue', money(t.revenue, cur), d.revenue_pct)}
        ${metricStat('ROAS', num2(t.roas), d.roas_pct)}
        ${metricStat('Conversions', num(t.conversions), d.conversions_pct)}
        ${metricStat('CPA', money(t.cpa, cur), d.cpa_pct, true)}
        ${metricStat('CTR', `${num2(t.ctr)}%`, d.ctr_pct)}
      </div>

      ${renderFindings(run.findings || [])}

      <p style="margin-top:1.5rem">
        <a class="btn btn--sm" href="#/runs/${encodeURIComponent(run.run_id)}">Open full run and report</a>
        ${run.pending_approval ? `<a class="btn btn--sm btn--ghost" href="#/approvals">Review ${run.effects.length} staged write(s)</a>` : ''}
      </p>
    </section>`;

  $('#result-panel').focus();
}

/** `inverted` marks metrics where a rise is bad (CPA). */
function metricStat(label, value, delta, inverted = false) {
  const d = dir(delta);
  const tone = d === 'flat' ? 'flat' : inverted ? (d === 'up' ? 'down' : 'up') : d;
  const word = d === 'up' ? 'up' : d === 'down' ? 'down' : 'unchanged';
  return `
    <div class="stat">
      <span class="stat__label">${esc(label)}</span>
      <span class="stat__value">${esc(value)}</span>
      <span class="stat__delta" data-dir="${tone}">
        ${esc(pct(delta))} <span class="muted">week on week (${word})</span>
      </span>
    </div>`;
}

function renderFindings(findings) {
  if (!findings.length) {
    return `<p class="muted" style="margin-top:1.5rem">No movement passed the materiality threshold this week.</p>`;
  }
  const items = findings
    .map(
      (f) => `
      <li class="variant">
        <div class="card__head" style="margin-bottom:.5rem">
          <h3 style="margin:0">${esc(f.headline)}</h3>
          ${badge(SEVERITY, f.severity)}
        </div>
        <p class="muted" style="margin:0">${esc(f.recommendation)}</p>
      </li>`
    )
    .join('');
  return `
    <h3 style="margin-top:1.5rem">What the workflow is acting on</h3>
    <ul style="list-style:none;margin:0;padding:0">${items}</ul>`;
}

function renderCreativeResult(run) {
  const target = $('#run-result');
  target.innerHTML = `
    <section class="card" style="margin-top:1.5rem" aria-labelledby="creative-result-title" tabindex="-1" id="result-panel">
      <div class="card__head">
        <h2 id="creative-result-title">${esc(run.label)} — copy generated</h2>
        ${badge(RUN_STATUS, run.status)}
      </div>
      <p class="muted">
        ${run.approved_variants.length} passed the brand check,
        ${run.rejected_variants.length} rejected after ${run.revision_rounds} revision round(s).
        Approved variants are staged as Trello cards; rejections are staged as a QA note.
      </p>
      ${renderVariants('Passed brand check', run.approved_variants)}
      ${renderVariants('Escalated to a copywriter', run.rejected_variants)}
      <p style="margin-top:1.5rem">
        <a class="btn btn--sm" href="#/runs/${encodeURIComponent(run.run_id)}">Open full run</a>
        ${run.pending_approval ? `<a class="btn btn--sm btn--ghost" href="#/approvals">Review ${run.effects.length} staged write(s)</a>` : ''}
      </p>
    </section>`;
  $('#result-panel').focus();
}

function renderVariants(title, variants) {
  if (!variants || !variants.length) return '';
  const items = variants
    .map(
      (v) => `
      <li class="variant">
        <p class="variant__headline">${esc(v.headline)}</p>
        <p class="muted" style="margin-bottom:.5rem">${esc(v.body)}</p>
        <p class="small muted" style="margin:0">Brand score ${esc(v.score)} of 100</p>
        <div class="meter" style="--pct:${Math.max(0, Math.min(100, Number(v.score) || 0))}%"
             role="img" aria-label="Brand score ${esc(v.score)} out of 100"></div>
        ${v.violations && v.violations.length
          ? `<ul class="violations">${v.violations.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`
          : ''}
      </li>`
    )
    .join('');
  return `
    <h3 style="margin-top:1.5rem">${esc(title)} (${variants.length})</h3>
    <ul style="list-style:none;margin:0;padding:0">${items}</ul>`;
}

// ------------------------------------------------------------ approvals
function renderApprovals() {
  const pending = state.runs.filter((r) => r.pending_approval);

  if (!pending.length) {
    setView(`
      <div class="page-head">
        <h1>Approvals</h1>
        <p>Staged writes wait here until someone releases them. Nothing expires and nothing sends itself.</p>
      </div>
      <div class="empty">
        <p><strong>Nothing is waiting.</strong></p>
        <p>Run a workflow and its outbound writes will queue here for review.</p>
        <p><a class="btn btn--sm" href="#/run">Run a workflow</a></p>
      </div>`);
    return;
  }

  const cards = pending.map(renderApprovalCard).join('');
  setView(`
    <div class="page-head">
      <h1>Approvals</h1>
      <p>
        ${pending.length} run${pending.length === 1 ? '' : 's'} paused. Release everything,
        or release part of a run and hold the rest — an account lead can send the client
        report while holding the internal action cards.
      </p>
    </div>
    ${cards}`);

  $$('[data-approval-form]').forEach((form) => {
    form.addEventListener('submit', onDecisionSubmit);
    $('[data-select-all]', form).addEventListener('click', () => {
      const boxes = $$('input[type="checkbox"]', form);
      const allOn = boxes.every((b) => b.checked);
      boxes.forEach((b) => { b.checked = !allOn; });
      announce(allOn ? 'All writes deselected.' : 'All writes selected.');
    });
  });
}

function renderApprovalCard(run) {
  const proposed = run.effects.filter((e) => e.status === 'proposed');
  const settled = run.effects.filter((e) => e.status !== 'proposed');
  const irreversible = proposed.filter((e) => !e.reversible).length;

  const choices = proposed
    .map(
      (e) => `
      <label class="choice">
        <input type="checkbox" name="effect" value="${e.index}" checked
               data-reversible="${e.reversible}" data-summary="${esc(e.summary)}">
        <span class="choice__body">
          <span class="choice__title">${esc(e.summary)}</span>
          <span class="small muted mono" style="display:block">${esc(e.connector)}.${esc(e.action)}</span>
          ${!e.reversible
            ? `<span class="badge badge--warn" style="margin-top:.35rem">Cannot be undone once sent</span>`
            : ''}
        </span>
      </label>`
    )
    .join('');

  return `
    <section class="card" aria-labelledby="run-${esc(run.run_id)}-title">
      <div class="card__head">
        <div>
          <h2 id="run-${esc(run.run_id)}-title" style="margin-bottom:.25rem">${esc(run.label)}</h2>
          <p class="small muted" style="margin:0">
            ${esc(WORKFLOW_LABEL[run.workflow] || run.workflow)} ·
            <span class="mono">${esc(run.run_id)}</span> ·
            ${esc(when(run.started_at))}
          </p>
        </div>
        ${badge(RUN_STATUS, run.status)}
      </div>

      <form data-approval-form data-run-id="${esc(run.run_id)}">
        <fieldset>
          <legend>
            ${proposed.length} write${proposed.length === 1 ? '' : 's'} staged
            ${irreversible ? `— ${irreversible} cannot be undone` : ''}
          </legend>
          ${choices}
        </fieldset>

        <div class="btn-row">
          <button type="submit" class="btn" name="decision" value="approve">Approve and send selected</button>
          <button type="submit" class="btn btn--ghost" name="decision" value="reject">Reject selected</button>
          <button type="button" class="btn btn--ghost btn--sm" data-select-all>Select / deselect all</button>
          <a class="btn btn--ghost btn--sm" href="#/runs/${encodeURIComponent(run.run_id)}">Inspect run</a>
        </div>
      </form>

      ${settled.length ? renderSettledEffects(settled) : ''}
    </section>`;
}

function renderSettledEffects(effects) {
  const rows = effects
    .map(
      (e) => `
      <tr>
        <td>${esc(e.summary)}</td>
        <td class="mono">${esc(e.connector)}.${esc(e.action)}</td>
        <td>${badge(EFFECT_STATUS, e.status)}</td>
      </tr>`
    )
    .join('');
  return `
    <details style="margin-top:1rem">
      <summary>Already decided (${effects.length})</summary>
      <div class="table-wrap" style="margin-top:.75rem">
        <table>
          <thead><tr><th scope="col">Write</th><th scope="col">Target</th><th scope="col">Status</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>`;
}

async function onDecisionSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const runId = form.dataset.runId;
  const decision = event.submitter ? event.submitter.value : 'approve';
  const boxes = $$('input[name="effect"]:checked', form);

  if (!boxes.length) {
    announce('Select at least one write before deciding.', 'danger');
    const first = $('input[name="effect"]', form);
    if (first) first.focus();
    return;
  }

  const indexes = boxes.map((b) => Number(b.value));
  const irreversible = boxes.filter((b) => b.dataset.reversible === 'false');

  if (decision === 'approve') {
    const list = irreversible.length
      ? `<p>${irreversible.length} of these cannot be undone once sent:</p><ul>${irreversible
          .map((b) => `<li>${esc(b.dataset.summary)}</li>`)
          .join('')}</ul>`
      : '';
    const ok = await confirmDialog({
      title: `Send ${indexes.length} write${indexes.length === 1 ? '' : 's'}?`,
      body: `${list}<p>This dispatches them to the configured connectors${
        state.health && state.health.connector_mode === 'live'
          ? ' — this instance is pointed at <strong>live accounts</strong>.'
          : '. This instance is in mock mode, so they are recorded rather than sent.'
      }</p>`,
      confirmLabel: 'Send now',
    });
    if (!ok) {
      announce('Nothing sent.');
      return;
    }
  }

  const button = event.submitter;
  busy(button, true, decision === 'approve' ? 'Sending…' : 'Rejecting…');
  try {
    const result = await API.decide(runId, { decision, effect_indexes: indexes });
    await refreshRuns();
    announce(
      decision === 'approve'
        ? `${result.dispatched} write${result.dispatched === 1 ? '' : 's'} sent` +
            (result.failed ? `, ${result.failed} failed.` : '. Nothing else was released.')
        : `${result.rejected} write${result.rejected === 1 ? '' : 's'} rejected. Nothing was sent.`,
      result.failed ? 'danger' : 'ok'
    );
    renderApprovals();
    $('#main').focus();
  } catch (err) {
    announce(`Decision failed: ${err.message}`, 'danger');
    busy(button, false);
  }
}

// ----------------------------------------------------------- run history
function renderRunsList() {
  if (!state.runs.length) {
    setView(`
      <div class="page-head"><h1>Run history</h1></div>
      <div class="empty">
        <p><strong>No runs yet.</strong></p>
        <p><a class="btn btn--sm" href="#/run">Run a workflow</a></p>
      </div>`);
    return;
  }

  const rows = state.runs
    .slice()
    .reverse()
    .map(
      (r) => `
      <tr>
        <td><a href="#/runs/${encodeURIComponent(r.run_id)}">${esc(r.label || r.run_id)}</a></td>
        <td>${esc(WORKFLOW_LABEL[r.workflow] || r.workflow)}</td>
        <td>${badge(RUN_STATUS, r.status)}</td>
        <td class="num">${esc(r.effects.filter((e) => e.status === 'executed').length)} / ${esc(r.effects.length)}</td>
        <td>${esc(when(r.started_at))}</td>
      </tr>`
    )
    .join('');

  setView(`
    <div class="page-head">
      <h1>Run history</h1>
      <p>Every run keeps a trace of what each node did and what it produced.</p>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <caption>Runs recorded by this server, newest first.</caption>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Workflow</th>
              <th scope="col">Status</th>
              <th scope="col" class="num">Writes sent</th>
              <th scope="col">Started</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`);
}

async function renderRunDetail(runId) {
  view().setAttribute('aria-busy', 'true');
  view().innerHTML = `<p class="muted">Loading run ${esc(runId)}…</p>`;

  let run;
  try {
    run = await API.run(runId);
  } catch (err) {
    setView(`
      <div class="page-head"><h1>Run not found</h1></div>
      <div class="callout callout--danger">
        <p>${esc(err.message)}</p>
        <p><a href="#/runs">Back to run history</a></p>
      </div>`);
    return;
  }

  const artifacts = run.artifacts || {};
  const steps = (run.trace && run.trace.steps) || [];

  const effectRows = run.effects
    .map(
      (e) => `
      <tr>
        <td>${esc(e.summary)}</td>
        <td class="mono">${esc(e.connector)}.${esc(e.action)}</td>
        <td>${e.reversible ? '<span class="muted">Reversible</span>' : '<span class="badge badge--warn">Irreversible</span>'}</td>
        <td>${badge(EFFECT_STATUS, e.status)}</td>
        <td>${e.result ? `<span class="mono small">${esc(JSON.stringify(e.result))}</span>` : '<span class="muted">—</span>'}</td>
      </tr>`
    )
    .join('');

  const timeline = steps
    .map(
      (s) => `
      <li>
        <span class="timeline__node">${esc(s.node)}</span>
        <span class="timeline__meta"> · ${esc(num2(s.duration_ms))} ms</span>
        <div>${esc(s.summary)}</div>
        ${s.detail && Object.keys(s.detail).length
          ? `<details><summary>Detail</summary><pre class="detail-json">${esc(JSON.stringify(s.detail, null, 2))}</pre></details>`
          : ''}
      </li>`
    )
    .join('');

  setView(`
    <div class="page-head">
      <p class="small"><a href="#/runs">← Run history</a></p>
      <h1>${esc(run.label || run.run_id)}</h1>
      <p>
        ${esc(WORKFLOW_LABEL[run.workflow] || run.workflow)} ·
        <span class="mono">${esc(run.run_id)}</span> ·
        ${esc(when(run.started_at))} ·
        ${badge(RUN_STATUS, run.status)}
      </p>
    </div>

    ${run.errors && run.errors.length
      ? `<div class="callout callout--danger" role="alert">
           <h2>This run failed</h2>
           <ul>${run.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>
         </div>`
      : ''}

    ${run.pending_approval
      ? `<div class="callout callout--warn">
           <p><strong>Staged writes are waiting.</strong> Nothing has left the building.</p>
           <p><a class="btn btn--sm" href="#/approvals">Review and release</a></p>
         </div>`
      : ''}

    <section class="card" aria-labelledby="effects-title">
      <div class="card__head"><h2 id="effects-title">Outbound writes</h2></div>
      ${run.effects.length
        ? `<div class="table-wrap"><table>
             <caption>Every external write this run proposed, and what happened to it.</caption>
             <thead><tr>
               <th scope="col">Write</th><th scope="col">Target</th>
               <th scope="col">Undo</th><th scope="col">Status</th><th scope="col">Result</th>
             </tr></thead>
             <tbody>${effectRows}</tbody>
           </table></div>`
        : '<p class="muted">This run staged no writes.</p>'}
    </section>

    ${renderArtifacts(artifacts)}

    <section class="card" aria-labelledby="trace-title">
      <div class="card__head"><h2 id="trace-title">Execution trace</h2></div>
      <p class="muted small">
        One entry per graph node, in the order they ran. Persisted to
        <span class="mono">runs/${esc(run.workflow)}-${esc(run.run_id)}.json</span>.
      </p>
      <ol class="timeline">${timeline}</ol>
    </section>
  `);
}

function renderArtifacts(artifacts) {
  const blocks = [];

  if (artifacts.report_markdown) {
    blocks.push(`
      <section class="card" aria-labelledby="report-title">
        <div class="card__head"><h2 id="report-title">Report document</h2></div>
        <div class="report">${renderMarkdown(artifacts.report_markdown)}</div>
      </section>`);
  }

  const approved = artifacts.approved_variants || [];
  const rejected = artifacts.rejected_variants || [];
  if (approved.length || rejected.length) {
    blocks.push(`
      <section class="card" aria-labelledby="variants-title">
        <div class="card__head"><h2 id="variants-title">Generated copy</h2></div>
        <p class="muted small">
          Scored in Python against the client's brand rules
          ${artifacts.revision_rounds ? `after ${esc(artifacts.revision_rounds)} revision round(s)` : ''}.
        </p>
        ${renderVariants('Passed brand check', approved)}
        ${renderVariants('Escalated to a copywriter', rejected)}
      </section>`);
  }

  return blocks.join('');
}

// ------------------------------------------------------------------ router
const ROUTES = {
  dashboard: renderDashboard,
  run: renderRunView,
  approvals: renderApprovals,
  runs: renderRunsList,
};

function currentRoute() {
  const hash = (location.hash || '#/dashboard').replace(/^#\/?/, '');
  const [name, ...rest] = hash.split('/');
  return { name: name || 'dashboard', param: rest.join('/') };
}

async function route() {
  const { name, param } = currentRoute();

  $$('.tabs__link').forEach((link) => {
    const active = link.dataset.route === name;
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });

  if (name === 'runs' && param) {
    await renderRunDetail(param);
  } else {
    (ROUTES[name] || renderDashboard)();
  }

  const heading = $('#view h1');
  document.title = `${
    heading
      ? heading.textContent.trim()
      : { dashboard: 'Overview', run: 'Run a workflow', approvals: 'Approvals', runs: 'Run history' }[name] ||
        'Console'
  } — AgencyOps Orchestrator`;

  $('#main').focus();
}

// ------------------------------------------------------------------- data
async function refreshRuns() {
  state.runs = await API.runs();
  const pending = state.runs.reduce(
    (n, r) => n + r.effects.filter((e) => e.status === 'proposed').length,
    0
  );
  const badgeEl = $('#pending-count');
  badgeEl.hidden = pending === 0;
  badgeEl.textContent = String(pending);
  badgeEl.setAttribute('aria-label', `${pending} writes awaiting approval`);
}

function paintEnvStrip() {
  const h = state.health || {};
  $('#env-connectors').textContent = h.connector_mode === 'live' ? 'Live accounts' : 'Mock fixtures';
  $('#env-engine').textContent = h.llm_model || (ENGINE_LABEL[h.llm_engine] || ['Unknown'])[0];
  $('#env-gate').textContent = h.human_approval_required ? 'Enabled' : 'Disabled';
}

// ------------------------------------------------------------------ theme
function initTheme() {
  const toggle = $('#theme-toggle');
  const stored = localStorage.getItem('agencyops-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    const dark = theme === 'dark';
    toggle.setAttribute('aria-pressed', String(dark));
    toggle.textContent = dark ? 'Light theme' : 'Dark theme';
  };
  apply(stored || (prefersDark ? 'dark' : 'light'));
  toggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('agencyops-theme', next);
    apply(next);
    announce(`${next === 'dark' ? 'Dark' : 'Light'} theme applied.`);
  });
}

// ------------------------------------------------------------------- boot
async function boot() {
  initTheme();
  window.addEventListener('hashchange', route);

  try {
    const [health, clients] = await Promise.all([API.health(), API.clients()]);
    state.health = health;
    state.clients = clients;
    paintEnvStrip();
    await refreshRuns();
  } catch (err) {
    setView(`
      <div class="callout callout--danger" role="alert">
        <h1>Cannot reach the orchestrator</h1>
        <p>${esc(err.message)}</p>
        <p class="muted">Start it with <span class="mono">uvicorn agencyops.api.main:app --app-dir src</span>.</p>
      </div>`);
    return;
  }

  await route();
}

boot();
