const data = window.MOCK_PIPELINE;
let selectedPartner = data.partners[0];

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[c]));

const fmt = (value, digits = 1) => {
  if (value === null || value === undefined || value === "") return "unknown";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : esc(value);
};

const pill = (label, tone = "") => `<span class="pill ${tone}">${esc(label)}</span>`;

document.querySelectorAll(".nav button").forEach((button) => {
  button.addEventListener("click", () => {
    showView(button.dataset.view);
  });
});

document.getElementById("openRunbook").addEventListener("click", () => showView("runbook"));
document.getElementById("openCommandPalette").addEventListener("click", openCommandPalette);
document.getElementById("commandPalette").addEventListener("click", (event) => {
  if (event.target.id === "commandPalette") closeCommandPalette();
});
document.getElementById("commandSearch").addEventListener("input", renderCommandResults);
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openCommandPalette();
  }
  if (event.key === "Escape") {
    closeCommandPalette();
  }
});

function showView(viewId) {
  document.querySelectorAll(".nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === viewId);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("hidden", view.id !== viewId);
  });
}

function commandItems() {
  const views = [
    ["Overview", "overview", "Jump to pipeline summary"],
    ["Setup Wizard", "setup", "Review workspace readiness"],
    ["Runbook", "runbook", "See the next safe action"],
    ["Review Inbox", "inbox", "Triage human decisions"],
    ["Email Enrichment", "email", "Export missing-email partners and import Apollo results"],
    ["Review Queue", "queue", "Inspect outreach rows"],
    ["Restore Center", "restore", "Compare and restore batches"],
    ["Dry-Run Preview", "dryrun", "Preview writes before mutation"],
    ["Health", "health", "Inspect errors and future API contract"],
  ].map(([label, view, detail]) => ({ kind: "screen", label, detail, view }));
  const partners = data.partners.map((partner) => ({
    kind: "partner",
    label: partner.name,
    detail: `${partner.fund} · ${partner.status}`,
    view: "partner",
    partnerId: partner.id,
  }));
  const inbox = data.inbox.map((item) => ({
    kind: "review",
    label: `${item.type}: ${item.item}`,
    detail: item.next,
    view: "inbox",
  }));
  return [...views, ...partners, ...inbox];
}

function openCommandPalette() {
  document.getElementById("commandPalette").classList.remove("hidden");
  document.getElementById("commandSearch").value = "";
  renderCommandResults();
  document.getElementById("commandSearch").focus();
}

function closeCommandPalette() {
  document.getElementById("commandPalette").classList.add("hidden");
}

function renderCommandResults() {
  const query = document.getElementById("commandSearch").value.toLowerCase();
  const items = commandItems().filter((item) => {
    const blob = `${item.kind} ${item.label} ${item.detail}`.toLowerCase();
    return blob.includes(query);
  }).slice(0, 12);
  document.getElementById("commandResults").innerHTML = items.map((item) => `
    <button class="command-result" data-view-target="${esc(item.view)}" data-partner-target="${esc(item.partnerId || "")}">
      ${pill(item.kind, item.kind === "review" ? "warn" : item.kind === "partner" ? "ready" : "neutral")}
      <div>
        <strong>${esc(item.label)}</strong>
        <span>${esc(item.detail)}</span>
      </div>
    </button>
  `).join("") || `
    <div class="empty-state compact-state">
      <strong>No matching command</strong>
      <p>Try partner name, stage, review item, or screen name.</p>
    </div>
  `;
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const partnerId = button.dataset.partnerTarget;
      if (partnerId) {
        selectedPartner = data.partners.find((partner) => partner.id === partnerId) || selectedPartner;
        renderPartner();
      }
      showView(button.dataset.viewTarget);
      closeCommandPalette();
    });
  });
}

function renderMetrics() {
  const items = [
    ["Funds", data.counts.funds],
    ["Partners", data.counts.partners],
    ["Verified Signals", data.counts.verifiedSignals],
    ["Recommended", data.counts.recommended],
    ["Ready", data.counts.ready],
    ["Blocked", data.counts.blocked],
  ];
  return `<div class="metrics">${items.map(([label, value]) => `
    <article class="metric-card">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
    </article>
  `).join("")}</div>`;
}

