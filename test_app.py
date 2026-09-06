"""Local Flask test client covering PRD §12 as much as possible without Compose."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

# Ensure env before importing app
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["MARKETING_URL"] = ""
os.environ["FROM_NAME"] = "Alex"
os.environ.pop("SMTP_HOST", None)
os.environ.pop("OWNER_NOTIFY_EMAIL", None)
os.environ.pop("FROM_EMAIL", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_BASE_URL", None)

SAMPLE_PDF = Path(__file__).resolve().parent / "sample-quote.pdf"
SAMPLE_HTML = Path(__file__).resolve().parent / "sample-quote.html"


class OpenPingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        upload_path = str(Path(self._tmpdir.name) / "uploads")
        os.makedirs(upload_path, exist_ok=True)
        os.environ["DATABASE_PATH"] = db_path
        os.environ["UPLOAD_DIR"] = upload_path
        os.environ["OWNER_PASSWORD"] = "testpass"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
        os.environ["BUSINESS_NAME"] = "Harbor HVAC"
        os.environ["MARKETING_URL"] = ""
        os.environ["FROM_NAME"] = "Alex"
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("OWNER_NOTIFY_EMAIL", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_BASE_URL", None)

        import importlib
        import app as app_module

        importlib.reload(app_module)
        self.app_module = app_module
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            app_module.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login(self) -> None:
        r = self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    def _create_quote(self, html: bool = False) -> tuple[int, str]:
        self._login()
        if html:
            data_file = (BytesIO(SAMPLE_HTML.read_bytes()), "sample-quote.html")
        else:
            data_file = (BytesIO(SAMPLE_PDF.read_bytes()), "sample-quote.pdf")
        r = self.client.post(
            "/quotes/new",
            data={
                "title": "AC Install",
                "prospect_name": "Priya Sharma",
                "prospect_email": "priya@example.com",
                "notes": "Prefers weekday mornings",
                "file": data_file,
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        loc = r.headers.get("Location", "")
        self.assertIn("/quotes/", loc)
        quote_id = int(loc.rstrip("/").split("/")[-1])
        with self.app.app_context():
            q = self.app_module.get_quote(quote_id)
            token = q["token"]
        return quote_id, token

    # 1. Health
    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["llm_configured"], False)

    # 2. Auth
    def test_auth_gate(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/login", r.headers.get("Location", ""))

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        # create then hit public view without login session cleared conceptually —
        # public routes work without owner session
        quote_id, token = self._create_quote()
        self.client.get("/logout")
        r = self.client.get(f"/v/{token}", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(r.status_code, 200)

    # 3. Create + first open
    def test_create_and_first_open(self) -> None:
        quote_id, token = self._create_quote()
        detail = self.client.get(f"/quotes/{quote_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(f"/v/{token}".encode(), detail.data)

        r = self.client.get(
            f"/v/{token}",
            headers={"User-Agent": "Mozilla/5.0 (Test)"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")

        with self.app.app_context():
            q = self.app_module.get_quote(quote_id)
            self.assertIn(q["status"], ("opened", "followup_queued"))
            self.assertGreaterEqual(q["open_count"], 1)
            self.assertIsNotNone(q["opened_at"])
            fu = self.app_module.get_followup(quote_id)
            self.assertIsNotNone(fu)
            n = (
                self.app_module.get_db()
                .execute("SELECT COUNT(*) AS c FROM followups WHERE quote_id = ?", (quote_id,))
                .fetchone()["c"]
            )
            self.assertEqual(n, 1)

    # 4. Second open still one followup
    def test_idempotent_followup(self) -> None:
        quote_id, token = self._create_quote()
        headers = {"User-Agent": "Mozilla/5.0"}
        self.assertEqual(self.client.get(f"/v/{token}", headers=headers).status_code, 200)
        self.assertEqual(self.client.get(f"/v/{token}", headers=headers).status_code, 200)
        with self.app.app_context():
            n = (
                self.app_module.get_db()
                .execute("SELECT COUNT(*) AS c FROM followups WHERE quote_id = ?", (quote_id,))
                .fetchone()["c"]
            )
            self.assertEqual(n, 1)
            q = self.app_module.get_quote(quote_id)
            self.assertGreaterEqual(q["open_count"], 2)

    # 5. Draft content + mark sent; Approve hidden without SMTP
    def test_draft_and_mark_sent(self) -> None:
        quote_id, token = self._create_quote()
        self.client.get(f"/v/{token}", headers={"User-Agent": "Mozilla/5.0"})
        with self.app.app_context():
            fu = self.app_module.get_followup(quote_id)
            self.assertIsNotNone(fu)
            self.assertTrue(
                "Priya" in fu["subject"] or "AC Install" in fu["subject"]
            )
            self.assertIn("opened", fu["body"].lower())

        detail = self.client.get(f"/quotes/{quote_id}")
        self.assertEqual(detail.status_code, 200)
        # Approve & send disabled without SMTP (button disabled or absent as active submit)
        self.assertIn(b"Approve", detail.data)
        self.assertTrue(
            b'disabled' in detail.data.lower() or b"SMTP required" in detail.data
            or b'title="SMTP required"' in detail.data
        )

        r = self.client.post(
            f"/quotes/{quote_id}/followup",
            data={"action": "mark_sent", "subject": fu_subject(self, quote_id), "body": "x"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        with self.app.app_context():
            fu = self.app_module.get_followup(quote_id)
            q = self.app_module.get_quote(quote_id)
            self.assertEqual(fu["status"], "sent")
            self.assertEqual(q["status"], "followup_sent")

        # Approving again is no-op when already sent
        r = self.client.post(
            f"/quotes/{quote_id}/followup",
            data={"action": "approve_send", "subject": "x", "body": "y"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)

    # 6. No sequences table; UNIQUE one followup
    def test_no_sequences_unique_followup(self) -> None:
        quote_id, token = self._create_quote()
        self.client.get(f"/v/{token}", headers={"User-Agent": "Mozilla/5.0"})
        with self.app.app_context():
            db = self.app_module.get_db()
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            self.assertNotIn("sequences", tables)
            self.assertIn("quotes", tables)
            self.assertIn("opens", tables)
            self.assertIn("followups", tables)
            # UNIQUE constraint
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """
                    INSERT INTO followups (quote_id, subject, body, status, created_at)
                    VALUES (?, 's', 'b', 'queued', '2026-01-01T00:00:00Z')
                    """,
                    (quote_id,),
                )

    # 7. HTML upload path
    def test_html_upload(self) -> None:
        quote_id, token = self._create_quote(html=True)
        r = self.client.get(f"/v/{token}", headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"AC Install Quote", r.data)
        with self.app.app_context():
            fu = self.app_module.get_followup(quote_id)
            self.assertIsNotNone(fu)
            n = (
                self.app_module.get_db()
                .execute("SELECT COUNT(*) AS c FROM followups WHERE quote_id = ?", (quote_id,))
                .fetchone()["c"]
            )
            self.assertEqual(n, 1)

    # 8. Empty MARKETING_URL → no Powered by
    def test_no_powered_by_footer(self) -> None:
        self._login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Powered by OpenPing", r.data)

    # 9. Works without LLM
    def test_works_without_llm(self) -> None:
        self.assertFalse(self.app_module.llm_configured())
        quote_id, token = self._create_quote()
        self.client.get(f"/v/{token}", headers={"User-Agent": "Mozilla/5.0"})
        with self.app.app_context():
            fu = self.app_module.get_followup(quote_id)
            self.assertIsNotNone(fu)
            self.assertIn("Harbor HVAC", fu["body"])

    # Soft bot skip
    def test_bot_ua_skips_first_open(self) -> None:
        quote_id, token = self._create_quote()
        r = self.client.get(f"/v/{token}", headers={"User-Agent": "Googlebot/2.1"})
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            q = self.app_module.get_quote(quote_id)
            self.assertEqual(q["status"], "link_ready")
            self.assertEqual(q["open_count"], 0)
            self.assertIsNone(self.app_module.get_followup(quote_id))

    def test_tracking_pixel(self) -> None:
        quote_id, token = self._create_quote()
        r = self.client.get(
            f"/t/{token}.gif",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/gif")
        with self.app.app_context():
            q = self.app_module.get_quote(quote_id)
            self.assertIn(q["status"], ("opened", "followup_queued"))
            self.assertIsNotNone(self.app_module.get_followup(quote_id))

    def test_unknown_token_404(self) -> None:
        r = self.client.get("/v/" + ("ab" * 32), headers={"User-Agent": "Mozilla/5.0"})
        self.assertEqual(r.status_code, 404)


def fu_subject(test: OpenPingTests, quote_id: int) -> str:
    with test.app.app_context():
        return test.app_module.get_followup(quote_id)["subject"]


if __name__ == "__main__":
    unittest.main()
