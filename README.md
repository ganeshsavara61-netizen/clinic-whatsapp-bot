# Clinic WhatsApp Assistant (real backend)

A WhatsApp bot for a clinic. Patients message the clinic's WhatsApp number; the bot answers in Telugu or English using Google Gemini, books appointments, and tells the clinic owner. It uses the **official WhatsApp Cloud API** (Meta). Python standard library only, no packages to install.

## Status: read this first

| Part | Status |
|---|---|
| Webhook verify, signature check, duplicate-message filter | Tested (automated, local) |
| Booking rules (future date, clinic hours, closed days, double-booking, one per patient per day, cancel) | Tested (automated, local) |
| Emergency words (English and Telugu) skip the AI and say call 108 | Tested (automated, local) |
| Gemini fallback when the AI fails | Tested with mocks |
| Real call to Gemini (model names, JSON reply format) | **NOT tested live. Needs your Gemini key** |
| Real send/receive through WhatsApp Cloud API | **NOT tested live. Needs your Meta app** |
| Deploy on Render free | **NOT tested live** |

Tests use fake Gemini and WhatsApp replies. Real services can differ, so expect to fix small things in the first live test (check Render logs).

## What it does not do
- No automatic appointment reminders. Reminders outside 24 hours after the patient's last message need a Meta-approved template message and a database that survives restarts. Not built.
- Bookings are stored in a small database file. **Render free wipes it on every restart or redeploy.** So each booking is also sent to the clinic owner on WhatsApp and (optionally) saved to a Google Sheet. Treat the Google Sheet as the real record.
- Text only. Voice notes and images get a "please type" reply.
- Not medical advice. The bot is told never to diagnose or name medicines. Emergency keywords get a fixed 108 reply.
- It is not a replacement for a receptionist. Tell clinic owners it is a first version.