function renderOverview() {
  const attention = [
    ...data.gates.map((gate) => ({ title: gate.title, detail: gate.action, tone: gate.level === "block" ? "danger" : "warn" })),
    ...data.reviewRows.filter((row) => row.issue).map((row) => ({
      title: `${row.partner}: ${row.issue}`,
      detail: row.status,
      tone: row.qa === "fail" ? "danger" : "warn",
    })),
  ];
  document.getElementById("overview").innerHTML = `
    ${renderMetrics()}
    <div class="overview-grid">
      <section class="panel">
        <div class="panel-head">
          <h2>Safety Gates</h2>
          ${pill("mock", "neutral")}
        </div>
        <div class="gate-list">
          ${data.gates.map((gate) => `
            <article class="gate ${gate.level}">
              <div>
                <strong>${esc(gate.title)}</strong>
                <p>${esc(gate.detail)}</p>
              </div>
              <span>${esc(gate.action)}</span>
            </article>
          `).join("")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>Next Safe Action</h2>
          ${pill("blocked", "danger")}
        </div>
        <p class="large-copy">Wait for Stage 7 to be regenerated after the scoring refactor, then re-open the review queue.</p>
        <div class="command-preview">
          <code>uv run scripts/07_generate_emails.py --top 10</code>
          <button class="ghost-button" type="button" disabled title="Disabled in static prototype">Copy</button>
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head">
        <h2>Needs Attention</h2>
        <span class="quiet">${attention.length} item(s)</span>
      </div>
      <div class="attention-list">
        ${attention.map((item) => `
          <article>
            ${pill(item.tone === "danger" ? "block" : "review", item.tone)}
            <div>
              <strong>${esc(item.title)}</strong>
              <span>${esc(item.detail)}</span>
            </div>
          </article>
        `).join("")}
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Pipeline Stages</h2>
        <span class="quiet">Read-only design preview</span>
      </div>
      ${renderStageTable()}
    </section>
  `;
}

function renderRunbook() {
  document.getElementById("runbook").innerHTML = `
    <div class="runbook-layout">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Guided Runbook</h2>
            <p>Designed for one safe next move at a time</p>
          </div>
          ${pill("static", "neutral")}
        </div>
        <div class="steps">
          ${data.runbook.map((step, index) => `
            <article class="step ${step.status}">
              <div class="step-index">${index + 1}</div>
              <div>
                <div class="step-title">
                  <strong>${esc(step.title)}</strong>
                  ${pill(step.status, step.status === "done" ? "ok" : step.status === "blocked" ? "danger" : "warn")}
                </div>
                <p>${esc(step.detail)}</p>
                <div class="command-preview compact">
                  <code>${esc(step.command)}</code>
                  <button class="ghost-button" type="button" disabled title="Disabled in static prototype">Copy</button>
                </div>
              </div>
            </article>
          `).join("")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>Readiness Checklist</h2>
          ${pill("mock", "neutral")}
        </div>
        <div class="checklist">
          ${data.setupChecklist.map((item) => `
            <div class="check ${item.state}">
              <span>${item.state === "done" ? "OK" : item.state === "warn" ? "!" : "X"}</span>
              <strong>${esc(item.label)}</strong>
            </div>
          `).join("")}
        </div>
        <div class="prototype-note">
          The real UI should source this from status/doctor JSON after the refactor,
          so the operator sees one truth across CLI and browser.
        </div>
      </section>
    </div>
  `;
}

