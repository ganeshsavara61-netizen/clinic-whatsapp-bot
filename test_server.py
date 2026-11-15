import hashlib, hmac, json, os, tempfile, threading, time, unittest, urllib.request, urllib.error
from datetime import timedelta
from http.server import ThreadingHTTPServer

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(VERIFY_TOKEN="vt", APP_SECRET="sec", WHATSAPP_TOKEN="wt", PHONE_NUMBER_ID="123",
                  GEMINI_API_KEY="gk", ADMIN_TOKEN="adm")
import server as s

CLINIC = s.load_clinic()
SENT, GEM = [], []


def fake_http(url, payload, headers, timeout=20):
    if "generativelanguage" in url:
        assert headers.get("x-goog-api-key") == "gk"
        r = GEM.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(r)}]}}]}
    assert "graph.facebook.com" in url and headers["Authorization"] == "Bearer wt"
    SENT.append((payload["to"], payload["text"]["body"]))
    return {"messages": [{"id": "x"}]}


s.http_json = fake_http


def next_weekday(wd):
    d = s.now_ist().date() + timedelta(days=2)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


class T(unittest.TestCase):
    def setUp(self):
        SENT.clear(); GEM.clear(); s.HISTORY.clear(); s.RATE.clear()
        with s.db() as c:
            c.execute("DELETE FROM bookings"); c.execute("DELETE FROM seen")

    def book(self, wa, d, t, name="Ravi"):
        GEM.append({"reply": "Done", "action": "book",
                    "booking": {"name": name, "date": str(d), "time": t}, "reason": "fever"})
        return s.handle_text(CLINIC, wa, "book")

    def test_booking_ok_and_double_book_rejected(self):
        d = next_weekday(0)
        r = self.book("91111", d, "10:00")
        self.assertIn("Booked: Ravi", r)
        r2 = self.book("91222", d, "10:00", "Sita")
        self.assertIn("taken", r2)
        self.assertNotIn("Booked", r2)

    def test_sunday_closed_and_past_and_outside_hours(self):
        self.assertIn("closed", self.book("91111", next_weekday(6), "10:00"))
        self.assertIn("passed", self.book("91111", s.now_ist().date() - timedelta(days=1), "10:00"))
        self.assertIn("outside", self.book("91111", next_weekday(1), "15:00"))
        self.assertIn("name", self.book("91111", next_weekday(1), "10:00", name=""))

    def test_one_booking_per_day_and_cancel(self):
        d = next_weekday(2)
        self.book("91111", d, "10:00")
        self.assertIn("already have", self.book("91111", d, "10:15"))
        self.assertIn("cancelled", s.handle_text(CLINIC, "91111", "cancel"))
        self.assertIn("Booked", self.book("91111", d, "10:15"))

    def test_emergency_skips_ai(self):
        self.assertIn("108", s.handle_text(CLINIC, "9", "I have chest pain"))
        self.assertIn("108", s.handle_text(CLINIC, "9", "నాకు ఛాతి నొప్పి ఉంది"))
        self.assertEqual(GEM, [])
        self.assertNotIn("108", s.handle_text(CLINIC, "9", "what are the benefits") if GEM.append({"reply": "ok", "action": "none"}) is None else "")

    def test_gemini_failure_falls_back(self):
        GEM.extend([RuntimeError("x"), RuntimeError("y")])
        self.assertIn("staff will reply", s.handle_text(CLINIC, "9", "hi"))

    def test_model_fallback(self):
        GEM.extend([RuntimeError("quota"), {"reply": "Hello", "action": "none"}])
        self.assertEqual(s.handle_text(CLINIC, "9", "hi"), "Hello")

    def test_garbage_ai_booking_data(self):
        GEM.append({"reply": "x", "action": "book", "booking": {"name": "A", "date": "tomorrow", "time": "ten"}})
        self.assertIn("could not understand", s.handle_text(CLINIC, "9", "book"))

    def test_rate_limit(self):
        for _ in range(20):
            GEM.append({"reply": "ok", "action": "none"})
            self.assertIsNotNone(s.handle_text(CLINIC, "9", "hi"))
        self.assertIsNone(s.handle_text(CLINIC, "9", "hi"))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.Handler.clinic = CLINIC
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as x:
                return x.status, x.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_verify(self):
        self.assertEqual(self.req("/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=777"), (200, "777"))
        self.assertEqual(self.req("/webhook?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=777")[0], 403)

    def test_signature_and_end_to_end(self):
        SENT.clear(); GEM.clear(); GEM.append({"reply": "Fee is Rs. 300", "action": "none"})
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid.1", "from": "919999", "type": "text", "text": {"body": "fee entha?"}}]}}]}]}).encode()
        self.assertEqual(self.req("/webhook", body, {"X-Hub-Signature-256": "sha256=bad"})[0], 403)
        self.assertEqual(self.req("/webhook", body)[0], 403)
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        self.assertEqual(self.req("/webhook", body, {"X-Hub-Signature-256": sig})[0], 200)
        for _ in range(50):
            if SENT: break
            time.sleep(0.05)
        self.assertEqual(SENT, [("919999", "Fee is Rs. 300")])
        # duplicate delivery from Meta is ignored
        self.req("/webhook", body, {"X-Hub-Signature-256": sig}); time.sleep(0.3)
        self.assertEqual(len(SENT), 1)

    def test_admin_needs_token(self):
        self.assertEqual(self.req("/admin")[0], 403)
        self.assertEqual(self.req("/admin?token=adm")[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=1)
