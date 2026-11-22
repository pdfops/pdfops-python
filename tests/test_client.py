"""Offline tests for the pdfops client — a fake transport records every request.

Run:  python3 -m unittest discover -s tests -t .
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
from unittest import mock

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
        self.assertEqual(headers["X-Pdfops-Client-Version"], __version__)
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

    def test_password_forwarded_on_fill_inspect_merge(self):
        for call, path in (
            (lambda c: c.fill_form(PDF, {"a": "b"}, password="pw"), "/api/fill-form"),
            (lambda c: c.merge([PDF, PDF], password="pw"), "/api/merge"),
            (lambda c: c.inspect(PDF, password="pw"), "/api/inspect"),
        ):
            t = FakeTransport(body=b"{}" if path == "/api/inspect" else PDF)
            call(PdfOps(transport=t))
            _, url, headers, body = t.calls[0]
            self.assertTrue(url.endswith(path))
            parts = parts_of(body, headers["Content-Type"])
            self.assertEqual(parts[-1][0], "password")
            self.assertEqual(parts[-1][3], b"pw")

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
        # Isolated from a real PDFOPS_API_KEY sitting in the ambient
        # environment (e.g. this box's cron env), which would otherwise
        # make the client think a key was supplied.
        with mock.patch.dict(os.environ, {"PDFOPS_API_KEY": ""}):
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


def fields_of(t: "FakeTransport"):
    """Last call -> (url, [(name, filename, value-or-bytes)]) with text parts decoded."""
    _, url, headers, body = t.calls[-1]
    return url, [(n, fn, d) for n, fn, _c, d in parts_of(body, headers["Content-Type"])]


def text_fields(t: "FakeTransport"):
    url, parts = fields_of(t)
    assert parts[0] == ("pdf", "input.pdf", PDF)
    return url, [(n, d.decode()) for n, _fn, d in parts[1:]]


class PageOperations(unittest.TestCase):
    def test_extract_and_delete_pages(self):
        for method, path in (("extract_pages", "extract-pages"), ("delete_pages", "delete-pages")):
            t = FakeTransport(body=PDF)
            self.assertEqual(getattr(PdfOps(transport=t), method)(PDF, "1-3,5"), PDF)
            self.assertEqual(text_fields(t), (f"https://pdfops.dev/api/{path}", [("pages", "1-3,5")]))
            getattr(PdfOps(transport=t), method)(PDF, "2", password="pw")
            self.assertEqual(text_fields(t)[1], [("pages", "2"), ("password", "pw")])

    def test_rotate_sends_degrees_as_string_and_omits_pages(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).rotate(PDF, -90)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/rotate", [("degrees", "-90")]))
        PdfOps(transport=t).rotate(PDF, 180, pages="1,3", password="pw")
        self.assertEqual(text_fields(t)[1], [("degrees", "180"), ("pages", "1,3"), ("password", "pw")])

    def test_split_decodes_base64_parts(self):
        import base64
        api = {"count": 2, "parts": [
            {"pages": "1-2", "page_count": 2, "pdf": base64.b64encode(b"%PDF-A").decode()},
            {"pages": "3", "page_count": 1, "pdf": base64.b64encode(b"%PDF-B").decode()},
        ]}
        t = FakeTransport(body=json.dumps(api).encode())
        res = PdfOps(transport=t).split(PDF, ranges="1-2,3")
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/split", [("ranges", "1-2,3")]))
        self.assertEqual(res.count, 2)
        self.assertEqual([(p.pages, p.page_count, p.pdf) for p in res.parts], [("1-2", 2, b"%PDF-A"), ("3", 1, b"%PDF-B")])
        PdfOps(transport=t).split(PDF, every=5, password="pw")
        self.assertEqual(text_fields(t)[1], [("every", "5"), ("password", "pw")])

    def test_watermark_all_options_and_defaults_omitted(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).watermark(PDF, "DRAFT")
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/watermark", [("text", "DRAFT")]))
        PdfOps(transport=t).watermark(
            PDF, "DRAFT", opacity=0.25, size=36, color="#ff0000", rotation=30,
            position="top", pages="1-2", password="pw",
        )
        self.assertEqual(text_fields(t)[1], [
            ("text", "DRAFT"), ("opacity", "0.25"), ("size", "36"), ("color", "#ff0000"),
            ("rotation", "30"), ("position", "top"), ("pages", "1-2"), ("password", "pw"),
        ])

    def test_text_parts_are_utf8_text_plain(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).watermark(PDF, "Entw\u00fcrfe \u2014 \u00e9")
        _, _, headers, body = t.calls[-1]
        name, _fn, ctype, data = parts_of(body, headers["Content-Type"])[1]
        self.assertEqual((name, ctype, data), ("text", "text/plain", "Entw\u00fcrfe \u2014 \u00e9".encode("utf-8")))

    def test_page_numbers_options(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).page_numbers(PDF)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/page-numbers", []))
        PdfOps(transport=t).page_numbers(
            PDF, format="Page {n} of {total}", position="top-left", size=12, start=0, margin=20,
            pages="2-", password="pw",
        )
        self.assertEqual(text_fields(t)[1], [
            ("format", "Page {n} of {total}"), ("position", "top-left"), ("size", "12"),
            ("start", "0"), ("margin", "20"), ("pages", "2-"), ("password", "pw"),
        ])

    def test_unlock_note_header(self):
        t = FakeTransport(body=PDF, headers={"x-pdfops-note": "not-encrypted"})
        res = PdfOps(transport=t).unlock(PDF)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/unlock", []))
        self.assertEqual((res.pdf, res.note), (PDF, "not-encrypted"))
        res = PdfOps(transport=FakeTransport(body=PDF, headers={"X-Pdfops-Note": "not-encrypted"})).unlock(PDF)
        self.assertEqual(res.note, "not-encrypted")
        t = FakeTransport(body=PDF)
        res = PdfOps(transport=t).unlock(PDF, "pw")
        self.assertEqual((res.pdf, res.note), (PDF, None))
        self.assertEqual(text_fields(t)[1], [("password", "pw")])

    def test_protect_joins_permissions_and_empty_list_allows_none(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).protect(PDF, user_password="u", owner_password="o", permissions=["print", "copy"], password="pw")
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/protect", [
            ("user_password", "u"), ("owner_password", "o"), ("permissions", "print,copy"), ("password", "pw"),
        ]))
        PdfOps(transport=t).protect(PDF, user_password="u", permissions=[])
        self.assertEqual(text_fields(t)[1], [("user_password", "u"), ("permissions", "")])
        PdfOps(transport=t).protect(PDF, owner_password="o")
        self.assertEqual(text_fields(t)[1], [("owner_password", "o")])

    def test_metadata_returns_json_dict(self):
        api = {"page_count": 2, "pages": [{"width": 612, "height": 792, "rotation": 0}], "title": None,
               "encrypted": False, "has_form": False, "form_field_count": 0, "form_field_count_capped": False}
        t = FakeTransport(body=json.dumps(api).encode())
        self.assertEqual(PdfOps(transport=t).metadata(PDF), api)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/metadata", []))
        PdfOps(transport=t).metadata(PDF, password="pw")
        self.assertEqual(text_fields(t)[1], [("password", "pw")])

    def test_set_metadata_joins_keywords_and_keeps_empty_strings(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).set_metadata(
            PDF, title="T", author="A", subject="S", creator="C", keywords=["a", "b"], password="pw"
        )
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/set-metadata", [
            ("title", "T"), ("author", "A"), ("subject", "S"), ("creator", "C"),
            ("keywords", "a,b"), ("password", "pw"),
        ]))
        PdfOps(transport=t).set_metadata(PDF, title="")
        self.assertEqual(text_fields(t)[1], [("title", "")])

    def test_page_op_errors_surface_slug(self):
        t = FakeTransport(status=400, body=json.dumps({"error": "invalid_pages", "details": "page 9 out of range"}).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).extract_pages(PDF, "9")
        self.assertEqual(cm.exception.code, "invalid_pages")


class EdgeSuite(unittest.TestCase):
    def test_extract_text_returns_json_and_sends_options(self):
        api = {"pageCount": 1, "pages": [{"page": 1, "text": "hi", "tables": []}], "pagesWithoutText": [], "truncated": False}
        t = FakeTransport(body=json.dumps(api).encode())
        self.assertEqual(PdfOps(transport=t).extract_text(PDF), api)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/extract-text", []))
        PdfOps(transport=t).extract_text(PDF, pages="2-", tables_format="csv", password="pw")
        self.assertEqual(text_fields(t)[1], [("pages", "2-"), ("tables_format", "csv"), ("password", "pw")])

    def test_extract_text_plain_exposes_truncated_header(self):
        t = FakeTransport(body="a\fb".encode(), headers={"X-PDFops-Truncated": "true"})
        res = PdfOps(transport=t).extract_text_plain(PDF, pages="1-2")
        self.assertEqual((res.text, res.truncated), ("a\fb", True))
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/extract-text", [("format", "text"), ("pages", "1-2")]))
        res = PdfOps(transport=FakeTransport(body=b"x")).extract_text_plain(PDF)
        self.assertEqual((res.text, res.truncated), ("x", False))

    def test_extract_tables_xlsx_is_binary_and_exposes_headers(self):
        xlsx = b"PK\x03\x04\x00\xffbinary"
        hdrs = {"x-pdfops-truncated": "true", "X-PDFops-Truncated-By": "pages,sheets", "X-PDFops-Cells-Truncated": "true"}
        t = FakeTransport(body=xlsx, headers=hdrs)
        res = PdfOps(transport=t).extract_tables_xlsx(PDF, pages="1-3")
        self.assertEqual((res.xlsx, res.truncated, res.truncated_by, res.cells_truncated), (xlsx, True, ["pages", "sheets"], True))
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/extract-text", [("tables_format", "xlsx"), ("pages", "1-3")]))
        res = PdfOps(transport=FakeTransport(body=xlsx)).extract_tables_xlsx(PDF)
        self.assertEqual((res.truncated, res.truncated_by, res.cells_truncated), (False, [], False))

    def test_extract_tables_xlsx_no_tables_error_keeps_body(self):
        body = {"error": "no_tables", "details": "read stopped early", "truncated": True, "nextPage": 201}
        t = FakeTransport(status=400, body=json.dumps(body).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).extract_tables_xlsx(PDF)
        self.assertEqual((cm.exception.code, cm.exception.body["nextPage"]), ("no_tables", 201))

    def test_find_text_booleans_and_numbers(self):
        api = {"count": 0, "truncated": False, "hits": [], "pagesWithoutText": [], "pagesScanned": 1, "nextPage": None}
        t = FakeTransport(body=json.dumps(api).encode())
        self.assertEqual(PdfOps(transport=t).find_text(PDF, "Total"), api)
        self.assertEqual(text_fields(t), ("https://pdfops.dev/api/find-text", [("query", "Total")]))
        PdfOps(transport=t).find_text(PDF, r"\d+", regex=True, case_sensitive=False, whole_word=True, pages="1-2", max_hits=5, password="pw")
        self.assertEqual(text_fields(t)[1], [
            ("query", r"\d+"), ("regex", "true"), ("case_sensitive", "false"), ("whole_word", "true"),
            ("pages", "1-2"), ("max_hits", "5"), ("password", "pw"),
        ])

    def test_edit_sends_items_json_and_image_file_parts(self):
        items = [{"type": "text", "page": 1, "x": 10, "y": 20, "text": "Hi"}, {"type": "image", "page": 1, "x": 0, "y": 0, "image": "logo"}]
        png = b"\x89PNG\r\n\x1a\nfake"
        t = FakeTransport(body=PDF)
        self.assertEqual(PdfOps(transport=t).edit(PDF, items, images={"logo": png}, password="pw"), PDF)
        url, parts = fields_of(t)
        self.assertEqual(url, "https://pdfops.dev/api/edit")
        self.assertEqual([p[0] for p in parts], ["pdf", "items", "password", "logo"])
        self.assertEqual(json.loads(parts[1][2]), items)
        self.assertEqual(parts[3], ("logo", "logo", png))

    def test_add_fields_returns_pdf_and_warning(self):
        fields = [{"type": "text", "name": "Name", "page": 1, "x": 50, "y": 700, "width": 200, "height": 20}]
        t = FakeTransport(body=PDF)
        res = PdfOps(transport=t).add_fields(PDF, fields)
        self.assertEqual((res.pdf, res.warning), (PDF, None))
        url, parts = text_fields(t)
        self.assertEqual(url, "https://pdfops.dev/api/add-fields")
        self.assertEqual(json.loads(parts[0][1]), fields)
        t = FakeTransport(body=PDF, headers={"x-pdfops-warning": "xfa-layer-dropped; ..."})
        self.assertEqual(PdfOps(transport=t).add_fields(PDF, fields, password="pw").warning, "xfa-layer-dropped; ...")
        self.assertEqual(text_fields(t)[1][-1], ("password", "pw"))

    def test_attach_parts_options_and_keys_header(self):
        keys = [{"name": "data.xml", "key": "data.xml"}, {"name": "file-1", "key": "file-1"}]
        t = FakeTransport(body=PDF, headers={"X-Pdfops-Attachment-Keys": json.dumps(keys)})
        opts = [{"relationship": "Data"}]
        res = PdfOps(transport=t).attach(PDF, [("data.xml", b"<a/>"), b"raw"], options=opts, password="pw")
        self.assertEqual((res.pdf, res.attachment_keys), (PDF, keys))
        _, _, headers, body = t.calls[0]
        parts = parts_of(body, headers["Content-Type"])
        self.assertEqual([(n, fn) for n, fn, _c, _d in parts], [
            ("pdf", "input.pdf"), ("file", "data.xml"), ("file", "file-1"), ("options", None), ("password", None),
        ])
        self.assertEqual(parts[1][2:], ("application/xml", b"<a/>"))
        self.assertEqual(parts[2][2], "application/octet-stream")
        self.assertEqual(json.loads(parts[3][3]), opts)

    def test_attach_path_uses_basename_and_missing_header_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "note.txt")
            with open(path, "wb") as fh:
                fh.write(b"hello")
            t = FakeTransport(body=PDF)
            res = PdfOps(transport=t).attach(PDF, [path])
        self.assertEqual(res.attachment_keys, [])
        _, _, headers, body = t.calls[0]
        self.assertEqual(parts_of(body, headers["Content-Type"])[1][:2], ("file", "note.txt"))

    def test_html_to_pdf_json_body_and_headers(self):
        t = FakeTransport(body=PDF, headers={"x-pdfops-pages": "3", "x-pdfops-blocked-requests": "2"})
        res = PdfOps(transport=t).html_to_pdf("<h1>Hi</h1>", page_size="a4", landscape=False, margin={"top": 10, "left": 5}, scale=0.8)
        self.assertEqual((res.pdf, res.pages, res.blocked_requests), (PDF, 3, 2))
        method, url, headers, body = t.calls[0]
        self.assertEqual((method, url, headers["Content-Type"]), ("POST", "https://pdfops.dev/api/html-to-pdf", "application/json"))
        self.assertEqual(json.loads(body), {"html": "<h1>Hi</h1>", "page_size": "a4", "landscape": False, "margin": {"top": 10, "left": 5}, "scale": 0.8})
        res = PdfOps(transport=FakeTransport(body=PDF)).html_to_pdf("<p>x</p>")
        self.assertEqual((res.pages, res.blocked_requests), (None, None))

    def test_image_to_pdf_repeats_pdf_field(self):
        t = FakeTransport(body=PDF)
        self.assertEqual(PdfOps(transport=t).image_to_pdf([b"\xff\xd8a", b"\x89PNGb"], page_size="a4", margin=0), PDF)
        url, parts = fields_of(t)
        self.assertEqual(url, "https://pdfops.dev/api/image-to-pdf")
        self.assertEqual([(n, d) for n, _fn, d in parts], [("pdf", b"\xff\xd8a"), ("pdf", b"\x89PNGb"), ("page_size", b"a4"), ("margin", b"0")])

    def test_text_to_pdf_json_body_omits_unset(self):
        t = FakeTransport(body=PDF)
        self.assertEqual(PdfOps(transport=t).text_to_pdf("# Hi"), PDF)
        self.assertEqual(json.loads(t.calls[0][3]), {"content": "# Hi"})
        PdfOps(transport=t).text_to_pdf("a,b", format="csv", page_numbers=False, font_size=9, title="T")
        method, url, headers, body = t.calls[-1]
        self.assertEqual((method, url, headers["Content-Type"]), ("POST", "https://pdfops.dev/api/text-to-pdf", "application/json"))
        self.assertEqual(json.loads(body), {"content": "a,b", "format": "csv", "font_size": 9, "title": "T", "page_numbers": False})

    def test_invoice_factur_x_passes_through_and_error_body_has_missing(self):
        req = {"e_invoice": "factur-x", "from": {"name": "A", "country": "DE", "vat_id": "DE123456789"}, "to": {"name": "B", "country": "FR"},
               "items": [{"description": "x", "unit": "C62", "unit_price": 1}], "currency": "EUR", "invoice_number": "1", "date": "2026-11-01", "due": "2026-12-01", "tax_rate": 19}
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).invoice(req)
        self.assertEqual(json.loads(t.calls[0][3]), req)
        err = {"error": "einvoice_fields_required", "details": "also requires: from.country", "missing": ["from.country", "items[0].unit"]}
        t = FakeTransport(status=400, body=json.dumps(err).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).invoice({"e_invoice": "factur-x"})
        self.assertEqual((cm.exception.code, cm.exception.body["missing"]), ("einvoice_fields_required", ["from.country", "items[0].unit"]))
        for code in ("invalid_einvoice_field", "unrenderable_text"):
            t = FakeTransport(status=400, body=json.dumps({"error": code, "details": "d"}).encode())
            with self.assertRaises(PdfOpsError) as cm:
                PdfOps(transport=t).invoice(req)
            self.assertEqual(cm.exception.code, code)


class TrustBoundaries(unittest.TestCase):
    def test_attach_pair_str_data_is_refused_not_read_as_path(self):
        t = FakeTransport(body=PDF)
        with self.assertRaises(TypeError) as cm:
            PdfOps(transport=t).attach(PDF, [("a.xml", "/etc/hostname")])
        self.assertIn("pathlib.Path", str(cm.exception))
        self.assertEqual(t.calls, [])

    def test_attach_pair_accepts_bytearray_fileobj_pathlike_and_list(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.txt"
            p.write_bytes(b"from-disk")
            t = FakeTransport(body=PDF)
            PdfOps(transport=t).attach(PDF, [("a", bytearray(b"1")), ("b", io.BytesIO(b"2")), ("c", p), ["d", b"4"]])
        _, _, headers, body = t.calls[0]
        got = [(fn, data) for n, fn, _c, data in parts_of(body, headers["Content-Type"]) if n == "file"]
        self.assertEqual(got, [("a", b"1"), ("b", b"2"), ("c", b"from-disk"), ("d", b"4")])

    def test_multipart_rejects_header_injection_chars(self):
        for bad in ("a\r\nb", "a\nb", "a\0b"):
            with self.assertRaises(ValueError):
                PdfOps(transport=FakeTransport()).attach(PDF, [(bad, b"x")])
            with self.assertRaises(ValueError):
                PdfOps(transport=FakeTransport()).edit(PDF, [], images={bad: b"x"})

    def test_attach_quote_names_travel_via_options_and_backslash_is_refused(self):
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).attach(PDF, [('we"ird.xml', b"<a/>"), ("ok.xml", b"<b/>")], options=[{"relationship": "Data"}])
        _, _, headers, body = t.calls[0]
        parts = parts_of(body, headers["Content-Type"])
        self.assertEqual([(n, fn) for n, fn, _c, _d in parts if n == "file"], [("file", "file-0"), ("file", "ok.xml")])
        opts = json.loads([d for n, _f, _c, d in parts if n == "options"][0])
        self.assertEqual(opts, [{"relationship": "Data", "name": 'we"ird.xml'}])
        with self.assertRaises(ValueError):  # the API refuses backslashes in attachment names
            PdfOps(transport=FakeTransport()).attach(PDF, [("C:\\dir\\a.xml", b"<a/>")])

    def test_attach_accepts_a_path_as_the_name_and_none_options(self):
        import pathlib
        t = FakeTransport(body=PDF)
        PdfOps(transport=t).attach(PDF, [(pathlib.Path("dir/a.xml"), b"<a/>"), ("b.xml", b"<b/>")], options=[None, {"relationship": "Data"}])
        _, _, headers, body = t.calls[0]
        parts = parts_of(body, headers["Content-Type"])
        self.assertEqual([fn for n, fn, _c, _d in parts if n == "file"], ["a.xml", "b.xml"])
        self.assertEqual(json.loads([d for n, _f, _c, d in parts if n == "options"][0]), [{}, {"relationship": "Data"}])

    def test_edit_image_keys_validated(self):
        for bad in ("pdf", "password", "items", "", "a b", 'a"b', "x" * 65, "é"):
            t = FakeTransport(body=PDF)
            with self.assertRaises(ValueError):
                PdfOps(transport=t).edit(PDF, [], images={bad: b"x"})
            self.assertEqual(t.calls, [])
        PdfOps(transport=FakeTransport(body=PDF)).edit(PDF, [], images={"logo-1.v2_x": b"x"})

    def test_untrusted_headers_never_raise(self):
        for raw in ("not json", "{}", '"s"', "[1, null]", '[{"name": 1, "key": "k"}]', '[{"name": "n"}]'):
            t = FakeTransport(body=PDF, headers={"x-pdfops-attachment-keys": raw})
            self.assertEqual(PdfOps(transport=t).attach(PDF, [("a", b"x")]).attachment_keys, [])
        # one bad entry, or a count that differs from the files sent, drops them all (no shifted keys)
        t = FakeTransport(body=PDF, headers={"x-pdfops-attachment-keys": '[{"name": "n", "key": "k", "x": 1}, 5]'})
        self.assertEqual(PdfOps(transport=t).attach(PDF, [("a", b"x"), ("b", b"y")]).attachment_keys, [])
        t = FakeTransport(body=PDF, headers={"x-pdfops-attachment-keys": '[null, {"name": "b", "key": "k"}]'})
        self.assertEqual(PdfOps(transport=t).attach(PDF, [("a", b"x"), ("b", b"y")]).attachment_keys, [])
        t = FakeTransport(body=PDF, headers={"x-pdfops-attachment-keys": '[{"name": "n", "key": "k", "x": 1}]'})
        self.assertEqual(PdfOps(transport=t).attach(PDF, [("a", b"x")]).attachment_keys, [{"name": "n", "key": "k"}])
        for v in ("٣", "1.5", "-1", " 3", "", "9" * 40):
            t = FakeTransport(body=PDF, headers={"x-pdfops-pages": v, "x-pdfops-blocked-requests": v})
            res = PdfOps(transport=t).html_to_pdf("<p>x</p>")
            self.assertEqual((res.pages, res.blocked_requests), (None, None), v)

    def test_extract_text_refuses_xlsx(self):
        t = FakeTransport(body=b"PK")
        with self.assertRaises(ValueError) as cm:
            PdfOps(transport=t).extract_text(PDF, tables_format="xlsx")
        self.assertIn("extract_tables_xlsx", str(cm.exception))
        self.assertEqual(t.calls, [])

    def test_urllib_transport_does_not_follow_redirects(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen: List[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/api/inspect":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def do_GET(self):
                seen.append("GET " + self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            c = PdfOps("secret", base_url=f"http://127.0.0.1:{srv.server_port}")
            with self.assertRaises(PdfOpsError) as cm:
                c.inspect(PDF)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(cm.exception.status, 302)
        self.assertEqual(seen, ["/api/inspect"])


class Errors(unittest.TestCase):
    def test_non_json_error_has_no_body(self):
        t = FakeTransport(status=502, body=b"Bad Gateway")
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).inspect(PDF)
        self.assertIsNone(cm.exception.body)

    def test_structured_api_error(self):
        t = FakeTransport(status=400, body=json.dumps({"error": "unknown_field", "details": "No field named Nmae"}).encode())
        with self.assertRaises(PdfOpsError) as cm:
            PdfOps(transport=t).fill_form(PDF, {"Nmae": "x"})
        e = cm.exception
        self.assertEqual((e.status, e.code, e.details), (400, "unknown_field", "No field named Nmae"))
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
