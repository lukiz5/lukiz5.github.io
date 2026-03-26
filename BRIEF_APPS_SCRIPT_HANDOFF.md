# SENNS Brief - Apps Script Backend Handoff

## What we are building
We are keeping the custom SENNS brief page on `senns.studio` and moving the backend work to Google Apps Script.

Final flow:
1. Client opens `brief.html` or `en/brief.html`.
2. Client fills the custom SENNS form.
3. Client can optionally upload files directly in the form.
4. The page sends the form payload to a deployed Google Apps Script Web App.
5. Apps Script saves uploaded files to Google Drive.
6. Apps Script generates a client PDF brief with file links.
7. Apps Script emails the client PDF to the client.
8. Apps Script generates an internal `.md` brief for the studio.
9. Apps Script emails the `.md` file directly to `hello@senns.studio`.
10. The client only sees a thank-you state and a download button for the PDF.

## What I implemented

### Frontend
Updated:
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/brief.html`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/en/brief.html`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/brief-config.js`

Changes made:
- removed the old visible `.md` download from the client UI
- switched the brief flow from local generation to backend submission
- added hidden iframe submission bridge for Apps Script `POST`
- added status handling for:
  - backend not configured
  - upload read failure
  - submission failure
  - timeout
- added success output that shows only:
  - confirmation
  - client PDF download button
- changed file upload handling to real payload upload from the browser
- added total upload limit handling:
  - max 10 files
  - max 10 MB each
  - max 20 MB total
- relaxed the `Other` field validation so checkbox selection alone is enough
- removed old front-end PDF / Markdown generation logic from the client source

### Backend package for Google Apps Script
Created:
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/Code.gs`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/appsscript.json`

What the Apps Script backend does:
- accepts the posted brief payload
- validates required fields and upload limits
- stores uploaded files in Google Drive
- creates public view/download links for uploaded files
- builds the client PDF in Google Docs and exports it as PDF
- emails the PDF to the client
- builds the internal Markdown brief for the studio
- emails the `.md` directly to `hello@senns.studio`
- returns a success payload back to the page via `postMessage`

## Why this is the right compromise
This keeps:
- your custom SENNS design
- your own URL
- no separate server to host
- no extra paid email provider
- no Google Form in front of the client

Google Apps Script gives us the backend pieces we need:
- file storage via Google Drive
- outgoing email via Google Workspace / MailApp
- a simple web endpoint via Web App deployment

## Important implementation notes
- The client does not get access to the internal `.md` file in the UI.
- The `.md` is generated only on the Apps Script side and sent directly to `hello@senns.studio`.
- File links inside the PDF point to Google Drive shared links.
- Current upload limits are intentionally conservative because files are sent as base64 from the browser.
- If later you want larger uploads, the next step would be direct-to-storage upload instead of posting files through Apps Script.

## Files changed in this repo
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/brief.html`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/en/brief.html`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/brief-config.js`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/Code.gs`
- `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/appsscript.json`

## What still requires manual work from you
These are the only manual steps left because they require access to your Google account.

1. Create a new Google Apps Script project.
2. Paste in `Code.gs` and `appsscript.json` from:
   - `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/Code.gs`
   - `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/docs/google-apps-script/brief-backend/appsscript.json`
3. Deploy the script as a Web App:
   - Execute as: `Me`
   - Who has access: `Anyone`
4. Copy the deployed Web App URL.
5. Paste that URL into:
   - `/Users/apple/Desktop/SENNS.STUDIO/Strona/SENNS.STUDIO WEB/brief-config.js`
   in `WEB_APP_URL`
6. Commit and push that config change.
7. Test one real submission from both:
   - `https://senns.studio/brief.html`
   - `https://senns.studio/en/brief.html`
8. Confirm:
   - client receives PDF by email
   - PDF includes file links
   - studio receives `.md` at `hello@senns.studio`

## Suggested Google Drive structure
The script creates this automatically:
- `SENNS Brief Intake/`
- `SENNS Brief Intake/submissions/`
- one folder per submission
- inside each submission:
  - uploaded files
  - generated client PDF

## If something fails during your manual setup
Check these first:
- the Web App URL is pasted correctly into `brief-config.js`
- the Web App is deployed to the latest version
- Web App access is set to `Anyone`
- Google account has permission to send mail from Apps Script
- Drive sharing is allowed for `Anyone with the link`

## Current status
Implementation in the website repo is ready for Apps Script hookup.
The remaining work is only Google-side deployment and inserting the final Web App URL.
