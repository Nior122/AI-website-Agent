# Test & Release Status

This document records the baseline test suite audit, the defect root causes, their verified fixes, and the regression-free release gate verification.

---

## 1. Test Suite Verification Summary

- **Total Test Cases**: 709
- **Passing**: 708
- **Skipped**: 1 (`test_render_blueprint_matches_documented_topology` - requires Docker/Render environment)
- **Failures**: **0**
- **Errors**: **0**
- **Platform**: Windows 11 (Python 3.11.13)
- **Status**: **100% GREEN (PASS)**

---

## 2. Audit of Fixed Deficiencies

During the Phase 50 takeover audit, 5 pre-existing test failures were identified and resolved with minimal, architecture-aligned fixes:

### 1. Nested Subprocess Winsock Provider Error (`WinError 10106`)
- **File**: `tests/test_phase42_integration_setup_and_preflight.py`
- **Test**: `FreshCloneBehaviourTests.test_the_suite_reads_workspace_records_without_depending_on_module_order`
- **Symptom**: `OSError: [WinError 10106] The requested service provider could not be loaded or initialized` during `ThreadingHTTPServer(("127.0.0.1", 0), handler)` inside a spawned subprocess.
- **Root Cause**: The test created a subprocess passing `env={"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ["PATH"]}`. On Windows, stripping `SystemRoot` / `WINDIR` from the environment prevents the Windows Sockets DLL from loading network service providers.
- **Fix**: Replaced the stripped environment with `dict(os.environ, PYTHONPATH=str(REPO_ROOT))`, preserving OS core environment variables while isolating repository modules.

### 2. Unix `which` Command in Windows Environment (`WinError 2`)
- **File**: `tests/test_phase41_integration_and_recovery.py`
- **Test**: `UncertainPublishCliTests.test_a_resolved_retry_is_recorded_then_fails_on_the_absent_cli`
- **Symptom**: `FileNotFoundError: [WinError 2] The system cannot find the file specified` when invoking `subprocess.run(["which", "vercel"])`.
- **Root Cause**: `which` is a POSIX binary that does not exist in standard Windows shells.
- **Fix**: Replaced with Python standard library `shutil.which("vercel", path=...)` for platform-agnostic executable location.

### 3 & 4. Host Environment Leakage of Global Vercel CLI
- **File**: `tests/test_phase39_monitoring_and_operational_controls.py`
- **Tests**:
  - `MonitoringTestCase.test_cli_surfaces_report_state_and_leave_the_workspace_untouched`
  - `MonitoringTestCase.test_health_reports_read_existing_records_without_adding_state`
- **Symptom**: `AssertionError: True != False` when asserting `configuration["cli_available"] == False`.
- **Root Cause**: The host machine had Vercel CLI installed globally (`%APPDATA%\npm\vercel.cmd`). Because `provider_configuration()` checks the system `PATH`, the tests received `True` instead of the fixture's expected `False`.
- **Fix**: Isolated the fixture in `MonitoringTestCase.setUp()` using `mock.patch("deployment.deployer.shutil.which", return_value=None)` with clean teardown via `addCleanup()`.

### 5. Subprocess Execution Environment Leakage of Global Vercel CLI
- **File**: `tests/test_phase41_integration_and_recovery.py`
- **Test**: `UncertainPublishAuditTests.test_an_uncertain_publish_reaches_every_operator_surface`
- **Symptom**: `AssertionError: True != False` when inspecting output of `orchestration.cli deployment-status`.
- **Root Cause**: The `_Disposable.module()` test harness invoked CLI commands as subprocesses inheriting the host's `PATH`.
- **Fix**: Implemented `_Disposable._clean_env()` which strips paths containing `vercel*` executables from `PATH` for CLI subprocesses under test, ensuring tests execute with complete isolation from host machine package state.

---

## 3. Verification Commands

To reproduce the full suite test run:
```powershell
python -m unittest discover -s tests -p "test_*.py"
```

To run focused operational tests:
```powershell
python -m unittest tests.test_phase39_monitoring_and_operational_controls
python -m unittest tests.test_phase41_integration_and_recovery
python -m unittest tests.test_phase42_integration_setup_and_preflight
python -m unittest tests.test_scout_agent
python -m unittest tests.test_tavily_enrichment
```

---

## 4. Stage 8 Live Production Security & Operational Acceptance

| Verification Gate | Target Environment | Method / Command | Result |
| --- | --- | --- | --- |
| **API Credential Rotation** | Render (`srv-db7cartg1s2s739vrasg`) | Generated 43-char URL-safe bearer token; updated env var | **PASS**: Old token refused `HTTP 401`; new token accepted `HTTP 200` |
| **Repo Secret Cleanliness** | Git version control | `git grep` token scan across all commits and files | **PASS**: Zero credentials found in repository |
| **Health Probe** | `https://ai-website-agent-api.onrender.com` | `GET /healthz` (unauthenticated) | **PASS**: `HTTP 200 {"status": "ok"}` |
| **Readiness Probe** | `https://ai-website-agent-api.onrender.com` | `GET /readiness` (authenticated) | **PASS**: `HTTP 200 {"status": "READY", "storage_backend": "postgres", "production_mode": true}` |
| **Transactional Persistence** | Supabase Postgres Pooler | `POST /jobs` test job (`stage8-verify-1792557783`) | **PASS**: `HTTP 201 Created`; verified read-back, idempotency replay, and restart survival |
| **Refusal & Boundary Gates** | Render API | Unsupported verbs (`POST /healthz`), invalid job types | **PASS**: Refused with `405 Method Not Allowed` / `400 Bad Request` |
| **Smoke Test A: Website Pipeline** | Local Isolated Fixtures | `tests/test_website_builder.py`, `tests/test_validation.py`, `tests/test_full_system.py` | **PASS**: 36 passed in 6.09s |
| **Smoke Test B: Social Pipeline** | Local Isolated Fixtures | `tests/test_social_engine.py` | **PASS**: 21 passed in 0.23s |
| **Smoke Test C: Inbound Intake** | Local Isolated Fixtures | `tests/test_phase47_inbound_intake.py` | **PASS**: 32 passed in 5.17s |
| **Smoke Test D: Outbound Gates** | Local Isolated Fixtures | `tests/test_phase49_approved_sending.py`, `tests/test_outreach.py` | **PASS**: 97 passed in 16.11s |
| **Smoke Test E: Operator Console** | Local Isolated Fixtures | `tests/test_phase48_operator_channels.py` | **PASS**: 86 passed in 6.90s |
| **n8n Dedicated Workflows** | Personal Project `gF2ObH2mvjWtE5pb` | Workflows `01`–`05` audited via exports & execution history | **PASS**: All 5 workflows preserved inactive (`active: false`) |