function renderSetup() {
  const setup = data.setupWorkflow;
  document.getElementById("setup").innerHTML = `
    <div class="wizard-layout">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Setup Workflow</h2>
            <p>Start with the raise, then let the app build the investor workspace</p>
          </div>
          ${pill("mock", "neutral")}
        </div>
        <div class="wizard-steps">
          ${data.setupSteps.map((step, index) => `
            <article class="wizard-step ${step.state}">
              <span>${index + 1}</span>
              <div>
                <strong>${esc(step.name)}</strong>
                <p>${esc(step.summary)}</p>
              </div>
              ${pill(step.state, step.state === "done" ? "ok" : step.state === "blocked" ? "danger" : "warn")}
            </article>
          `).join("")}
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>First-Run Safety</h2>
          ${pill("recommended", "ok")}
        </div>
        <p class="large-copy">The first run stays read-only: generate investor recommendations and drafts, then stop before any external write.</p>
        <div class="choice-list">
          <label><input type="checkbox" checked disabled> Limit first run to 5 funds</label>
          <label><input type="checkbox" checked disabled> Use dry-run for Attio and Gmail</label>
          <label><input type="checkbox" checked disabled> Require human review before any sync</label>
          <label><input type="checkbox" disabled> Allow production writes</label>
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Tell Us About The Raise</h2>
          <p>This becomes the workspace profile, scoring context, and email positioning</p>
        </div>
        ${pill(setup.currentStep, "ok")}
      </div>
      <div class="setup-form-grid">
        <label>
          <span>Company</span>
          <input value="${esc(setup.profile.company)}" disabled>
        </label>
        <label>
          <span>Website</span>
          <input value="${esc(setup.profile.website)}" disabled>
        </label>
        <label>
          <span>Round</span>
          <select disabled><option>${esc(setup.profile.round)}</option></select>
        </label>
        <label>
          <span>Target raise</span>
          <input value="${esc(setup.profile.targetRaise)}" disabled>
        </label>
        <label>
          <span>First close timing</span>
          <input value="${esc(setup.profile.firstClose)}" disabled>
        </label>
        <label>
          <span>Location</span>
          <input value="${esc(setup.profile.location)}" disabled>
        </label>
        <label class="span-2">
          <span>One-liner</span>
          <textarea disabled>${esc(setup.profile.oneLiner)}</textarea>
        </label>
        <label class="span-2">
          <span>Traction</span>
          <textarea disabled>${esc(setup.profile.traction)}</textarea>
        </label>
        <label class="span-2">
          <span>Ideal customer</span>
          <textarea disabled>${esc(setup.profile.customer)}</textarea>
        </label>
        <label class="span-2">
          <span>Why now</span>
          <textarea disabled>${esc(setup.profile.whyNow)}</textarea>
        </label>
      </div>
    </section>
    <div class="wizard-layout">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Investor Targeting</h2>
            <p>The app can suggest sources after this, instead of forcing uploads first</p>
          </div>
          ${pill("guided", "neutral")}
        </div>
        <div class="target-grid">
          <div>
            <span>Stages</span>
            <div class="tag-list">${setup.investorFit.stages.map((tag) => pill(tag, "neutral")).join("")}</div>
          </div>
          <div>
            <span>Check size</span>
            <strong>${esc(setup.investorFit.checkSize)}</strong>
          </div>
          <div>
            <span>Geographies</span>
            <div class="tag-list">${setup.investorFit.geographies.map((tag) => pill(tag, "neutral")).join("")}</div>
          </div>
          <div class="span-2">
            <span>Target theses</span>
            <div class="tag-list">${setup.investorFit.thesisTags.map((tag) => pill(tag, "ok")).join("")}</div>
          </div>
          <div class="span-2">
            <span>Avoid</span>
            <div class="tag-list">${setup.investorFit.avoid.map((tag) => pill(tag, "warn")).join("")}</div>
          </div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Outreach Rules</h2>
            <p>Default to cautious until the operator explicitly unlocks more</p>
          </div>
          ${pill("no sends", "ok")}
        </div>
        <div class="choice-list">
          ${setup.outreachPolicy.map((item) => `
            <label><input type="checkbox" ${item.state === "on" ? "checked" : ""} disabled> ${esc(item.label)}</label>
          `).join("")}
        </div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Advanced Inputs</h2>
          <p>Useful later, but not required to begin the guided setup</p>
        </div>
        ${pill("optional", "neutral")}
      </div>
      <div class="advanced-inputs">
        <article>
          <strong>Investor list upload</strong>
          <p>Import a CSV if the founder already has a target list. Otherwise the app can start from thesis and stage fit.</p>
          <button disabled>Upload CSV</button>
        </article>
        <article>
          <strong>Fund/source URLs</strong>
          <p>Add specific fund pages, RSS feeds, partner pages, or portfolio lists when a source should be required.</p>
          <button disabled>Add Source</button>
        </article>
        <article>
          <strong>Email enrichment</strong>
          <p>Export missing-email partners for Apollo after the app has produced the initial partner list.</p>
          <button disabled>Prepare Apollo Export</button>
        </article>
      </div>
    </section>
  `;
}

