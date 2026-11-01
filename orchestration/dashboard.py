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
      touch-action: manipulation;
      user-select: none;
      -webkit-user-select: none;
    }
    .btn * { pointer-events: none; }
    .btn-sm { padding: 5px 10px; font-size: 0.8rem; }
    .btn-primary { background: var(--primary); color: #fff; }
    .btn-primary:active { background: var(--primary-hover); transform: scale(0.96); }
    .btn-success { background: var(--accent); color: #fff; }
    .btn-warning { background: var(--warning); color: #000; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-danger:active { background: var(--danger-hover); transform: scale(0.96); }
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
      touch-action: manipulation;
      user-select: none;
      -webkit-user-select: none;
    }
    .nav-item * { pointer-events: none; }
    .nav-item:active { transform: scale(0.92); opacity: 0.8; }
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

    <!-- Unauthenticated Warning Banner -->
    <div id="unauthBanner" style="display:none; background:rgba(239, 68, 68, 0.15); border:1px solid rgba(239, 68, 68, 0.4); border-radius:var(--radius); padding:14px 16px; margin-bottom:16px; align-items:center; justify-content:space-between; gap:12px;">
      <div>
        <strong style="color:#f87171; font-size:0.92rem; display:block;">Device Not Authenticated</strong>
        <p style="font-size:0.78rem; color:var(--text-muted); margin-top:2px;">Phone needs Bearer token to communicate with backend.</p>
      </div>
      <button class="btn btn-sm btn-danger" onclick="switchView('settings')">Authenticate</button>
    </div>

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
      <!-- 1. AI Content Generation Settings -->
      <div class="card">
        <div class="card-title">
          <span>AI Content Generation</span>
          <span id="aiActiveBadge" class="badge badge-ok">OPENROUTER</span>
        </div>
        <div class="card-subtitle">Model providers and fact-grounded copywriting inference.</div>
        <div class="form-group">
          <label class="form-label">Active Provider</label>
          <select id="setAiProvider" class="form-select" onchange="toggleAiProviderFields()">
            <option value="openrouter">OpenRouter (Recommended)</option>
            <option value="nvidia">NVIDIA NIM</option>
            <option value="deterministic">Deterministic Fallback (No API Keys)</option>
          </select>
        </div>
        <div id="openrouterFields">
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">OpenRouter API Key</label>
              <span id="orKeyBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setOpenRouterKey" class="form-input" placeholder="sk-or-v1-...">
            <div class="form-help">Leave empty to keep existing configured key.</div>
          </div>
          <div class="form-group">
            <label class="form-label">OpenRouter Model</label>
            <input type="text" id="setOpenRouterModel" class="form-input" placeholder="openrouter/free">
          </div>
        </div>
        <div id="nvidiaFields">
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">NVIDIA API Key</label>
              <span id="nvKeyBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setNvidiaKey" class="form-input" placeholder="nvapi-...">
            <div class="form-help">Leave empty to keep existing configured key.</div>
          </div>
          <div class="form-group">
            <label class="form-label">NVIDIA Model</label>
            <input type="text" id="setNvidiaModel" class="form-input" placeholder="meta/llama-3.1-70b-instruct">
          </div>
        </div>
      </div>

      <!-- 2. Discovery & Scouting Defaults -->
      <div class="card">
        <div class="card-title">Scouting & Discovery Defaults</div>
        <div class="card-subtitle">Default parameters when launching new lead discovery batches.</div>
        <div class="form-group">
          <label class="form-label">Default City</label>
          <input type="text" id="setDefaultCity" class="form-input" placeholder="Lagos">
        </div>
        <div class="form-group">
          <label class="form-label">Default Country</label>
          <input type="text" id="setDefaultCountry" class="form-input" placeholder="Nigeria">
        </div>
        <div class="form-group">
          <label class="form-label">Default Category</label>
          <select id="setDefaultCategory" class="form-select">
            <option value="Restaurants">Restaurants & Dining</option>
            <option value="Salons">Salons & Spas</option>
            <option value="Clinics">Clinics & Healthcare</option>
            <option value="Automotive">Automotive & Repair</option>
            <option value="Retail">Retail & Boutiques</option>
            <option value="Legal">Legal & Professional</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Default Lead Limit (1-25)</label>
          <input type="number" id="setDefaultLimit" class="form-input" min="1" max="25" placeholder="5">
        </div>
      </div>

      <!-- 3. Telegram Operator Bot & Notifications -->
      <div class="card">
        <div class="card-title">
          <span>Telegram Operator Bot</span>
          <span id="tgBotBadge" class="badge badge-warn">NOT CONNECTED</span>
        </div>
        <div class="card-subtitle">24/7 phone control and real-time operational notifications.</div>
        <div class="form-group">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <label class="form-label">Telegram Bot Token</label>
            <span id="tgKeyBadge" class="badge badge-warn">NOT SET</span>
          </div>
          <input type="password" id="setTelegramToken" class="form-input" placeholder="123456789:ABCdefGhIjkLmNoPqRsTuVwXyZ">
          <div class="form-help">Obtain from @BotFather on Telegram.</div>
        </div>
        <div class="form-group">
          <label class="form-label">Allowed User IDs</label>
          <input type="text" id="setTelegramUsers" class="form-input" placeholder="e.g. 12345678, 87654321">
          <div class="form-help">Comma-separated numeric Telegram user IDs permitted to command the bot.</div>
        </div>
        <div class="form-group">
          <label class="form-label">Allowed Chat IDs</label>
          <input type="text" id="setTelegramChats" class="form-input" placeholder="e.g. -100123456789">
        </div>
        <div class="form-group" style="display:flex; align-items:center; justify-content:space-between; margin-top:12px;">
          <label class="form-label" style="margin-bottom:0;">Operational Notifications</label>
          <input type="checkbox" id="setNotifications" style="width:20px; height:20px; accent-color:var(--primary);">
        </div>
      </div>

      <!-- 4. Autonomous Worker & Dispatch Engine -->
      <div class="card">
        <div class="card-title">Autonomous Worker Engine</div>
        <div class="card-subtitle">Background daemon polling, dispatch gates, and auto-recovery.</div>
        <div class="form-group" style="display:flex; align-items:center; justify-content:space-between;">
          <label class="form-label" style="margin-bottom:0;">Auto-Dispatch Queued Jobs</label>
          <input type="checkbox" id="setAutoDispatch" style="width:20px; height:20px; accent-color:var(--primary);">
        </div>
        <div class="form-group">
          <label class="form-label">Worker Poll Interval (1 - 60 seconds)</label>
          <input type="number" id="setWorkerInterval" class="form-input" min="1" max="60" placeholder="5">
          <div class="form-help">How frequently the background thread checks for queued pipeline jobs.</div>
        </div>
        <div class="form-group">
          <label class="form-label">Stale Job Recovery Timeout (seconds)</label>
          <input type="number" id="setStaleTimeout" class="form-input" min="60" max="7200" placeholder="900">
          <div class="form-help">Jobs running longer than this are automatically marked retry-eligible.</div>
        </div>
      </div>

      <!-- 5. Deployment & Vercel Preview -->
      <div class="card">
        <div class="card-title">Deployment & Hosting</div>
        <div class="card-subtitle">Vercel isolated preview provisioning and domain configuration.</div>
        <div class="form-group">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <label class="form-label">Vercel API Token</label>
            <span id="vercelKeyBadge" class="badge badge-warn">NOT SET</span>
          </div>
          <input type="password" id="setVercelToken" class="form-input" placeholder="Bearer token or personal access token">
        </div>
        <div class="form-group">
          <label class="form-label">Vercel Project Name</label>
          <input type="text" id="setVercelProject" class="form-input" placeholder="ai-website-agent-preview">
        </div>
        <div class="form-group">
          <label class="form-label">Preview Base Domain</label>
          <input type="text" id="setPreviewDomain" class="form-input" placeholder="https://ai-website-agent-preview.vercel.app">
        </div>
      </div>

      <!-- 6. Email Outreach (SMTP & Gmail API) -->
      <div class="card">
        <div class="card-title">
          <span>Email Outreach Engine</span>
          <span id="emailStatusBadge" class="badge badge-warn">NOT CONNECTED</span>
        </div>
        <div class="card-subtitle">Connect your agency email to send approved prospect pitches directly.</div>
        <div class="form-group" style="display:flex; align-items:center; justify-content:space-between;">
          <label class="form-label" style="margin-bottom:0;">Enable Email Outreach</label>
          <input type="checkbox" id="setEmailOutreach" style="width:20px; height:20px; accent-color:var(--primary);">
        </div>
        <div class="form-group">
          <label class="form-label">Sender Email Address</label>
          <input type="email" id="setEmailSender" class="form-input" placeholder="e.g. outreach@youragency.com">
        </div>
        <div class="form-group">
          <label class="form-label">Email Provider</label>
          <select id="setEmailProvider" class="form-select" onchange="toggleEmailProviderFields()">
            <option value="smtp">Standard SMTP (Gmail / Custom Domain)</option>
            <option value="gmail">Google Gmail API</option>
          </select>
        </div>
        <div id="smtpFields">
          <div class="form-group">
            <label class="form-label">SMTP Host</label>
            <input type="text" id="setSmtpHost" class="form-input" placeholder="smtp.gmail.com">
          </div>
          <div class="form-group">
            <label class="form-label">SMTP Port</label>
            <input type="number" id="setSmtpPort" class="form-input" placeholder="587">
          </div>
          <div class="form-group">
            <label class="form-label">SMTP Username / Email</label>
            <input type="text" id="setSmtpUser" class="form-input" placeholder="yourname@gmail.com">
          </div>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">SMTP App Password</label>
              <span id="smtpKeyBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setSmtpPass" class="form-input" placeholder="16-character Google App Password">
            <div class="form-help">For Gmail: Use an App Password generated from Google Account Security.</div>
          </div>
        </div>
        <div id="gmailApiFields" style="display:none;">
          <div class="form-group">
            <label class="form-label">Gmail Client ID</label>
            <input type="text" id="setGmailClientId" class="form-input" placeholder="12345-xxx.apps.googleusercontent.com">
          </div>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">Gmail Client Secret</label>
              <span id="gmailSecretBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setGmailClientSecret" class="form-input" placeholder="GOCSPX-...">
          </div>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">Gmail Refresh Token</label>
              <span id="gmailRefreshBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setGmailRefreshToken" class="form-input" placeholder="1//04...">
          </div>
        </div>
      </div>

      <!-- 7. Buffer & Meta Social Media API -->
      <div class="card">
        <div class="card-title">
          <span>Buffer & Social Media API</span>
          <span id="bufferStatusBadge" class="badge badge-warn">NOT CONNECTED</span>
        </div>
        <div class="card-subtitle">Connect Buffer to publish preview showcase posts across Instagram, Twitter/X, Facebook, and LinkedIn.</div>
        <div class="form-group" style="display:flex; align-items:center; justify-content:space-between;">
          <label class="form-label" style="margin-bottom:0;">Enable Social Publishing</label>
          <input type="checkbox" id="setSocialChannels" style="width:20px; height:20px; accent-color:var(--primary);">
        </div>
        <div class="form-group">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <label class="form-label">Buffer Access Token</label>
            <span id="bufferKeyBadge" class="badge badge-warn">NOT SET</span>
          </div>
          <input type="password" id="setBufferToken" class="form-input" placeholder="1/xxx... access token">
          <div class="form-help">Obtain from buffer.com/developers/apps.</div>
        </div>
        <div class="form-group">
          <label class="form-label">Buffer Instagram Profile ID</label>
          <input type="text" id="setBufferInstagram" class="form-input" placeholder="e.g. 64a8b7...">
        </div>
        <div class="form-group">
          <label class="form-label">Buffer Facebook Profile ID</label>
          <input type="text" id="setBufferFacebook" class="form-input" placeholder="e.g. 64a8c9...">
        </div>
        <div class="form-group">
          <label class="form-label">Buffer Twitter / X Profile ID</label>
          <input type="text" id="setBufferTwitter" class="form-input" placeholder="e.g. 64a8d0...">
        </div>
        <div class="form-group">
          <label class="form-label">Buffer LinkedIn Profile ID</label>
          <input type="text" id="setBufferLinkedin" class="form-input" placeholder="e.g. 64a8e1...">
        </div>

        <div style="border-top:1px solid rgba(255,255,255,0.06); margin:14px 0; padding-top:10px;">
          <strong style="font-size:0.88rem; color:#fff; display:block; margin-bottom:6px;">Meta / Instagram Graph API (Direct)</strong>
          <div class="form-group">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <label class="form-label">Meta Graph Access Token</label>
              <span id="metaKeyBadge" class="badge badge-warn">NOT SET</span>
            </div>
            <input type="password" id="setMetaToken" class="form-input" placeholder="EAAB...">
            <div class="form-help">Long-lived Page or Instagram Graph API token.</div>
          </div>
          <div class="form-group">
            <label class="form-label">Meta Facebook Page ID</label>
            <input type="text" id="setMetaPageId" class="form-input" placeholder="e.g. 1029384756">
          </div>
          <div class="form-group">
            <label class="form-label">Meta Instagram Business Account ID</label>
            <input type="text" id="setMetaInstaId" class="form-input" placeholder="e.g. 17841400...">
          </div>
        </div>
      </div>

      <!-- Save Button Fixed at bottom of settings -->
      <div style="margin-bottom: 24px;">
        <button class="btn btn-primary btn-block" style="padding:14px; font-size:1rem; font-weight:700;" onclick="saveAllSettings()">Save System Settings</button>
      </div>

      <!-- 6. Phone Authentication Token -->
      <div class="card">
        <div class="card-title">Phone Security Token</div>
        <div class="card-subtitle">This device's local bearer token for authenticating with the API.</div>
        <div class="form-group">
          <label class="form-label">Operator Bearer Token</label>
          <input type="password" id="authTokenInput" class="form-input" placeholder="Paste your API token here">
          <div class="form-help">Stored in this phone's localStorage.</div>
        </div>
        <button class="btn btn-outline btn-block" onclick="saveAuthToken()">Save Phone Token</button>
      </div>

      <!-- 7. Live Infrastructure Status -->
      <div class="card">
        <div class="card-title">Live Infrastructure</div>
        <div id="infraStatusList" class="item-list">
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">Backend API</span>
              <span class="badge badge-ok" id="infraApiBadge">ONLINE</span>
            </div>
            <div class="item-desc" id="infraApiDesc">Operational</div>
          </div>
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">Database Storage</span>
              <span class="badge badge-ok" id="infraStorageBadge">JSON</span>
            </div>
            <div class="item-desc" id="infraStorageDesc">Connected</div>
          </div>
          <div class="item-row">
            <div class="item-header">
              <span class="item-title">Background Worker</span>
              <span class="badge badge-ok" id="infraWorkerBadge">RUNNING</span>
            </div>
            <div class="item-desc" id="infraWorkerDesc">Autonomous queue polling</div>
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
      else if (viewName === 'settings') loadSettings();
    }

    function checkAuthBanner() {
      const banner = document.getElementById('unauthBanner');
      if (!banner) return;
      if (!getToken()) {
        banner.style.display = 'flex';
      } else {
        banner.style.display = 'none';
      }
    }

    // 1. Overview Loader
    async function loadOverview() {
      checkAuthBanner();
      try {
        const results = await Promise.allSettled([
          fetch(`${API_BASE}/health`, { headers: authHeaders() }),
          fetch(`${API_BASE}/controls/hold`, { headers: authHeaders() }),
          fetch(`${API_BASE}/attention`, { headers: authHeaders() }),
          fetch(`${API_BASE}/leads`, { headers: authHeaders() }),
          fetch(`${API_BASE}/projects`, { headers: authHeaders() }),
          fetch(`${API_BASE}/jobs`, { headers: authHeaders() })
        ]);
        const [healthRes, holdRes, attnRes, leadsRes, projRes, jobsRes] = results;

        if (holdRes.status === 'fulfilled' && holdRes.value.ok) {
          const hold = await holdRes.value.json();
          renderHoldBanner(hold);
        }

        if (leadsRes.status === 'fulfilled' && leadsRes.value.ok) {
          const leadsData = await leadsRes.value.json();
          document.getElementById('statLeads').innerText = (leadsData.leads || []).length;
        }

        if (projRes.status === 'fulfilled' && projRes.value.ok) {
          const projData = await projRes.value.json();
          document.getElementById('statProjects').innerText = projData.project_count || (projData.projects || []).length || 0;
        }

        if (jobsRes.status === 'fulfilled' && jobsRes.value.ok) {
          const jobsData = await jobsRes.value.json();
          const queued = (jobsData.jobs || []).filter(j => ['PENDING', 'QUEUED'].includes(j.status));
          document.getElementById('statQueued').innerText = queued.length;
        }

        if (attnRes.status === 'fulfilled' && attnRes.value.ok) {
          const attn = await attnRes.value.json();
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

    // Settings Management
    function toggleAiProviderFields() {
      const p = document.getElementById('setAiProvider').value;
      const or = document.getElementById('openrouterFields');
      const nv = document.getElementById('nvidiaFields');
      if (or) or.style.display = (p === 'openrouter') ? 'block' : 'none';
      if (nv) nv.style.display = (p === 'nvidia') ? 'block' : 'none';
    }

    function toggleEmailProviderFields() {
      const p = document.getElementById('setEmailProvider').value;
      const smtp = document.getElementById('smtpFields');
      const gmail = document.getElementById('gmailApiFields');
      if (smtp) smtp.style.display = (p === 'smtp') ? 'block' : 'none';
      if (gmail) gmail.style.display = (p === 'gmail') ? 'block' : 'none';
    }

    async function loadSettings() {
      document.getElementById('authTokenInput').value = getToken();
      try {
        const [settingsRes, healthRes] = await Promise.all([
          fetch(`${API_BASE}/settings`, { headers: authHeaders() }),
          fetch(`${API_BASE}/health`, { headers: authHeaders() })
        ]);
        if (settingsRes.ok) {
          const cfg = await settingsRes.json();
          // AI
          document.getElementById('setAiProvider').value = cfg.ai_provider || 'openrouter';
          document.getElementById('setOpenRouterModel').value = cfg.openrouter_model || '';
          document.getElementById('setNvidiaModel').value = cfg.nvidia_model || '';
          document.getElementById('orKeyBadge').innerText = cfg.openrouter_api_key_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('orKeyBadge').className = cfg.openrouter_api_key_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('nvKeyBadge').innerText = cfg.nvidia_api_key_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('nvKeyBadge').className = cfg.nvidia_api_key_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('aiActiveBadge').innerText = (cfg.ai_provider || 'OPENROUTER').toUpperCase();

          // Discovery
          document.getElementById('setDefaultCity').value = cfg.default_city || 'Lagos';
          document.getElementById('setDefaultCountry').value = cfg.default_country || 'Nigeria';
          document.getElementById('setDefaultCategory').value = cfg.default_category || 'Restaurants';
          document.getElementById('setDefaultLimit').value = cfg.default_lead_limit || 5;

          // Telegram
          document.getElementById('tgKeyBadge').innerText = cfg.telegram_bot_token_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('tgKeyBadge').className = cfg.telegram_bot_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('tgBotBadge').innerText = cfg.telegram_bot_token_configured ? 'CONNECTED' : 'NOT CONNECTED';
          document.getElementById('tgBotBadge').className = cfg.telegram_bot_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('setTelegramUsers').value = cfg.telegram_allowed_user_ids || '';
          document.getElementById('setTelegramChats').value = cfg.telegram_allowed_chat_ids || '';
          document.getElementById('setNotifications').checked = cfg.notifications_enabled !== false;

          // Worker
          document.getElementById('setAutoDispatch').checked = cfg.auto_dispatch !== false;
          document.getElementById('setWorkerInterval').value = cfg.worker_poll_interval || 5;
          document.getElementById('setStaleTimeout').value = cfg.stale_job_timeout || 900;

          // Deployment
          document.getElementById('vercelKeyBadge').innerText = cfg.vercel_token_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('vercelKeyBadge').className = cfg.vercel_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('setVercelProject').value = cfg.vercel_project_name || '';
          document.getElementById('setPreviewDomain').value = cfg.preview_domain || '';

          // Email
          document.getElementById('setEmailOutreach').checked = cfg.email_outreach_enabled === true;
          document.getElementById('setEmailSender').value = cfg.email_sender_address || '';
          document.getElementById('setEmailProvider').value = cfg.email_provider || 'smtp';
          document.getElementById('setSmtpHost').value = cfg.smtp_host || 'smtp.gmail.com';
          document.getElementById('setSmtpPort').value = cfg.smtp_port || 587;
          document.getElementById('setSmtpUser').value = cfg.smtp_username || '';
          document.getElementById('smtpKeyBadge').innerText = cfg.smtp_password_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('smtpKeyBadge').className = cfg.smtp_password_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('setGmailClientId').value = cfg.gmail_client_id || '';
          document.getElementById('gmailSecretBadge').innerText = cfg.gmail_client_secret_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('gmailSecretBadge').className = cfg.gmail_client_secret_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('gmailRefreshBadge').innerText = cfg.gmail_refresh_token_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('gmailRefreshBadge').className = cfg.gmail_refresh_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          const emailConnected = cfg.smtp_password_configured || cfg.gmail_refresh_token_configured;
          document.getElementById('emailStatusBadge').innerText = emailConnected ? 'CONNECTED' : 'NOT CONNECTED';
          document.getElementById('emailStatusBadge').className = emailConnected ? 'badge badge-ok' : 'badge badge-warn';

          // Buffer & Meta Social Media
          document.getElementById('setSocialChannels').checked = cfg.social_channels_enabled !== false;
          document.getElementById('bufferKeyBadge').innerText = cfg.buffer_access_token_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('bufferKeyBadge').className = cfg.buffer_access_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('setBufferInstagram').value = cfg.buffer_instagram_profile_id || '';
          document.getElementById('setBufferFacebook').value = cfg.buffer_facebook_profile_id || '';
          document.getElementById('setBufferTwitter').value = cfg.buffer_twitter_profile_id || '';
          document.getElementById('setBufferLinkedin').value = cfg.buffer_linkedin_profile_id || '';
          document.getElementById('metaKeyBadge').innerText = cfg.meta_access_token_configured ? 'CONFIGURED' : 'NOT SET';
          document.getElementById('metaKeyBadge').className = cfg.meta_access_token_configured ? 'badge badge-ok' : 'badge badge-warn';
          document.getElementById('setMetaPageId').value = cfg.meta_page_id || '';
          document.getElementById('setMetaInstaId').value = cfg.meta_instagram_account_id || '';
          const socialConnected = cfg.buffer_access_token_configured || cfg.meta_access_token_configured;
          document.getElementById('bufferStatusBadge').innerText = socialConnected ? 'CONNECTED' : 'NOT CONNECTED';
          document.getElementById('bufferStatusBadge').className = socialConnected ? 'badge badge-ok' : 'badge badge-warn';

          toggleAiProviderFields();
          toggleEmailProviderFields();
        }

        if (healthRes.ok) {
          const h = await healthRes.json();
          const storage = h.health?.source_of_truth?.storage?.backend || 'JSON';
          document.getElementById('infraStorageBadge').innerText = storage.toUpperCase();
        }
      } catch (err) {
        console.error('Settings load error:', err);
      }
    }

    async function saveAllSettings() {
      const updates = {
        ai_provider: document.getElementById('setAiProvider').value,
        openrouter_model: document.getElementById('setOpenRouterModel').value,
        nvidia_model: document.getElementById('setNvidiaModel').value,
        default_city: document.getElementById('setDefaultCity').value,
        default_country: document.getElementById('setDefaultCountry').value,
        default_category: document.getElementById('setDefaultCategory').value,
        default_lead_limit: parseInt(document.getElementById('setDefaultLimit').value) || 5,
        telegram_allowed_user_ids: document.getElementById('setTelegramUsers').value,
        telegram_allowed_chat_ids: document.getElementById('setTelegramChats').value,
        notifications_enabled: document.getElementById('setNotifications').checked,
        auto_dispatch: document.getElementById('setAutoDispatch').checked,
        worker_poll_interval: parseInt(document.getElementById('setWorkerInterval').value) || 5,
        stale_job_timeout: parseInt(document.getElementById('setStaleTimeout').value) || 900,
        vercel_project_name: document.getElementById('setVercelProject').value,
        preview_domain: document.getElementById('setPreviewDomain').value,
        // Email
        email_outreach_enabled: document.getElementById('setEmailOutreach').checked,
        email_sender_address: document.getElementById('setEmailSender').value,
        email_provider: document.getElementById('setEmailProvider').value,
        smtp_host: document.getElementById('setSmtpHost').value,
        smtp_port: parseInt(document.getElementById('setSmtpPort').value) || 587,
        smtp_username: document.getElementById('setSmtpUser').value,
        gmail_client_id: document.getElementById('setGmailClientId').value,
        // Social Media
        social_channels_enabled: document.getElementById('setSocialChannels').checked,
        buffer_instagram_profile_id: document.getElementById('setBufferInstagram').value,
        buffer_facebook_profile_id: document.getElementById('setBufferFacebook').value,
        buffer_twitter_profile_id: document.getElementById('setBufferTwitter').value,
        buffer_linkedin_profile_id: document.getElementById('setBufferLinkedin').value,
        meta_page_id: document.getElementById('setMetaPageId').value,
        meta_instagram_account_id: document.getElementById('setMetaInstaId').value,
      };

      const orKey = document.getElementById('setOpenRouterKey').value.trim();
      if (orKey && !orKey.includes('...')) updates.openrouter_api_key = orKey;

      const nvKey = document.getElementById('setNvidiaKey').value.trim();
      if (nvKey && !nvKey.includes('...')) updates.nvidia_api_key = nvKey;

      const tgToken = document.getElementById('setTelegramToken').value.trim();
      if (tgToken && !tgToken.includes('...')) updates.telegram_bot_token = tgToken;

      const vercelToken = document.getElementById('setVercelToken').value.trim();
      if (vercelToken && !vercelToken.includes('...')) updates.vercel_token = vercelToken;

      const smtpPass = document.getElementById('setSmtpPass').value.trim();
      if (smtpPass && !smtpPass.includes('...')) updates.smtp_password = smtpPass;

      const gmailSecret = document.getElementById('setGmailClientSecret').value.trim();
      if (gmailSecret && !gmailSecret.includes('...')) updates.gmail_client_secret = gmailSecret;

      const gmailRefresh = document.getElementById('setGmailRefreshToken').value.trim();
      if (gmailRefresh && !gmailRefresh.includes('...')) updates.gmail_refresh_token = gmailRefresh;

      const bufToken = document.getElementById('setBufferToken').value.trim();
      if (bufToken && !bufToken.includes('...')) updates.buffer_access_token = bufToken;

      const metaToken = document.getElementById('setMetaToken').value.trim();
      if (metaToken && !metaToken.includes('...')) updates.meta_access_token = metaToken;

      try {
        const res = await fetch(`${API_BASE}/settings`, {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify(updates)
        });
        if (res.ok) {
          alert('Settings saved successfully!');
          document.getElementById('setOpenRouterKey').value = '';
          document.getElementById('setNvidiaKey').value = '';
          document.getElementById('setTelegramToken').value = '';
          document.getElementById('setVercelToken').value = '';
          document.getElementById('setSmtpPass').value = '';
          document.getElementById('setGmailClientSecret').value = '';
          document.getElementById('setGmailRefreshToken').value = '';
          document.getElementById('setBufferToken').value = '';
          document.getElementById('setMetaToken').value = '';
          loadSettings();
        } else {
          const err = await res.json();
          alert('Failed to save settings: ' + (err.error || err.message || 'Unknown error'));
        }
      } catch (err) {
        alert('Network error saving settings: ' + err.message);
      }
    }

    // Modal Helpers
    function openModal(id) { document.getElementById(id).classList.add('active'); }
    function closeModal(id) { document.getElementById(id).classList.remove('active'); }
    function openNewScoutModal() {
      const defCity = document.getElementById('setDefaultCity');
      if (defCity && defCity.value) document.getElementById('scoutCity').value = defCity.value;
      const defCountry = document.getElementById('setDefaultCountry');
      if (defCountry && defCountry.value) document.getElementById('scoutCountry').value = defCountry.value;
      const defCat = document.getElementById('setDefaultCategory');
      if (defCat && defCat.value) document.getElementById('scoutCategory').value = defCat.value;
      const defLim = document.getElementById('setDefaultLimit');
      if (defLim && defLim.value) document.getElementById('scoutLimit').value = defLim.value;
      openModal('scoutModal');
    }
    function openNewJobModal() {
      openNewScoutModal();
    }
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
              source: 'overpass',
              approve_live_lookups: true,
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

    // Explicitly bind all handler functions to window so mobile browsers always resolve them
    window.switchView = switchView;
    window.refreshCurrentView = refreshCurrentView;
    window.refreshView = refreshView;
    window.toggleHoldPrompt = toggleHoldPrompt;
    window.releaseHold = releaseHold;
    window.openModal = openModal;
    window.closeModal = closeModal;
    window.openNewScoutModal = openNewScoutModal;
    window.openNewJobModal = openNewJobModal;
    window.openAuthModal = openAuthModal;
    window.submitScoutJob = submitScoutJob;
    window.submitReviewDecision = submitReviewDecision;
    window.saveAllSettings = saveAllSettings;
    window.saveAuthToken = saveAuthToken;
    window.retryJob = retryJob;
    window.runJob = runJob;
    window.recoverStaleJobs = recoverStaleJobs;
    window.buildWebsiteForLead = buildWebsiteForLead;
    window.openPreview = openPreview;
    window.openPreviewInNewTab = openPreviewInNewTab;
    window.openReviewDialog = openReviewDialog;
    window.updateExpectedConfirmation = updateExpectedConfirmation;
    window.toggleAiProviderFields = toggleAiProviderFields;
    window.toggleEmailProviderFields = toggleEmailProviderFields;
    window.loadOverview = loadOverview;
    window.loadJobs = loadJobs;
    window.loadLeads = loadLeads;
    window.loadProjects = loadProjects;
    window.loadSettings = loadSettings;

    // Resilient initial load
    if (document.readyState === 'loading') {
      window.addEventListener('DOMContentLoaded', loadOverview);
    } else {
      loadOverview();
    }
  </script>
</body>
</html>
"""
