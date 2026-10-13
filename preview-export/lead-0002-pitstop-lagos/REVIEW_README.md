# Human Review Package — Pitstop Lagos (`project-0002`)

> **Disclosure:** Uncommissioned preview draft prepared for internal evaluation · Not an authorized or official website of Pitstop Lagos.

## 1. Build & Validation Identity
- **Project ID:** `project-0002`
- **Lead ID:** `lead-0002` (`REAL_PUBLIC_DATA`)
- **Build ID:** `build-2026-10-13T012227z`
- **Validation ID:** `validation-2026-10-13T012227z` (`PASS`, score `100/100`)
- **Current Stage / Review Status:** `READY_FOR_REVIEW` / `PENDING`
- **Canonical Artifact Path:** `output/lead-0002/build-2026-10-13T012227z/index.html` (`11242` bytes, SHA-256 `6d58ba2594fb3f92713aef3671646c65c015d2041f93dc4e6bd5282f5753e99b`)
- **Exported Review Copy:** `preview-export/lead-0002-pitstop-lagos/index.html`

## 2. Supported Business Facts Rendered
- **`business_name`:** `"Pitstop Lagos"` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`category`:** `"restaurant"` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`city`:** `"Lagos"` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`description`:** `"A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos."` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`email`:** `"info@pitstoplagos.com"` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`instagram_url`:** `"https://www.instagram.com/pitstoplagos/"` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)
- **`services`:** `[{"name": "Healthy-Inspired Cafe & Dining", "description": "Fresh meals and recovery snacks."}, {"name": "Cycling Community Rides", "description": "Weekend group rides."}, {"name": "Wattbike Training Studio", "description": "Dedicated indoor cycling training."}, {"name": "Bicycle Service & Repair", "description": "Maintenance and accessories."}]` (tier: `SOURCE_URL_ONLY`, status: `SUPPORTED_BY_SOURCE_URL`, sources: `https://pitstoplagos.com/, https://www.instagram.com/pitstoplagos/`)

## 3. Unresolved / Conflicting / Withheld Facts (Omitted from HTML)
- **`address` (`CONFLICTING_WITHHELD`):** Phase 25 and Phase 26 records contain two street plot variants ('92A Younis Bashorun Street' vs '92b Younis Bashorun Street') without a preserved primary-source excerpt establishing which plot suffix is current; withheld from website rendering until verified. — conflicting values: `["92A Younis Bashorun Street, Victoria Island, Lagos, Nigeria", "92b Younis Bashorun Street, Victoria Island, Lagos"]`
- **`phone` (`CONFLICTING_WITHHELD`):** Phase 25 and Phase 26 records contain two telephone numbers ('+234 807 749 8324' vs '+234 817 999 9005') without a preserved primary-source excerpt establishing which number is current; withheld from website rendering until verified. — conflicting values: `["+234 807 749 8324", "+234 817 999 9005"]`
- **`opening_hours` (`OMITTED_UNVERIFIED`):** No verified operating hours in public research records; omitted from website.
- **`whatsapp` (`OMITTED_UNVERIFIED`):** No verified WhatsApp number in public research records; omitted from website.
- **`menu_items` (`OMITTED_UNVERIFIED`):** No verified food menu items or dish prices in public research records; omitted from website.

## 4. Claims Lacking Independent Preserved Primary-Source Excerpts (`SOURCE_URL_ONLY`)
- Raw HTTP/HTML responses from `https://pitstoplagos.com/` and `https://www.instagram.com/pitstoplagos/` are not stored in Git (`data/leads/*` is git-ignored).
- The following rendered facts are consistent across project records and linked to those public URLs, and should be visually confirmed by the human reviewer:
  - **`business_name`:** `"Pitstop Lagos"`
  - **`category`:** `"restaurant"`
  - **`city`:** `"Lagos"`
  - **`description`:** `"A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos."`
  - **`email`:** `"info@pitstoplagos.com"`
  - **`instagram_url`:** `"https://www.instagram.com/pitstoplagos/"`
  - **`services`:** `[{"name": "Healthy-Inspired Cafe & Dining", "description": "Fresh meals and recovery snacks."}, {"name": "Cycling Community Rides", "description": "Weekend group rides."}, {"name": "Wattbike Training Studio", "description": "Dedicated indoor cycling training."}, {"name": "Bicycle Service & Repair", "description": "Maintenance and accessories."}]`

## 5. Links in Rendered Artifact
- **External Links:** `https://www.instagram.com/pitstoplagos/`
- **Internal Anchors:** `#top`, `#about`, `#offerings`, `#contact`

## 6. How to Open Locally for Human Visual Review (`127.0.0.1` Only)
- **Direct file:** Open `preview-export/lead-0002-pitstop-lagos/index.html` in a desktop browser.
- **Local loopback server:** Run `python3 -m builder.cli --serve preview-export/lead-0002-pitstop-lagos --host 127.0.0.1 --port 8080` (or `python3 -m tracking.cli review-package project-0002 --export-dir preview-export/lead-0002-pitstop-lagos --serve --port 8080`) and open `http://127.0.0.1:8080/`.
- **How to stop:** Press Ctrl+C in the terminal running the server to stop the local preview server.

## 7. Validation Limitations
- Static HTML/CSS/content validation only; does not execute a real browser layout engine.
- Browser-based visual inspection at desktop and mobile viewports was not performed in the sandbox because no browser binary or Playwright package is installed.
- External URLs are checked for syntax and scheme safety offline; live HTTP reachability and current live contact/address resolution require human operator verification.
