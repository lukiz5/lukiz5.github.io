# lukiz5.github.io

## SENNS Dashboard

### What it is
Private SENNS dashboard for monitoring paid, organic, revenue, and funnel performance.

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
- `LEMONSQUEEZY_API_KEY`
- `MAILERLITE_API_KEY`

### Access
Open `/dashboard`

### Security note
- The dashboard uses a client-side password gate suitable for a static GitHub Pages setup.
- This reduces casual access but is not equivalent to full server-side authentication.
- The Anthropic API key is session-only and is not stored in `localStorage`.