function renderInbox() {
  document.getElementById("inbox").innerHTML = `
    <div class="toolbar">
      <input id="inboxSearch" placeholder="Search review items">
      <select id="inboxFilter">
        <option value="">All review types</option>
        <option value="block">Blocking</option>
        <option value="warn">Warnings</option>
        <option value="review">Needs review</option>
      </select>
    </div>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Unified Review Inbox</h2>
          <p>One place for all human decisions</p>
        </div>
        ${pill(`${data.inbox.length} items`, "neutral")}
      </div>
      <div id="inboxRows"></div>
    </section>
  `;
  document.getElementById("inboxSearch").addEventListener("input", renderInboxRows);
  document.getElementById("inboxFilter").addEventListener("change", renderInboxRows);
  renderInboxRows();
}

function renderInboxRows() {
  const query = (document.getElementById("inboxSearch")?.value || "").toLowerCase();
  const filter = document.getElementById("inboxFilter")?.value || "";
  const rows = data.inbox.filter((row) => {
    const blob = `${row.type} ${row.owner} ${row.item} ${row.next}`.toLowerCase();
    return blob.includes(query) && (!filter || row.severity === filter);
  });
  document.getElementById("inboxRows").innerHTML = `
    <div class="inbox-list">
      ${rows.map((row) => `
        <article class="inbox-item ${row.severity}">
          ${pill(row.severity === "block" ? "blocker" : row.severity, row.severity === "block" ? "danger" : row.severity)}
          <div>
            <strong>${esc(row.type)}: ${esc(row.item)}</strong>
            <p>${esc(row.next)}</p>
          </div>
          <span>${esc(row.owner)}</span>
        </article>
      `).join("") || `<p class="quiet">No review items match.</p>`}
    </div>
  `;
}

