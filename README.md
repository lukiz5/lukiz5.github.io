# lukiz5.github.io

## SENNS Dashboard

### What it is
Private SENNS dashboard for monitoring paid, organic, sales, and funnel performance.

### How it works
- GitHub Actions runs [`scripts/fetch_data.py`](/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/scripts/fetch_data.py).
- Fresh data is written to [`data/senns_data.json`](/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/data/senns_data.json).
- [`dashboard/index.html`](/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/dashboard/index.html) renders the UI from that JSON.
- Claude chat analyzes the current dashboard snapshot after the user enters an Anthropic API key for the current session.

### Manual run
GitHub → Actions → `Fetch SENNS Dashboard Data` → `Run workflow`

### Required GitHub Secrets
- `META_ACCESS_TOKEN`
- `META_AD_ACCOUNT_ID`
- `META_INSTAGRAM_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `MAILERLITE_API_KEY`

### Meta attribution note (2026-04-17)
Meta lead/purchase events use preferred_order deduplication (fix 2026-04-17).
Canonical lead event: `offsite_conversion.fb_pixel_lead` (LP form submit on `senns.studio/kit`).
Canonical purchase event: `offsite_conversion.fb_pixel_purchase` (Payhip webhook via Make).
See [`scripts/fetch_data.py`](/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/scripts/fetch_data.py) `LEAD_ACTION_PREFERRED_ORDER` and `PURCHASE_ACTION_PREFERRED_ORDER` for fallback logic.

### Access
Open `/dashboard`
Open `/dashboard/cover-generator.html` for the internal cover tool.

### Security note
- The dashboard uses a client-side password gate suitable for a static GitHub Pages setup.
- This reduces casual access but is not equivalent to full server-side authentication.
- The Anthropic API key is session-only and is not stored in `localStorage`.
