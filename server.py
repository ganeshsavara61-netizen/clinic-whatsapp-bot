"""Clinic WhatsApp assistant: WhatsApp Cloud API webhook + Gemini + appointment booking.

Standard library only. Configuration comes from environment variables (see README).
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v25.0")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
IST = timezone(timedelta(hours=5, minutes=30))
DB_LOCK = threading.Lock()

EMERGENCY_WORDS = [
    "chest pain", "heart attack", "can't breathe", "cannot breathe", "breathing problem",
    "unconscious", "snake", "poison", "suicide", "bleeding heavily", "severe bleeding",
    "accident", "stroke", "seizure",
    "ఛాతి నొప్పి", "గుండె నొప్పి", "శ్వాస ఆడట్లేదు", "ఊపిరి", "స్పృహ", "పాము", "విషం",
    "ఆత్మహత్య", "రక్తస్రావం", "ప్రమాదం", "ఫిట్స్",
    "chathi noppi", "gunde noppi", "oopiri", "pamu",
]
EMERGENCY_REPLY = (
    "This sounds like an emergency. Please call 108 (ambulance) now or go to the nearest "
    "hospital immediately. Do not wait for a chat reply.\n\n"
    "ఇది అత్యవసర పరిస్థితిలా ఉంది. వెంటనే 108 కి కాల్ చేయండి లేదా దగ్గరలోని ఆసుపత్రికి వెళ్ళండి."
)
FALLBACK_REPLY = (
    "Thank you for your message. Our assistant is unavailable right now, so the clinic staff "
    "will reply to you soon.\n\nమీ సందేశానికి ధన్యవాదాలు. ప్రస్తుతం అసిస్టెంట్ అందుబాటులో లేదు, "
    "క్లినిక్ సిబ్బంది త్వరలో మీకు రిప్లై ఇస్తారు."
)


# ---------------------------------------------------------------- config
def env(name, default=""):
    return os.environ.get(name, default)


def load_clinic():
    raw = env("CLINIC_JSON")
    if raw:
        return json.loads(raw)
    path = env("CLINIC_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinic.json"))
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinic.example.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- http helper (patched in tests)
def http_json(url, payload, headers, timeout=20):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body}


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(env("DB_PATH", "clinic.db"), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, wa_id TEXT, "
        "name TEXT, date TEXT, time TEXT, reason TEXT, status TEXT DEFAULT 'booked', created TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS seen (msg_id TEXT PRIMARY KEY, ts REAL)")
    return conn


def seen_before(msg_id):
    with DB_LOCK, db() as c:
        if c.execute("SELECT 1 FROM seen WHERE msg_id=?", (msg_id,)).fetchone():
            return True
        c.execute("INSERT INTO seen VALUES (?,?)", (msg_id, time.time()))
        c.execute("DELETE FROM seen WHERE ts < ?", (time.time() - 86400,))
        return False


# ---------------------------------------------------------------- booking logic
def now_ist():
    return datetime.now(IST)


def slot_times(clinic, date_obj):
    hours = clinic.get("hours", {}).get(DAYS[date_obj.weekday()])
    if not hours:
        return []
    step = int(clinic.get("slot_minutes", 15))
    out = []
    for i in range(0, len(hours) - 1, 2):
        start = datetime.strptime(hours[i], "%H:%M")
        end = datetime.strptime(hours[i + 1], "%H:%M")
        t = start
        while t + timedelta(minutes=step) <= end:
            out.append(t.strftime("%H:%M"))
            t += timedelta(minutes=step)
    return out


def free_slots(clinic, date_str, limit=6):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    with DB_LOCK, db() as c:
        taken = {r[0] for r in c.execute(
            "SELECT time FROM bookings WHERE date=? AND status='booked'", (date_str,))}
    n = now_ist()
    res = []
    for t in slot_times(clinic, d):
        if t in taken:
            continue
        if d == n.date() and t <= n.strftime("%H:%M"):
            continue
        res.append(t)
    return res[:limit]


def try_book(clinic, wa_id, name, date_str, time_str, reason):
    """Returns (ok, message_en). Validates everything server-side; never trusts the AI."""
    name = (name or "").strip()[:60]
    if not name:
        return False, "I need the patient's name to book."
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        datetime.strptime(time_str, "%H:%M")
    except (ValueError, TypeError):
        return False, "I could not understand the date or time. Please give a date and time."
    n = now_ist()
    if d < n.date() or (d == n.date() and time_str <= n.strftime("%H:%M")):
        return False, "That time has already passed. Please choose a future time."
    if d > n.date() + timedelta(days=60):
        return False, "Bookings are only possible within the next 60 days."
    if time_str not in slot_times(clinic, d):
        free = free_slots(clinic, date_str)
        if not slot_times(clinic, d):
            return False, f"The clinic is closed on {d.strftime('%A %d %b')}. Please pick another day."
        return False, "That time is outside clinic hours. Free times that day: " + (", ".join(free) or "none")
    with DB_LOCK, db() as c:
        if c.execute("SELECT 1 FROM bookings WHERE date=? AND time=? AND status='booked'",
                     (date_str, time_str)).fetchone():
            clash = True
        else:
            clash = False
            dup = c.execute(
                "SELECT 1 FROM bookings WHERE wa_id=? AND date=? AND status='booked'",
                (wa_id, date_str)).fetchone()
            if dup:
                return False, "You already have a booking that day. Reply CANCEL to cancel it first."
            c.execute(
                "INSERT INTO bookings (wa_id,name,date,time,reason,created) VALUES (?,?,?,?,?,?)",
                (wa_id, name, date_str, time_str, (reason or "")[:200], n.isoformat()))
    if clash:
        free = free_slots(clinic, date_str)
        return False, "That slot is taken. Free times that day: " + (", ".join(free) or "none")
    return True, f"Booked: {name}, {d.strftime('%A %d %b %Y')} at {time_str}."


def cancel_booking(wa_id):
    n = now_ist()
    with DB_LOCK, db() as c:
        row = c.execute(
            "SELECT id,date,time FROM bookings WHERE wa_id=? AND status='booked' AND date>=? "
            "ORDER BY date,time LIMIT 1", (wa_id, n.strftime("%Y-%m-%d"))).fetchone()
        if not row:
            return None
        c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (row[0],))
        return row


# ---------------------------------------------------------------- Gemini
def system_prompt(clinic):
    n = now_ist()
    return f"""You are the WhatsApp reception assistant of {clinic.get('name')}.
