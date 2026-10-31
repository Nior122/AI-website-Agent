"""Mobile Operator Dashboard for Nior AI Website Agent.

Pure HTML5/CSS3/Vanilla JS embedded interface for operators to monitor, control,
and approve business pipeline tasks directly from their smartphone browser.
Zero external CDNs or runtime dependencies.
"""
from __future__ import annotations


def get_dashboard_html() -> str:
    """Return the complete standalone HTML for the mobile operator dashboard."""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>Nior AI Agent · Operator Console</title>
  <meta name="description" content="Phone-first control plane for Nior AI Website Agent">
  <meta name="theme-color" content="#0f172a">
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: rgba(22, 30, 49, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-elevated: #1a233a;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --accent: #10b981;
      --accent-hover: #059669;
      --warning: #f59e0b;
      --danger: #ef4444;
      --danger-hover: #dc2626;
      --purple: #8b5cf6;
      --radius: 14px;
      --radius-sm: 8px;
      --radius-pill: 9999px;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      --shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4), 0 8px 10px -6px rgba(0, 0, 0, 0.4);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 15px;
      line-height: 1.5;
      padding-bottom: 90px;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 50;
      background: rgba(11, 15, 25, 0.88);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--card-border);
      padding: 12px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 1.1rem;
      color: #fff;
      letter-spacing: -0.02em;
    }
    .brand-mark {
      width: 32px;
      height: 32px;
      background: linear-gradient(135deg, var(--primary), var(--purple));
      border-radius: 9px;
      display: grid;
      place-items: center;
      font-weight: 800;
      font-size: 0.95rem;
      color: #fff;
      box-shadow: 0 4px 12px rgba(59, 130, 246, 0.35);
    }
    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 14px;
      border-radius: var(--radius-sm);
      font-size: 0.88rem;
      font-weight: 600;
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.15s ease;
      text-decoration: none;
    }
    .btn-sm { padding: 5px 10px; font-size: 0.8rem; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:active { background: var(--primary-hover); transform: scale(0.98); }
    .btn-success { background: var(--accent); color: #fff; }
    .btn-warning { background: var(--warning); color: #000; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:active { background: var(--danger-hover); transform: scale(0.98); }
    .btn-outline {
      background: transparent;
      border-color: var(--card-border);
      color: var(--text-muted);
    }
    .btn-outline:active { background: rgba(255, 255, 255, 0.05); color: #fff; }
    .btn-block { width: 100%; }

    /* Bottom Nav Bar for Mobile Phones */
    .bottom-nav {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      height: 64px;
      background: rgba(15, 23, 42, 0.94);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border-top: 1px solid var(--card-border);
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      z-index: 100;
    }
    .nav-item {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 4px;
      color: var(--text-dim);
      font-size: 0.72rem;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      transition: color 0.15s ease;
    }
    .nav-item svg { width: 20px; height: 20px; stroke: currentColor; fill: none; stroke-width: 2; }
    .nav-item.active { color: var(--primary); }
    .nav-item.active svg { stroke: var(--primary); }

    .main-content {
      padding: 16px;
      max-width: 800px;
      margin: 0 auto;
    }
    .view-section { display: none; }
    .view-section.active { display: block; }

    /* Cards & Panels */
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      padding: 18px;
      margin-bottom: 16px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
    }
    .card-title {
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .card-subtitle {
      font-size: 0.82rem;
      color: var(--text-muted);
      margin-top: -6px;
      margin-bottom: 14px;
    }

    /* Status Badges */
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: var(--radius-pill);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .badge-ok, .badge-completed, .badge-pass, .badge-active, .badge-healthy {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-running, .badge-pending, .badge-queued {
      background: rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      border: 1px solid rgba(59, 130, 246, 0.3);
    }
    .badge-warning, .badge-needs_review, .badge-hold {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }
    .badge-danger, .badge-failed, .badge-blocked, .badge-error {
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.3);
    }

    /* Grid stats */
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 12px;
      margin-bottom: 16px;
    }
    @media (min-width: 500px) {
      .stat-grid { grid-template-columns: repeat(4, 1fr); }
    }
    .stat-box {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-sm);
      padding: 12px;
      text-align: center;
    }
    .stat-number {
      font-size: 1.6rem;
      font-weight: 800;
      color: #fff;
    }
    .stat-label {
      font-size: 0.75rem;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 600;
      letter-spacing: 0.04em;
    }

    /* Automation Hold Banner */
    .hold-banner {
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .hold-banner.held {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.18), rgba(239, 68, 68, 0.18));
      border: 1px solid rgba(245, 158, 11, 0.4);
    }
    .hold-banner.running {
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.15), rgba(59, 130, 246, 0.15));
      border: 1px solid rgba(16, 185, 129, 0.3);
    }

    /* Item List styling */
    .item-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .item-row {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-sm);
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .item-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .item-title { font-weight: 700; color: #fff; font-size: 0.95rem; }
    .item-desc { font-size: 0.82rem; color: var(--text-muted); }
    .item-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 4px;
      padding-top: 8px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }

    /* Modal dialog */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.8);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      z-index: 200;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .modal-overlay.active { display: flex; }
    .modal-box {
      background: var(--card-elevated);
      border: 1px solid var(--card-border);
      border-radius: var(--radius);
      width: 100%;
      max-width: 550px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 20px;
      box-shadow: var(--shadow);
    }
    .modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    .modal-title { font-size: 1.15rem; font-weight: 700; }
    .close-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 1.5rem;
      cursor: pointer;
    }

    /* Form controls */
    .form-group {
      margin-bottom: 14px;
    }
    .form-label {
      display: block;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .form-input, .form-select, .form-textarea {
      width: 100%;
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid var(--card-border);
      border-radius: var(--radius-sm);
      padding: 10px 12px;
      color: #fff;
      font-size: 0.92rem;
      font-family: inherit;
      outline: none;
    }
    .form-input:focus, .form-select:focus, .form-textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
    }
    .form-help {
      font-size: 0.75rem;
      color: var(--text-dim);
      margin-top: 4px;
    }

    /* Live Preview Frame Container */
    .preview-frame-container {
      width: 100%;
      height: 480px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--card-border);
      overflow: hidden;
      background: #fff;
      margin: 10px 0;
    }
    .preview-frame-container iframe {
      width: 100%;
      height: 100%;
      border: none;
    }
  </style>