## Cost: free to start, limits apply
- Meta test number: free to try, only up to 5 phone numbers you verify.
- Render free web service: free, sleeps after 15 minutes idle, about 1 minute to wake up ([Render docs](https://render.com/docs/free)).
- Gemini free tier: free of charge with rate limits, and Google may use free-tier content to improve its products ([pricing](https://ai.google.dev/gemini-api/docs/pricing)). Do not put real patient records through it without telling clinics.
- WhatsApp messaging: Meta's own pages disagree about what is free (one says replies inside the 24-hour window are free, another says the first 1,000 service conversations per month are free from 1 Oct 2026). Do not promise "unlimited free". Check [Meta pricing](https://developers.facebook.com/docs/whatsapp/pricing/) before a clinic goes live. Adding your own real number may require Meta business verification and a payment method on file. That part is unverified here.

---

## Setup steps for you (Windows laptop). About 45 minutes.

Menu names at Meta change often. If a button has a different name, look for the closest one.

### A. Get this code into your Render account (nothing to install)
The code is already at https://github.com/ganeshsavara61-netizen/clinic-whatsapp-bot . You do not need to download anything.

### B. Create the Gemini key (2 minutes)
1. Open https://aistudio.google.com/apikey and sign in with your Google account.
2. Click **Create API key**. If it asks for a project, pick **Create API key in new project**.
3. Copy the key. Keep it private (do not post it in chat screenshots or GitHub). You will paste it into Render in step D.

### C. Create the Meta app and test number
1. Open https://developers.facebook.com and log in with Facebook. Accept the developer terms and verify your phone if asked.
2. Click **My Apps** then **Create App**.
3. Choose use case **Other**, then app type **Business**. Name it `Clinic Bot`. Create it.
4. On the dashboard find **WhatsApp** and click **Set up**. Create or pick a Meta business portfolio when asked.
5. Left menu: **WhatsApp** then **API Setup**. Write down (these are not secret):
   - **Phone number ID** (long number under the test number)
   - **WhatsApp Business Account ID**
6. In the same page, in the **To** box, choose **Manage phone number list** and add your own WhatsApp number with +91. Enter the code WhatsApp sends you. (Maximum 5 numbers on the test number. Patients not on this list cannot get replies yet.)
7. Click **Generate access token** on that page. This temporary token lasts about 24 hours. Good enough for first testing. Copy it.
8. Get the App Secret: left menu **App settings** then **Basic**, click **Show** next to **App secret**, copy it.
9. Optional, permanent token (do this before showing clinics): open https://business.facebook.com/settings , **Users** then **System users** then **Add**, name it `bot`, role **Admin**. Click **Assign assets**, pick your app, give **Full control**. Click **Generate token**, pick your app, expiry **Never**, tick `whatsapp_business_messaging` and `whatsapp_business_management`. Copy the token.

### D. Deploy on Render (free)
1. Open https://render.com and **Sign up with GitHub** (use ganeshsavara61-netizen). If Render asks for a credit or debit card for the free plan, STOP and tell me. Do not enter a card. (I could not verify whether it asks.)
2. Click **New** then **Web Service**. Choose **Build and deploy from a Git repository**, pick `clinic-whatsapp-bot`. (If it is not listed, click **Configure account** and allow that repo.)
3. Fill in: Language **Python 3**. Build Command `echo ok`. Start Command `python server.py`. Instance type **Free**.
4. Open **Advanced** then **Add Environment Variable**. Add these (names must match exactly):
   - `VERIFY_TOKEN` = any word you invent, for example `clinic-verify-4821`
   - `WHATSAPP_TOKEN` = the token from step C7 (or C9)
   - `PHONE_NUMBER_ID` = from step C5
   - `APP_SECRET` = from step C8
   - `GEMINI_API_KEY` = from step B
   - `ADMIN_TOKEN` = another word you invent (for the bookings page)
   - `OWNER_WHATSAPP` = clinic owner's number with country code and no plus, like `919876543210` (optional)
5. Click **Deploy Web Service**. Wait for **Live**. Copy the address at the top, like `https://clinic-whatsapp-bot-xxxx.onrender.com`.
6. Open `that-address/health` in your browser. You should see `clinic whatsapp bot ok`. (First load can take about a minute.)

### E. Connect Meta to your server
1. Meta app, left menu **WhatsApp** then **Configuration**.
2. **Webhook**, click **Edit**. Callback URL = `https://YOUR-RENDER-ADDRESS/webhook`. Verify token = the exact `VERIFY_TOKEN` you chose. Click **Verify and save**. If it fails, open the `/health` page first (wakes the server) and retry.
3. Under **Webhook fields**, find **messages** and click **Subscribe**.

### F. Test
1. From your own WhatsApp (the number you added in C6), send a message to the test number shown on API Setup, like `Fee entha?`.
2. You should get an answer in a few seconds (up to a minute if the server was asleep).
3. Try `Tomorrow 11 am appointment kavali, name Ravi`. Then open `https://YOUR-RENDER-ADDRESS/admin?token=YOUR_ADMIN_TOKEN` to see the booking.
4. If nothing arrives: Render dashboard, your service, **Logs**. Look for `gemini ... failed` (key or model problem) or `WhatsApp send failed` (token or phone number ID problem). Send me the log lines, never the keys.

### G. Edit the clinic details
In GitHub open `clinic.example.json`, click the pencil, edit name, doctor, address, fee, hours, extra_info (hours are `[open, close, open, close]` per day, 24h format, `null` = closed). Commit. Render redeploys itself.

### H. Keep it awake and keep records (optional, free)
- Wake-up pings: at https://cron-job.org create a free job that opens `YOUR-RENDER-ADDRESS/health` every 10 minutes. Render gives 750 free hours a month, one always-on service uses about 744, so one service fits. Unverified.
- Google Sheet record: create a Google Sheet, **Extensions** then **Apps Script**, paste:
```js
function doPost(e) {
  var d = JSON.parse(e.postData.contents);
  SpreadsheetApp.getActiveSheet().appendRow([new Date(), d.event, d.name||'', d.phone||'', d.date||'', d.time||'', d.reason||d.message||'']);
  return ContentService.createTextOutput('ok');
}
```
  Click **Deploy** then **New deployment** then type **Web app**, Execute as **Me**, access **Anyone**. Copy the URL and add it in Render as `SHEETS_WEBHOOK_URL`. (Not tested.)

## Going live with a clinic's real number (after the test works)
- Needs a number that is not already on the WhatsApp or WhatsApp Business app, and a display name Meta approves. In API Setup click **Add phone number**.
- Patients can only be reached freely after they message first. Keep all flows patient-starts-chat.
- Get the clinic's written OK before patient messages go through Gemini free tier.
- For many clinics or reliability, upgrade hosting (paid), which this project does not require you to buy now.

## Run tests
`python test_server.py` (no keys needed).

## Files
`server.py` the whole bot. `clinic.example.json` clinic details. `render.yaml` optional Render blueprint. `test_server.py` tests.