Today is {n.strftime('%A %Y-%m-%d')}, current time {n.strftime('%H:%M')} IST.
Reply in the language the patient writes in: Telugu (Telugu script, or answer in Telugu script even if they type romanized Telugu) or English. Keep replies short, polite, plain words.
Clinic facts (the ONLY facts you may state):
{json.dumps(clinic, ensure_ascii=False)}
Rules:
- Never give diagnosis, medicine names or doses. For medical questions say the doctor must advise and offer to book.
- Never invent fees, timings, doctors or services not listed above. If unknown, say the staff will confirm.
- To book you need: patient name, date, time. Ask for what is missing. Use free times only from the clinic hours.
- When you have name+date+time, set action to "book". If patient wants to cancel, action "cancel". If they want a human or you cannot help, action "handoff". Otherwise "none".
- Output ONLY JSON: {{"reply": "<text>", "action": "none|book|cancel|handoff", "booking": {{"name": "", "date": "YYYY-MM-DD", "time": "HH:MM"}}, "reason": ""}}
- Dates must be resolved to YYYY-MM-DD using today's date above. Time in 24h HH:MM."""


def call_gemini(clinic, history):
    key = env("GEMINI_API_KEY")
    if not key:
        return None
    models = [m for m in [env("GEMINI_MODEL", "gemini-3.1-flash-lite")] +
              env("GEMINI_FALLBACK_MODELS", "gemini-3-flash-preview").split(",") if m.strip()]
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt(clinic)}]},
        "contents": contents,
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3,
                             "maxOutputTokens": 600},
    }
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m.strip()}:generateContent"
        try:
            res = http_json(url, payload, {"x-goog-api-key": key})
            text = res["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:  # try next model, then fall back
            log(f"gemini {m} failed: {type(e).__name__} {str(e)[:120]}")
    return None


# ---------------------------------------------------------------- WhatsApp
def send_text(to, body):
    token, pid = env("WHATSAPP_TOKEN"), env("PHONE_NUMBER_ID")
    if not token or not pid:
        log("WhatsApp send skipped: WHATSAPP_TOKEN / PHONE_NUMBER_ID not set")
        return False
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{pid}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
               "text": {"body": body[:4000], "preview_url": False}}
    try:
        http_json(url, payload, {"Authorization": f"Bearer {token}"})
        return True
    except Exception as e:
        log(f"WhatsApp send failed: {type(e).__name__} {str(e)[:150]}")
        return False


def notify_owner(text, row):
    owner = env("OWNER_WHATSAPP")
    if owner:
        # Only delivers if the owner messaged this number in the last 24h (WhatsApp rule).
        send_text(owner, text)
    url = env("SHEETS_WEBHOOK_URL")
    if url:
        try:
            http_json(url, row, {})
        except Exception as e:
            log(f"sheet webhook failed: {type(e).__name__} {str(e)[:100]}")


# ---------------------------------------------------------------- conversation
HISTORY = {}
RATE = {}


def rate_limited(wa_id, limit=20, window=600):
    now = time.time()
    q = [t for t in RATE.get(wa_id, []) if now - t < window]
    q.append(now)
    RATE[wa_id] = q
    return len(q) > limit