function renderEmailEnrichment() {
  const e = data.emailEnrichment;
  document.getElementById("email").innerHTML = `
    <div class="email-layout">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Email Enrichment</h2>
            <p>Export missing-email partners, enrich in Apollo, import results</p>
          </div>
          ${pill("static", "neutral")}
        </div>
        <div class="email-steps">
          <article>
            <span>1</span>
            <div>
              <strong>Export partners needing email</strong>
              <p>${e.counts.missing} partner(s) missing email are ready for Apollo export.</p>
              <div class="column-list">${e.exportColumns.map((col) => `<code>${esc(col)}</code>`).join("")}</div>
            </div>
            <button disabled>Export CSV</button>
          </article>
          <article>
            <span>2</span>
            <div>
              <strong>Upload Apollo results</strong>
              <p>Expected columns are validated before any import can be applied.</p>
              <div class="column-list">${e.uploadColumns.map((col) => `<code>${esc(col)}</code>`).join("")}</div>
            </div>
            <button disabled>Choose CSV</button>
          </article>
          <article>
            <span>3</span>
            <div>
              <strong>Preview updates</strong>
              <p>${e.counts.valid} valid, ${e.counts.conflicts} requiring review, ${e.counts.uploaded} uploaded rows total.</p>
            </div>
            <button disabled>Apply Updates</button>
          </article>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>Apollo Import Preview</h2>
          ${pill("no writes", "ok")}
        </div>
        <table>
          <thead><tr><th>Partner</th><th>Fund</th><th>Email</th><th>Source</th><th>Confidence</th><th>Status</th><th>Issue</th></tr></thead>
          <tbody>
            ${e.rows.map((row) => `<tr>
              <td>${esc(row.partner)}</td>
              <td>${esc(row.fund)}</td>
              <td>${esc(row.email || "none")}</td>
              <td>${esc(row.source)}</td>
              <td>${pill(row.confidence, row.confidence === "high" ? "ok" : row.confidence === "medium" ? "warn" : "neutral")}</td>
              <td>${pill(row.status, row.status === "valid" ? "ok" : row.status === "review" ? "warn" : "neutral")}</td>
              <td>${esc(row.issue)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </section>
    </div>
  `;
}

function renderRestore() {
  document.getElementById("restore").innerHTML = `
    <div class="restore-layout">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>Run Comparison & Restore</h2>
            <p>Designed to make bad generations reversible</p>
          </div>
          ${pill("actions disabled", "ok")}
        </div>
        <table>
          <thead><tr><th>Batch</th><th>Status</th><th>Ready</th><th>QA failed</th><th>Generated</th><th>Note</th></tr></thead>
          <tbody>
            ${data.batches.map((batch) => `<tr>
              <td><code>${esc(batch.id)}</code></td>
              <td>${pill(batch.status, batch.status.includes("stale") ? "danger" : batch.status === "restorable" ? "ok" : "neutral")}</td>
              <td>${esc(batch.ready)}</td>
              <td>${esc(batch.qaFailed)}</td>
              <td class="quiet">${esc(batch.generated)}</td>
              <td>${esc(batch.note)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>Current vs Last Good</h2>
          ${pill("preview", "neutral")}
        </div>
        <div class="diff-list">
          ${data.batchDiffs.map((row) => `
            <div>
              <span>${esc(row.metric)}</span>
              <strong>${esc(row.previous)} -> ${esc(row.current)}</strong>
              ${pill(row.change, row.change.startsWith("+") ? "warn" : "ok")}
            </div>
          `).join("")}
        </div>
        <div class="decision-tray">
          <button disabled>Restore Last Good</button>
          <button disabled>Export Old CSV</button>
          <button disabled>Compare Drafts</button>
          <button disabled>Keep Current</button>
        </div>
      </section>
    </div>
  `;
}

function renderDryRun() {
  document.getElementById("dryrun").innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Dry-Run Preview</h2>
          <p>${esc(data.dryRun.title)}</p>
        </div>
        ${pill("no writes", "ok")}
      </div>
      <p class="large-copy">${esc(data.dryRun.summary)}</p>
      <div class="dryrun-blockers">
        ${data.dryRun.blockers.map((blocker) => `${pill(blocker, "danger")}`).join("")}
      </div>
      <table>
        <thead><tr><th>Record</th><th>Object</th><th>Operation</th><th>Fields</th><th>Risk</th></tr></thead>
        <tbody>
          ${data.dryRun.changes.map((change) => `<tr>
            <td>${esc(change.record)}</td>
            <td>${esc(change.object)}</td>
            <td>${pill(change.operation, change.operation === "CONFLICT" ? "danger" : change.operation === "SKIP" ? "neutral" : "ok")}</td>
            <td>${esc(change.fields)}</td>
            <td>${pill(change.risk, change.risk === "review" ? "warn" : "neutral")}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      <div class="decision-tray">
        <button disabled>Approve Writes</button>
        <button disabled>Export Preview</button>
        <button disabled>Resolve Conflicts</button>
        <button disabled>Cancel</button>
      </div>
    </section>
  `;
}

function renderStageTable() {
  return `<table>
    <thead><tr><th>Stage</th><th>State</th><th>Processed</th><th>OK</th><th>Failed</th><th>Skipped</th><th>Last run</th></tr></thead>
    <tbody>
      ${data.stages.map((stage) => `<tr>
        <td>${esc(stage.name)}</td>
        <td>${pill(stage.state, stage.state)}</td>
        <td>${esc(stage.processed)}</td>
        <td>${esc(stage.ok)}</td>
        <td>${esc(stage.failed)}</td>
        <td>${esc(stage.skipped)}</td>
        <td class="quiet">${esc(stage.last)}</td>
      </tr>`).join("")}
    </tbody>
  </table>`;
}

function renderQueue() {
  document.getElementById("queue").innerHTML = `
    <div class="toolbar">
      <input id="queueSearch" placeholder="Search partner, fund, issue">
      <select id="queueFilter">
        <option value="">All rows</option>
        <option value="ready_to_send">Ready</option>
        <option value="warm_path_needed">Warm path</option>
        <option value="draft">Draft</option>
        <option value="qa_failed">QA failed</option>
      </select>
    </div>
    <section class="panel">
      <div class="panel-head">
        <h2>Review Queue</h2>
        <span class="quiet">Mock CSV rows</span>
      </div>
      <div id="queueRows"></div>
    </section>
  `;
  document.getElementById("queueSearch").addEventListener("input", renderQueueRows);
  document.getElementById("queueFilter").addEventListener("change", renderQueueRows);
  renderQueueRows();
}

function renderQueueRows() {
  const query = (document.getElementById("queueSearch")?.value || "").toLowerCase();
  const filter = document.getElementById("queueFilter")?.value || "";
  const rows = data.reviewRows.filter((row) => {
    const blob = `${row.partner} ${row.fund} ${row.issue}`.toLowerCase();
    return blob.includes(query) && (!filter || row.status === filter);
  });
  document.getElementById("queueRows").innerHTML = `<table>
    <thead><tr><th>Partner</th><th>Fund</th><th>Status</th><th>QA</th><th>Priority</th><th>Issue</th></tr></thead>
    <tbody>
      ${rows.map((row) => `<tr class="selectable-row" data-queue-partner="${esc(row.partnerId)}">
        <td>${esc(row.partner)}</td>
        <td>${esc(row.fund)}</td>
        <td>${pill(row.status, row.status === "ready_to_send" ? "ready" : row.status === "qa_failed" ? "danger" : "warn")}</td>
        <td>${pill(row.qa, row.qa === "pass" ? "ok" : "danger")}</td>
        <td>${fmt(row.priority, 1)}</td>
        <td>${esc(row.issue)}</td>
      </tr>`).join("")}
    </tbody>
  </table>`;
  document.querySelectorAll("[data-queue-partner]").forEach((row) => {
    row.addEventListener("click", () => {
      const partner = data.partners.find((p) => p.id === row.dataset.queuePartner);
      if (partner) {
        selectedPartner = partner;
        renderPartner();
        showView("partner");
      }
    });
  });
}

function renderPartnerList() {
  return `<div class="partner-list">
    ${data.partners.map((partner) => `
      <button class="${partner.id === selectedPartner.id ? "selected" : ""}" data-partner-id="${esc(partner.id)}">
        <strong>${esc(partner.name)}</strong>
        <span>${esc(partner.fund)}</span>
        <span>${pill(partner.status, partner.status === "ready_to_send" ? "ready" : "warn")}</span>
      </button>
    `).join("")}
  </div>`;
}

function renderPartner() {
  document.getElementById("partner").innerHTML = `
    <div class="partner-grid">
      <section class="panel">${renderPartnerList()}</section>
      <section class="panel partner-detail">
        <div class="panel-head">
          <div>
            <h2>${esc(selectedPartner.name)}</h2>
            <p>${esc(selectedPartner.title)} at ${esc(selectedPartner.fund)}</p>
          </div>
          ${pill(selectedPartner.status, selectedPartner.status === "ready_to_send" ? "ready" : "warn")}
        </div>
        <div class="score-row">
          <div><span>Priority</span><strong>${fmt(selectedPartner.priority, 1)}</strong></div>
          <div><span>Composite</span><strong>${fmt(selectedPartner.composite, 1)}</strong></div>
          <div><span>Round</span><strong>${fmt(selectedPartner.roundFit, 1)}</strong></div>
          <div><span>Lead</span><strong>${fmt(selectedPartner.lead, 1)}</strong></div>
          <div><span>Reach</span><strong>${fmt(selectedPartner.reach, 1)}</strong></div>
        </div>
        <div class="evidence">
          <div class="section-head">
            <h3>Primary Hook</h3>
            ${pill("source attached", "ok")}
          </div>
          <p>${esc(selectedPartner.hook)}</p>
          <a href="${esc(selectedPartner.source)}">${esc(selectedPartner.source)}</a>
        </div>
        <div class="draft-block">
          <div class="section-head">
            <h3>${esc(selectedPartner.subject)}</h3>
            <button class="ghost-button" type="button" disabled title="Disabled in static prototype">Copy Draft</button>
          </div>
          <pre>${esc(selectedPartner.draft)}</pre>
        </div>
        <div class="objection">
          <h3>Likely Objection</h3>
          <p>${esc(selectedPartner.objection)}</p>
          ${selectedPartner.preempted ? pill("preempted", "ok") : pill("not preempted", "warn")}
        </div>
        <div class="decision-tray">
          <button disabled>Approve Draft</button>
          <button disabled>Needs Warm Intro</button>
          <button disabled>Set Email</button>
          <button disabled>Reject</button>
        </div>
      </section>
    </div>
  `;
  document.querySelectorAll("[data-partner-id]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedPartner = data.partners.find((partner) => partner.id === button.dataset.partnerId) || selectedPartner;
      renderPartner();
    });
  });
}

function renderHealth() {
  document.getElementById("health").innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>Recent Errors</h2>
        ${pill("mock", "neutral")}
      </div>
      <table>
        <thead><tr><th>When</th><th>Stage</th><th>Record</th><th>Message</th></tr></thead>
        <tbody>
          ${data.errors.map((error) => `<tr>
            <td class="quiet">${esc(error.when)}</td>
            <td>${esc(error.stage)}</td>
            <td>${esc(error.record)}</td>
            <td>${esc(error.message)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Future Integration Contract</h2>
        ${pill("no database", "ok")}
      </div>
      <div class="contract-grid">
        <div><strong>GET /api/health</strong><span>status + doctor JSON</span></div>
        <div><strong>GET /api/review-queue</strong><span>current CSV or active batch</span></div>
        <div><strong>GET /api/partners/:id</strong><span>score, evidence, drafts</span></div>
        <div><strong>POST /api/actions/*</strong><span>later, after run locks</span></div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>Disabled Until Backend Is Stable</h2>
        ${pill("intentionally inert", "ok")}
      </div>
      <div class="disabled-actions">
        <button disabled>Run Stage</button>
        <button disabled>Sync Attio</button>
        <button disabled>Create Gmail Drafts</button>
        <button disabled>Write Manual Override</button>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head">
        <h2>State Lab</h2>
        ${pill("design states", "neutral")}
      </div>
      <div class="state-grid">
        ${data.states.map((item) => renderStateCard(item)).join("")}
      </div>
    </section>
  `;
}

function renderStateCard(item) {
  if (item.state === "loading") {
    return `
      <article class="state-card">
        <div class="skeleton wide"></div>
        <div class="skeleton"></div>
        <div class="skeleton short"></div>
      </article>
    `;
  }
  return `
    <article class="state-card ${item.state}">
      ${pill(item.state, item.state === "error" ? "danger" : "neutral")}
      <strong>${esc(item.title)}</strong>
      <p>${esc(item.detail)}</p>
      <button disabled>${esc(item.action)}</button>
    </article>
  `;
}

function renderAll() {
  renderOverview();
  renderSetup();
  renderRunbook();
  renderInbox();
  renderEmailEnrichment();
  renderQueue();
  renderPartner();
  renderRestore();
  renderDryRun();
  renderHealth();
}

renderAll();
