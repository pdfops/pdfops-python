"""Offline tests for the pdfops client — a fake transport records every request.

Run:  python3 -m unittest discover -s python/tests -t python
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pdfops import PdfOps, PdfOpsError, __version__  # noqa: E402

PDF = b"%PDF-1.4\n%fake\n"


class FakeTransport:
    def __init__(self, status: int = 200, body: bytes = b"", headers: Optional[dict] = None) -> None:
        self.status, self.body, self.headers = status, body, headers or {}
        self.calls: List[Tuple[str, str, dict, Optional[bytes]]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.status, self.headers, self.body


def parts_of(body: bytes, content_type: str):
    """Decode a multipart body into [(name, filename, ctype, data)]."""
    boundary = re.search(r"boundary=(\S+)", content_type).group(1).encode()
    out = []
    for chunk in body.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, data = chunk.lstrip(b"\r\n").split(b"\r\n\r\n", 1)
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        fn = re.search(rb'filename="([^"]+)"', head)
        ctype = re.search(rb"Content-Type: (\S+)", head).group(1).decode()
        out.append((name, fn.group(1).decode() if fn else None, ctype, data[:-2]))
    return out


class HeadersAndInputs(unittest.TestCase):
    def test_key_and_client_tag_headers(self):
        t = FakeTransport(body=PDF)
        PdfOps("pdfops_live_x", transport=t).fill_form(PDF, {"a": "b"})
        _, url, headers, _ = t.calls[0]
        self.assertEqual(url, "https://pdfops.dev/api/fill-form")
        self.assertEqual(headers["X-API-Key"], "pdfops_live_x")
        self.assertEqual(headers["X-Pdfops-Client"], "py")
        self.assertEqual(headers["User-Agent"], f"pdfops-python/{__version__}")

    def test_keyless_and_custom_tag_and_base_url(self):
        t = FakeTransport(body=b"{}")
        PdfOps(base_url="http://localhost:3000/", client_tag="airflow", transport=t).inspect(PDF)
        _, url, headers, _ = t.calls[0]
        self.assertEqual(url, "http://localhost:3000/api/inspect")
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(headers["X-Pdfops-Client"], "airflow")

    def test_env_key_is_picked_up(self):
        os.environ["PDFOPS_API_KEY"] = "pdfops_live_env"
        try:
            t = FakeTransport(body=b"{}")
            PdfOps(transport=t).inspect(PDF)
            self.assertEqual(t.calls[0][2]["X-API-Key"], "pdfops_live_env")
        finally:
            del os.environ["PDFOPS_API_KEY"]

    def test_pdf_input_forms(self):
        t = FakeTransport(body=b"{}")
        c = PdfOps(transport=t)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(PDF)
            path = fh.name
        try:
            c.inspect(PDF)                    # bytes
            c.inspect(bytearray(PDF))         # bytearray
            c.inspect(path)                   # str path
            from pathlib import Path
            c.inspect(Path(path))             # PathLike
            c.inspect(io.BytesIO(PDF))        # binary file object
        finally:
            os.unlink(path)
        for _, _, headers, body in t.calls:
            (name, fn, ctype, data), = parts_of(body, headers["Content-Type"])
            self.assertEqual((name, fn, ctype, data), ("pdf", "input.pdf", "application/pdf", PDF))

    def test_text_mode_file_rejected(self):
        with self.assertRaises(TypeError):
            PdfOps(transport=FakeTransport()).inspect(io.StringIO("%PDF"))


class Endpoints(unittest.TestCase):
    def test_fill_form_parts_and_flatten(self):
        t = FakeTransport(body=PDF)
        out = PdfOps(transport=t).fill_form(PDF, {"Name": "Ada", "Agree": "true"}, flatten=True)
        self.assertEqual(out, PDF)
        _, _, headers, body = t.calls[0]
        parts = parts_of(body, headers["Content-Type"])
        self.assertEqual([p[0] for p in parts], ["pdf", "fields", "flatten"])
        self.assertEqual(json.loads(parts[1][3]), {"Name": "Ada", "Agree": "true"})
        self.assertEqual(parts[2][3], b"true")

    def test_fill_form_without_flatten_has_no_flatten_part(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).fill_form(PDF, {"a": "b"})
        _, _, headers, body = t.calls[0]
        self.assertEqual([p[0] for p in parts_of(body, headers["Content-Type"])], ["pdf", "fields"])

    def test_merge_order_and_minimum(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).merge([PDF, b"%PDF-2", b"%PDF-3"])
        _, url, headers, body = t.calls[0]
        self.assertTrue(url.endswith("/api/merge"))
        self.assertEqual([p[1] for p in parts_of(body, headers["Content-Type"])], ["part-0.pdf", "part-1.pdf", "part-2.pdf"])
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).merge([PDF])
        self.assertEqual(cm.exception.code, "too_few_pdfs")
        self.assertEqual(len(t.calls), 1, "no request is sent for an invalid merge")

    def test_invoice_is_json(self):
        t = FakeTransport(body=PDF)
        req = {"from": "PDFops", "to": "Ada", "items": [{"description": "x", "unit_price": 1}], "invoice_number": "1"}
        self.assertEqual(PdfOps(transport=t).invoice(req), PDF)
        method, url, headers, body = t.calls[0]
        self.assertEqual((method, url), ("POST", "https://pdfops.dev/api/invoice"))
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), req)

    def test_usage_requires_key_and_parses_json(self):
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=FakeTransport()).usage()
        self.assertEqual(cm.exception.code, "missing_api_key")
        t = FakeTransport(body=json.dumps({"tier": "free", "limit": 250, "used": 3, "remaining": 247}).encode())
        u = PdfOps("k", transport=t).usage()
        self.assertEqual((t.calls[0][0], u["remaining"]), ("GET", 247))

    def test_signup_sends_email_without_key_headers(self):
        t = FakeTransport(status=204)
        PdfOps("k", transport=t).signup("dev@example.com")
        method, url, headers, body = t.calls[0]
        self.assertEqual((method, url), ("POST", "https://pdfops.dev/api/signup"))
        self.assertNotIn("X-API-Key", headers)
        self.assertEqual(json.loads(body), {"email": "dev@example.com"})


class Errors(unittest.TestCase):
    def test_structured_api_error(self):
        t = FakeTransport(status=422, body=json.dumps({"error": "unknown_field", "details": "No field named Nmae"}).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).fill_form(PDF, {"Nmae": "x"})
        e = cm.exception
        self.assertEqual((e.status, e.code, e.details), (422, "unknown_field", "No field named Nmae"))
        self.assertEqual(str(e), "unknown_field: No field named Nmae")

    def test_non_json_error_falls_back_to_status(self):
        t = FakeTransport(status=502, body=b"Bad Gateway")
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).inspect(PDF)
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.details), (502, "http_502", "Bad Gateway"))

    def test_rate_limit_error_code_surfaces(self):
        t = FakeTransport(status=429, body=json.dumps({"error": "rate_limited", "details": "100/IP/mo exceeded"}).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).merge([PDF, PDF])
        self.assertEqual(cm.exception.code, "rate_limited")


if __name__ == "__main__":
    unittest.main()