def handle_text(clinic, wa_id, text):
    """Returns the reply text for one incoming patient message."""
    text = (text or "").strip()[:1000]
    low = text.lower()
    if any(w in low for w in EMERGENCY_WORDS):
        return EMERGENCY_REPLY
    if rate_limited(wa_id):
        return None
    if low in ("cancel", "cancel booking", "రద్దు"):
        row = cancel_booking(wa_id)
        if row:
            notify_owner(f"Cancelled: {wa_id} {row[1]} {row[2]}",
                         {"event": "cancelled", "phone": wa_id, "date": row[1], "time": row[2]})
            return f"Your booking on {row[1]} at {row[2]} is cancelled.\nమీ బుకింగ్ రద్దు చేయబడింది."
        return "I could not find an upcoming booking for this number."
    hist = HISTORY.setdefault(wa_id, [])
    hist.append(("user", text))
    del hist[:-10]
    out = call_gemini(clinic, hist)
    if not isinstance(out, dict) or not out.get("reply"):
        hist.pop()
        return FALLBACK_REPLY
    reply = str(out["reply"])
    action = out.get("action", "none")
    if action == "book":
        b = out.get("booking") or {}
        ok, msg = try_book(clinic, wa_id, b.get("name"), b.get("date"), b.get("time"), out.get("reason"))
        if ok:
            notify_owner(f"New booking: {b.get('name')} {b.get('date')} {b.get('time')} ({wa_id}) {out.get('reason','')}",
                         {"event": "booked", "phone": wa_id, "name": b.get("name"), "date": b.get("date"),
                          "time": b.get("time"), "reason": out.get("reason", "")})
            reply = f"{msg}\n\n{reply}" if msg not in reply else reply
        else:
            reply = msg + "\n\n(Please tell me another time.)"
    elif action == "cancel":
        row = cancel_booking(wa_id)
        reply = (f"Your booking on {row[1]} at {row[2]} is cancelled." if row
                 else "I could not find an upcoming booking for this number.")
    elif action == "handoff":
        notify_owner(f"Patient {wa_id} needs a human reply. Last message: {text[:200]}",
                     {"event": "handoff", "phone": wa_id, "message": text[:200]})
    hist.append(("model", reply))
    del hist[:-10]
    return reply


def process_payload(clinic, payload):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            for msg in val.get("messages", []):
                mid, wa_id = msg.get("id"), msg.get("from")
                if not mid or not wa_id or seen_before(mid):
                    continue
                if msg.get("type") == "text":
                    reply = handle_text(clinic, wa_id, msg["text"].get("body", ""))
                else:
                    reply = ("I can read text messages only. Please type your question.\n"
                             "దయచేసి మీ ప్రశ్నను టెక్స్ట్‌లో టైప్ చేయండి.")
                if reply:
                    send_text(wa_id, reply)


def valid_signature(body, header):
    secret = env("APP_SECRET")
    if not secret:
        return env("ALLOW_UNSIGNED") == "1"
    if not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header[7:])


def log(msg):
    print(f"[{now_ist().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    clinic = None

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/health"):
            return self._send(200, "clinic whatsapp bot ok")
        if u.path == "/webhook":
            vt = env("VERIFY_TOKEN")
            if vt and q.get("hub.mode") == ["subscribe"] and q.get("hub.verify_token") == [vt]:
                return self._send(200, q.get("hub.challenge", [""])[0])
            return self._send(403, "forbidden")
        if u.path == "/admin":
            tok = env("ADMIN_TOKEN")
            if not tok or q.get("token") != [tok]:
                return self._send(403, "forbidden")
            with DB_LOCK, db() as c:
                rows = c.execute("SELECT date,time,name,wa_id,reason,status FROM bookings "
                                 "ORDER BY date,time").fetchall()
            html = "<meta charset=utf-8><h3>Bookings (resets when the free host restarts)</h3><table border=1 cellpadding=4>"
            for r in rows:
                html += "<tr>" + "".join(f"<td>{str(x).replace('<','&lt;')}</td>" for x in r) + "</tr>"
            return self._send(200, html + "</table>", "text/html; charset=utf-8")
        return self._send(404, "not found")

    def do_POST(self):
        if urlparse(self.path).path != "/webhook":
            return self._send(404, "not found")
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            return self._send(413, "too large")
        body = self.rfile.read(n)
        if not valid_signature(body, self.headers.get("X-Hub-Signature-256")):
            return self._send(403, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return self._send(400, "bad json")
        # Reply 200 fast so Meta does not retry; process in the background.
        threading.Thread(target=self._safe, args=(payload,), daemon=True).start()
        self._send(200, "ok")

    def _safe(self, payload):
        try:
            process_payload(self.clinic, payload)
        except Exception as e:
            log(f"processing error: {type(e).__name__} {str(e)[:150]}")


def main():
    Handler.clinic = load_clinic()
    port = int(env("PORT", "8080"))
    for k in ("VERIFY_TOKEN", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "APP_SECRET", "GEMINI_API_KEY"):
        log(f"{k}: {'set' if env(k) else 'MISSING'}")
    log(f"listening on {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