</head>
<body>

  <!-- Top Fixed Header -->
  <header>
    <div class="brand">
      <div class="brand-mark">N</div>
      <div>
        <span>Nior Business Operator</span>
      </div>
    </div>
    <div class="header-actions">
      <button class="btn btn-sm btn-outline" id="refreshBtn" onclick="refreshCurrentView()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
        Sync
      </button>
      <button class="btn btn-sm btn-outline" onclick="openAuthModal()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 7a4 4 0 100-8 4 4 0 000 8z"/></svg>
      </button>
    </div>
  </header>

  <!-- Main Container -->
  <main class="main-content">

    <!-- 1. OVERVIEW & ATTENTION VIEW -->
    <section id="view-overview" class="view-section active">
      <!-- Automation Hold Banner -->
      <div class="hold-banner running" id="holdBanner">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <strong id="holdStatusTitle">Automation: ACTIVE</strong>
            <p style="font-size:0.8rem; color:var(--text-muted);" id="holdStatusSubtitle">Autonomous background workers and dispatch are free to run.</p>
          </div>
          <span class="badge badge-ok" id="holdBadge">RUNNING</span>
        </div>
        <div style="display:flex; gap:8px; margin-top:6px;">
          <button class="btn btn-sm btn-warning" id="holdToggleBtn" onclick="toggleHoldPrompt()">Pause Automation</button>
        </div>
      </div>

      <!-- Quick Metrics -->
      <div class="stat-grid">
        <div class="stat-box">
          <div class="stat-number" id="statLeads">0</div>
          <div class="stat-label">Leads</div>
        </div>
        <div class="stat-box">
          <div class="stat-number" id="statProjects">0</div>
          <div class="stat-label">Projects</div>
        </div>
        <div class="stat-box">
          <div class="stat-number" id="statQueued">0</div>
          <div class="stat-label">Queued Jobs</div>
        </div>
        <div class="stat-box">
          <div class="stat-number" id="statAttention">0</div>
          <div class="stat-label">Attention</div>
        </div>
      </div>

      <!-- Urgent Operator Attention -->
      <div class="card">
        <div class="card-title">
          <span>Operator Attention Queue</span>
          <span class="badge badge-warning" id="attentionBadge">0 Items</span>
        </div>
        <div class="card-subtitle">Items requiring your explicit human decision or review.</div>
        <div id="attentionList" class="item-list">
          <div style="text-align:center; padding:20px; color:var(--text-dim);">No pending blocker items. Queue healthy.</div>
        </div>
      </div>

      <!-- Quick Actions -->
      <div class="card">
        <div class="card-title">Quick Operational Actions</div>
        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap:10px;">
          <button class="btn btn-primary btn-block" onclick="openNewScoutModal()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8M21 21l-4.35-4.35"/></svg>
            Discover Leads
          </button>
          <button class="btn btn-outline btn-block" onclick="recoverStaleJobs()">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M23 4v6h-6M1 20v-6h6"/></svg>
            Recover Stale
          </button>
        </div>
      </div>
    </section>

    <!-- 2. JOBS & WORKERS VIEW -->
    <section id="view-jobs" class="view-section">
      <div class="card">
        <div class="card-title">
          <span>Background Jobs</span>
          <button class="btn btn-sm btn-primary" onclick="openNewJobModal()">+ New Job</button>
        </div>
        <div class="card-subtitle">Persistent execution records from backend database.</div>
        <div id="jobsList" class="item-list">
          <div style="text-align:center; padding:20px; color:var(--text-dim);">Loading jobs...</div>
        </div>
      </div>
    </section>

    <!-- 3. LEADS & PIPELINE VIEW -->
    <section id="view-leads" class="view-section">
      <div class="card">
        <div class="card-title">
          <span>Business Leads</span>
          <button class="btn btn-sm btn-primary" onclick="openNewScoutModal()">+ Discover</button>
        </div>
        <div class="card-subtitle">Source-backed prospects discovered and qualified.</div>
        <div id="leadsList" class="item-list">
          <div style="text-align:center; padding:20px; color:var(--text-dim);">Loading leads...</div>
        </div>
      </div>
    </section>

    <!-- 4. PROJECTS & WEBSITES VIEW -->
    <section id="view-projects" class="view-section">
      <div class="card">
        <div class="card-title">
          <span>Generated Websites</span>
        </div>
        <div class="card-subtitle">Multi-page website artifacts, validation scores, and preview deployments.</div>
        <div id="projectsList" class="item-list">
          <div style="text-align:center; padding:20px; color:var(--text-dim);">Loading projects...</div>
        </div>
      </div>
    </section>

    <!-- 5. SETTINGS & CHANNELS VIEW -->
    <section id="view-settings" class="view-section">
      <div class="card">
        <div class="card-title">System & Operator Identity</div>
        <div class="card-subtitle">Phone authentication and backend connectivity.</div>
        <div class="form-group">
          <label class="form-label">Operator Bearer Token</label>
          <input type="password" id="authTokenInput" class="form-input" placeholder="Paste your API token here">
          <div class="form-help">Stored securely in your phone's local storage.</div>
        </div>
        <button class="btn btn-primary btn-block" onclick="saveAuthToken()">Save Credentials</button>
      </div>

      <div class="card">
        <div class="card-title">Configured Infrastructure</div>
        <div id="infraStatusList" class="item-list">
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">Backend API</span>
              <span class="badge badge-ok">HEALTHY</span>
            </div>
            <div class="item-desc">Render Cloud Service</div>
          </div>
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">Database Storage</span>
              <span class="badge badge-ok" id="infraStorageBadge">POSTGRES</span>
            </div>
            <div class="item-desc" id="infraStorageDesc">Connected to Cloud Database</div>
          </div>
        </div>
      </div>
    </section>

  </main>

  <!-- Bottom Nav for Phone Navigation -->
  <nav class="bottom-nav">
    <div class="nav-item active" onclick="switchView('overview')">
      <svg viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2zM9 22V12h6v10"/></svg>
      <span>Overview</span>
    </div>
    <div class="nav-item" onclick="switchView('jobs')">
      <svg viewBox="0 0 24 24"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
      <span>Jobs</span>
    </div>
    <div class="nav-item" onclick="switchView('leads')">
      <svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 7a4 4 0 100-8 4 4 0 000 8zM23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>
      <span>Leads</span>
    </div>
    <div class="nav-item" onclick="switchView('projects')">
      <svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2M8 21h8M12 17v4"/></svg>
      <span>Websites</span>
    </div>
    <div class="nav-item" onclick="switchView('settings')">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
      <span>Settings</span>
    </div>
  </nav>

  <!-- MODALS -->

  <!-- 1. Discovery Modal -->
  <div class="modal-overlay" id="scoutModal">
    <div class="modal-box">
      <div class="modal-header">
        <h3 class="modal-title">New Lead Discovery (Scout)</h3>
        <button class="close-btn" onclick="closeModal('scoutModal')">&times;</button>
      </div>
      <div class="form-group">
        <label class="form-label">City</label>
        <input type="text" id="scoutCity" class="form-input" value="Lagos">
      </div>
      <div class="form-group">
        <label class="form-label">Country</label>
        <input type="text" id="scoutCountry" class="form-input" value="Nigeria">
      </div>
      <div class="form-group">
        <label class="form-label">Niche / Category</label>
        <select id="scoutCategory" class="form-select">
          <option value="Restaurants">Restaurants & Dining</option>
          <option value="Salons">Salons & Spas</option>
          <option value="Clinics">Clinics & Healthcare</option>
          <option value="Automotive">Automotive & Repair</option>
          <option value="Retail">Retail & Boutiques</option>
          <option value="Legal">Legal & Professional</option>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">Lead Limit</label>
        <input type="number" id="scoutLimit" class="form-input" value="5" min="1" max="10">
      </div>
      <button class="btn btn-primary btn-block" onclick="submitScoutJob()">Start Discovery Job</button>
    </div>
  </div>

  <!-- 2. Website Preview Modal -->
  <div class="modal-overlay" id="previewModal">
    <div class="modal-box" style="max-width:700px;">
      <div class="modal-header">
        <h3 class="modal-title" id="previewTitle">Website Preview</h3>
        <button class="close-btn" onclick="closeModal('previewModal')">&times;</button>
      </div>
      <div class="preview-frame-container">
        <iframe id="previewIframe" src="about:blank"></iframe>
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-top:10px;">
        <span id="previewScoreBadge" class="badge badge-ok">Score: 100/100</span>
        <button class="btn btn-sm btn-outline" onclick="openPreviewInNewTab()">Open Full Page</button>
      </div>
    </div>
  </div>

  <!-- 3. Review & Approval Modal -->
  <div class="modal-overlay" id="reviewModal">
    <div class="modal-box">
      <div class="modal-header">
        <h3 class="modal-title">Human Decision Gate</h3>
        <button class="close-btn" onclick="closeModal('reviewModal')">&times;</button>
      </div>
      <div class="form-group">
        <label class="form-label">Project</label>
        <input type="text" id="reviewProjectId" class="form-input" readonly>
      </div>
      <div class="form-group">
        <label class="form-label">Assigned Build ID</label>
        <input type="text" id="reviewBuildId" class="form-input" readonly>
      </div>
      <div class="form-group">
        <label class="form-label">Decision</label>
        <select id="reviewDecision" class="form-select" onchange="updateExpectedConfirmation()">
          <option value="APPROVED_FOR_PREVIEW">APPROVE FOR PREVIEW</option>
          <option value="REJECTED">REJECT</option>
          <option value="CHANGES_REQUESTED">REQUEST CHANGES</option>
        </select>
      </div>
      <div class="form-group">
        <label class="form-label">Reviewer Notes</label>
        <textarea id="reviewNotes" class="form-textarea" rows="3" placeholder="Notes justifying decision (e.g. Verified layout and menu accuracy)"></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">Exact Confirmation String</label>
        <input type="text" id="reviewConfirm" class="form-input" placeholder="project_id:build_id:DECISION">
        <div class="form-help">Must exactly match the assigned build to prevent approving stale builds.</div>
      </div>
      <button class="btn btn-success btn-block" onclick="submitReviewDecision()">Submit Binding Decision</button>
    </div>
  </div>

  <!-- Javascript Controller -->
  <script>
    const API_BASE = window.location.origin;
    let currentPreviewHtml = '';

    // Auth Token handling
    function getToken() {
      const urlParams = new URLSearchParams(window.location.search);
      const urlToken = urlParams.get('token');
      if (urlToken) {
        localStorage.setItem('nior_api_token', urlToken);
        return urlToken;
      }
      return localStorage.getItem('nior_api_token') || '';
    }

    function saveAuthToken() {
      const token = document.getElementById('authTokenInput').value.trim();
      if (!token) return alert('Please enter a valid token');
      localStorage.setItem('nior_api_token', token);
      alert('Token saved to phone storage.');
      refreshCurrentView();
    }

    function authHeaders() {
      const token = getToken();
      return {
        'Content-Type': 'application/json',
        ...(token ? { 'Authorization': `Bearer ${token}` } : {})
      };
    }

    // View Navigation
    function switchView(viewName) {
      document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

      const targetView = document.getElementById(`view-${viewName}`);
      if (targetView) targetView.classList.add('active');

      const navIndices = { 'overview': 0, 'jobs': 1, 'leads': 2, 'projects': 3, 'settings': 4 };
      const navItem = document.querySelectorAll('.bottom-nav .nav-item')[navIndices[viewName]];
      if (navItem) navItem.classList.add('active');

      refreshView(viewName);
    }

    function refreshCurrentView() {
      const activeSection = document.querySelector('.view-section.active');
      if (activeSection) {
        const viewName = activeSection.id.replace('view-', '');
        refreshView(viewName);
      }
    }

    function refreshView(viewName) {
      if (viewName === 'overview') loadOverview();
      else if (viewName === 'jobs') loadJobs();
      else if (viewName === 'leads') loadLeads();
      else if (viewName === 'projects') loadProjects();
      else if (viewName === 'settings') {
        document.getElementById('authTokenInput').value = getToken();
      }
    }

    // 1. Overview Loader
    async function loadOverview() {
      try {
        const [healthRes, holdRes, attnRes, leadsRes, projRes, jobsRes] = await Promise.all([
          fetch(`${API_BASE}/health`, { headers: authHeaders() }),
          fetch(`${API_BASE}/controls/hold`, { headers: authHeaders() }),
          fetch(`${API_BASE}/attention`, { headers: authHeaders() }),
          fetch(`${API_BASE}/leads`, { headers: authHeaders() }),
          fetch(`${API_BASE}/projects`, { headers: authHeaders() }),
          fetch(`${API_BASE}/jobs`, { headers: authHeaders() })
        ]);

        if (holdRes.ok) {
          const hold = await holdRes.json();
          renderHoldBanner(hold);
        }

        if (leadsRes.ok) {
          const leadsData = await leadsRes.json();
          document.getElementById('statLeads').innerText = (leadsData.leads || []).length;
        }

        if (projRes.ok) {
          const projData = await projRes.json();
          document.getElementById('statProjects').innerText = projData.project_count || (projData.projects || []).length || 0;
        }

        if (jobsRes.ok) {
          const jobsData = await jobsRes.json();
          const queued = (jobsData.jobs || []).filter(j => ['PENDING', 'QUEUED'].includes(j.status));
          document.getElementById('statQueued').innerText = queued.length;
        }

        if (attnRes.ok) {
          const attn = await attnRes.json();
          renderAttention(attn);
        }
      } catch (err) {
        console.error('Failed to load overview:', err);
      }
    }

    function renderHoldBanner(hold) {
      const banner = document.getElementById('holdBanner');
      const title = document.getElementById('holdStatusTitle');
      const subtitle = document.getElementById('holdStatusSubtitle');
      const badge = document.getElementById('holdBadge');
      const btn = document.getElementById('holdToggleBtn');

      if (hold.held) {
        banner.className = 'hold-banner held';
        title.innerText = 'Automation: ON HOLD';
        subtitle.innerText = `Paused by ${hold.set_by || 'operator'}: ${hold.reason || 'No reason recorded'}`;
        badge.className = 'badge badge-hold';
        badge.innerText = 'PAUSED';
        btn.className = 'btn btn-sm btn-success';
        btn.innerText = 'Resume Automation';
        btn.onclick = () => releaseHold();
      } else {
        banner.className = 'hold-banner running';
        title.innerText = 'Automation: ACTIVE';
        subtitle.innerText = 'Autonomous background workers and dispatch are free to run.';
        badge.className = 'badge badge-ok';
        badge.innerText = 'RUNNING';
        btn.className = 'btn btn-sm btn-warning';
        btn.innerText = 'Pause Automation';
        btn.onclick = () => toggleHoldPrompt();
      }
    }

    async function toggleHoldPrompt() {
      const reason = prompt('Reason for placing automation on hold:', 'Operator phone maintenance');
      if (reason === null) return;
      try {
        const res = await fetch(`${API_BASE}/controls/hold`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ actor: 'phone-dashboard', reason: reason })
        });
        if (res.ok) {
          alert('Automation held.');
          loadOverview();
        } else {
          alert('Failed to set hold');
        }
      } catch (err) {
        alert('Error: ' + err.message);
      }
    }

    async function releaseHold() {
      if (!confirm('Release automation hold and allow background dispatch?')) return;
      try {
        const res = await fetch(`${API_BASE}/controls/release`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ actor: 'phone-dashboard' })
        });
        if (res.ok) {
          alert('Automation resumed.');
          loadOverview();
        }
      } catch (err) {
        alert('Error: ' + err.message);
      }
    }

    function renderAttention(attn) {
      const list = document.getElementById('attentionList');
      const badge = document.getElementById('attentionBadge');
      const items = attn.items || [];
      badge.innerText = `${items.length} Items`;

      if (!items.length) {
        list.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-dim);">No pending blocker items. Queue healthy.</div>';
        document.getElementById('statAttention').innerText = '0';
        return;
      }

      document.getElementById('statAttention').innerText = items.length;
      list.innerHTML = items.map(item => `
        <div class="item-row">
          <div class="item-header">
            <span class="item-title">${item.kind || 'Attention Required'}</span>
            <span class="badge badge-warning">${item.severity || 'WARN'}</span>
          </div>
          <div class="item-desc">${item.summary || item.description || ''}</div>
          <div class="item-footer">
            <span style="font-size:0.75rem; color:var(--text-dim);">${item.project_id || item.job_id || ''}</span>
            <span style="font-size:0.75rem; color:var(--primary); font-weight:600;">${item.next_action || 'Review'}</span>
          </div>
        </div>
      `).join('');
    }

    // 2. Jobs Loader
    async function loadJobs() {
      const container = document.getElementById('jobsList');
      try {
        const res = await fetch(`${API_BASE}/jobs`, { headers: authHeaders() });
        if (!res.ok) throw new Error('API refused request');
        const data = await res.json();
        const jobs = data.jobs || [];
        if (!jobs.length) {
          container.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-dim);">No jobs found.</div>';
          return;
        }
        container.innerHTML = jobs.map(j => {
          const badgeClass = `badge-${(j.status || '').toLowerCase()}`;
          return `
            <div class="item-row">
              <div class="item-header">
                <span class="item-title">${j.job_type} · ${j.job_id}</span>
                <span class="badge ${badgeClass}">${j.status}</span>
              </div>
              <div class="item-desc">${(j.output && j.output.summary) || j.error || 'No summary'}</div>
              <div class="item-footer">
                <span style="font-size:0.75rem; color:var(--text-dim);">${j.lead_id || j.project_id || ''}</span>
                <div style="display:flex; gap:6px;">
                  ${['FAILED', 'NEEDS_REVIEW'].includes(j.status) ? `<button class="btn btn-sm btn-outline" onclick="retryJob('${j.job_id}')">Retry</button>` : ''}
                  ${['PENDING', 'QUEUED'].includes(j.status) ? `<button class="btn btn-sm btn-primary" onclick="runJob('${j.job_id}')">Run Now</button>` : ''}
                </div>
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        container.innerHTML = `<div style="text-align:center; padding:20px; color:var(--danger);">${err.message}</div>`;
      }
    }

    async function runJob(jobId) {
      try {
        const res = await fetch(`${API_BASE}/jobs/${jobId}/run`, {
          method: 'POST',
          headers: authHeaders()
        });
        const data = await res.json();
        alert(`Job ${jobId}: ${data.status}`);
        loadJobs();
      } catch (err) {
        alert('Error running job: ' + err.message);
      }
    }

    async function retryJob(jobId) {
      try {
        const res = await fetch(`${API_BASE}/jobs/${jobId}/retry`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ run: true })
        });
        const data = await res.json();
        alert(`Retried: ${data.status}`);
        loadJobs();
      } catch (err) {
        alert('Error retrying job: ' + err.message);
      }
    }

    async function recoverStaleJobs() {
      try {
        const res = await fetch(`${API_BASE}/jobs/recover-stale`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ older_than_seconds: 600 })
        });
        const data = await res.json();
        alert(`Recovered ${data.recovered_count || 0} stale jobs.`);
        loadOverview();
      } catch (err) {
        alert('Error: ' + err.message);
      }
    }

    // 3. Leads Loader
    async function loadLeads() {
      const container = document.getElementById('leadsList');
      try {
        const res = await fetch(`${API_BASE}/leads`, { headers: authHeaders() });
        const data = await res.json();
        const leads = data.leads || [];
        if (!leads.length) {
          container.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-dim);">No leads recorded.</div>';
          return;
        }
        container.innerHTML = leads.map(l => `
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">${l.business_name || 'Unnamed Business'}</span>
              <span class="badge badge-${(l.qualification_status || 'discovered').toLowerCase()}">${l.qualification_status || 'DISCOVERED'}</span>
            </div>
            <div class="item-desc">${l.city || ''}, ${l.country || ''} · ${l.business_type || 'General'}</div>
            <div class="item-footer">
              <span style="font-size:0.75rem; color:var(--text-dim);">${l.lead_id}</span>
              ${l.qualification_status === 'QUALIFIED' ? `<button class="btn btn-sm btn-primary" onclick="buildWebsiteForLead('${l.lead_id}')">Build Site</button>` : ''}
            </div>
          </div>
        `).join('');
      } catch (err) {
        container.innerHTML = `<div style="text-align:center; padding:20px; color:var(--danger);">${err.message}</div>`;
      }
    }

    async function buildWebsiteForLead(leadId) {
      if (!confirm(`Generate website for ${leadId}?`)) return;
      try {
        const jobId = `build-job-${Date.now()}`;
        const res = await fetch(`${API_BASE}/jobs`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({
            job_type: 'BUILD',
            job_id: jobId,
            lead_id: leadId,
            run: true
          })
        });
        const data = await res.json();
        alert(`Build initiated: ${data.status}`);
        switchView('projects');
      } catch (err) {
        alert('Error: ' + err.message);
      }
    }

    // 4. Projects Loader
    async function loadProjects() {
      const container = document.getElementById('projectsList');
      try {
        const res = await fetch(`${API_BASE}/projects`, { headers: authHeaders() });
        const data = await res.json();
        const projects = Object.values(data.projects || {});
        if (!projects.length) {
          container.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-dim);">No projects recorded.</div>';
          return;
        }
        container.innerHTML = projects.map(p => `
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">${p.business_name || p.project_id}</span>
              <span class="badge badge-${(p.review_status || 'pending').toLowerCase()}">${p.review_status || 'PENDING'}</span>
            </div>
            <div class="item-desc">Stage: ${p.current_stage || ''} · Build: ${p.assigned_build_id || 'None'}</div>
            <div class="item-footer">
              <button class="btn btn-sm btn-outline" onclick="openPreview('${p.project_id}')">Preview Site</button>
              <button class="btn btn-sm btn-primary" onclick="openReviewDialog('${p.project_id}', '${p.assigned_build_id}')">Review Gate</button>
            </div>
          </div>
        `).join('');
      } catch (err) {
        container.innerHTML = `<div style="text-align:center; padding:20px; color:var(--danger);">${err.message}</div>`;
      }
    }

    async function openPreview(projectId) {
      try {
        const res = await fetch(`${API_BASE}/projects/${projectId}/artifact`, { headers: authHeaders() });
        if (!res.ok) throw new Error('Artifact not available');
        const data = await res.json();
        currentPreviewHtml = data.html_content;
        document.getElementById('previewTitle').innerText = `Preview: ${projectId}`;
        document.getElementById('previewScoreBadge').innerText = `SHA: ${data.html_sha256 ? data.html_sha256.substring(0, 10) : 'verified'}`;
        const iframe = document.getElementById('previewIframe');
        iframe.srcdoc = data.html_content;
        openModal('previewModal');
      } catch (err) {
        alert('Could not load preview artifact: ' + err.message);
      }
    }

    function openPreviewInNewTab() {
      const win = window.open('', '_blank');
      win.document.write(currentPreviewHtml);
      win.document.close();
    }

    function openReviewDialog(projectId, buildId) {
      document.getElementById('reviewProjectId').value = projectId;
      document.getElementById('reviewBuildId').value = buildId;
      document.getElementById('reviewDecision').value = 'APPROVED_FOR_PREVIEW';
      updateExpectedConfirmation();
      openModal('reviewModal');
    }

    function updateExpectedConfirmation() {
      const pid = document.getElementById('reviewProjectId').value;
      const bid = document.getElementById('reviewBuildId').value;
      const dec = document.getElementById('reviewDecision').value;
      document.getElementById('reviewConfirm').value = `${pid}:${bid}:${dec}`;
    }

    async function submitReviewDecision() {
      const projectId = document.getElementById('reviewProjectId').value;
      const decision = document.getElementById('reviewDecision').value;
      const notes = document.getElementById('reviewNotes').value.trim();
      const confirmStr = document.getElementById('reviewConfirm').value.trim();

      if (!notes) return alert('Reviewer notes are strictly required.');

      try {
        const res = await fetch(`${API_BASE}/projects/${projectId}/review`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({
            decision: decision,
            notes: notes,
            confirm: confirmStr,
            reviewer: 'operator-phone'
          })
        });
        const data = await res.json();
        if (res.ok) {
          alert('Decision recorded successfully: ' + data.review_status);
          closeModal('reviewModal');
          loadProjects();
        } else {
          alert('Review refused: ' + (data.error || 'Confirmation mismatch'));
        }
      } catch (err) {
        alert('Error: ' + err.message);
      }
    }

    // Modal Helpers
    function openModal(id) { document.getElementById(id).classList.add('active'); }
    function closeModal(id) { document.getElementById(id).classList.remove('active'); }
    function openNewScoutModal() { openModal('scoutModal'); }
    function openAuthModal() { switchView('settings'); }

    async function submitScoutJob() {
      const city = document.getElementById('scoutCity').value.trim();
      const country = document.getElementById('scoutCountry').value.trim();
      const category = document.getElementById('scoutCategory').value;
      const limit = parseInt(document.getElementById('scoutLimit').value) || 5;

      const jobId = `scout-phone-${Date.now()}`;
      try {
        const res = await fetch(`${API_BASE}/jobs`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({
            job_type: 'SCOUT',
            job_id: jobId,
            input: {
              city: city,
              country: country,
              niche: category,
              lead_limit: limit,
              dry_run: true
            },
            run: true
          })
        });
        const data = await res.json();
        alert(`Discovery job created: ${data.status}`);
        closeModal('scoutModal');
        switchView('jobs');
      } catch (err) {
        alert('Failed to launch discovery: ' + err.message);
      }
    }

    // Initial load
    window.addEventListener('DOMContentLoaded', () => {
      loadOverview();
    });
  </script>
</body>
</html>
"""
