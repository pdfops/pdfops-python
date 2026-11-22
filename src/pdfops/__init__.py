"""pdfops — Python client for the PDFops API (https://pdfops.dev).

Zero dependencies: standard-library ``urllib`` only, so it runs anywhere
Python does — Lambda, Cloud Functions, Airflow tasks, a cron, a notebook.
Errors carry the API's structured ``{"error", "details"}`` body via
:class:`PdfOpsError`.

    from pdfops import PdfOps

    client = PdfOps(api_key="pdfops_live_...")      # or PdfOps() for the keyless trial
    fields = client.inspect("blank.pdf")["fillTemplate"]
    fields["Name"] = "Ada Lovelace"
    open("filled.pdf", "wb").write(client.fill_form("blank.pdf", fields, flatten=True))
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib import error as urlerror
from urllib import request as urlrequest

__all__ = [
    "PdfOps", "PdfOpsError", "PdfInput", "SplitPart", "SplitResult", "UnlockResult", "AddFieldsResult",
    "AttachResult", "ExtractTextPlainResult", "ExtractTablesXlsxResult", "HtmlToPdfResult", "__version__",
]
__version__ = "0.4.0"

#: A PDF given as raw bytes, a filesystem path, or a binary file object.
PdfInput = Union[bytes, bytearray, memoryview, str, "os.PathLike[str]", IO[bytes]]

#: ``transport(method, url, headers, body) -> (status, headers, body)``.
#: Injected in tests; the default speaks HTTP through urllib.
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, Mapping[str, str], bytes]]

DEFAULT_BASE_URL = "https://pdfops.dev"
USER_AGENT = f"pdfops-python/{__version__}"


@dataclass(frozen=True)
class SplitPart:
    """One output of :meth:`PdfOps.split`."""

    #: The 1-based page spec this part covers, e.g. ``"3-5"``.
    pages: str
    page_count: int
    #: The part's PDF bytes (decoded from the API's base64).
    pdf: bytes


@dataclass(frozen=True)
class SplitResult:
    count: int
    parts: List[SplitPart]


@dataclass(frozen=True)
class UnlockResult:
    pdf: bytes
    #: The API's ``x-pdfops-note`` header: ``"not-encrypted"`` when the input had
    #: no encryption (it is returned re-saved). ``None`` otherwise.
    note: Optional[str] = None


@dataclass(frozen=True)
class AddFieldsResult:
    pdf: bytes
    #: The API's ``x-pdfops-warning`` header: set when the input had an XFA layer
    #: that the save dropped. ``None`` otherwise.
    warning: Optional[str] = None


@dataclass(frozen=True)
class AttachResult:
    pdf: bytes
    #: The API's ``x-pdfops-attachment-keys`` header, parsed: one
    #: ``{"name", "key"}`` per added file, in request order (names cut to 40 characters).
    attachment_keys: List[Dict[str, str]]


@dataclass(frozen=True)
class ExtractTextPlainResult:
    #: Pages joined by a form feed.
    text: str
    #: The API's ``X-PDFops-Truncated`` header: the read stopped at a cap.
    truncated: bool = False


@dataclass(frozen=True)
class ExtractTablesXlsxResult:
    #: The .xlsx workbook bytes, one sheet per detected table.
    xlsx: bytes
    #: ``X-PDFops-Truncated``: a cap stopped the read or cut the workbook.
    truncated: bool = False
    #: ``X-PDFops-Truncated-By``: which caps (``pages``, ``output``, ``sheets``, ``columns``, ``rows``).
    truncated_by: List[str] = field(default_factory=list)
    #: ``X-PDFops-Cells-Truncated``: a cell over Excel's 32,767 characters was cut.
    cells_truncated: bool = False


@dataclass(frozen=True)
class HtmlToPdfResult:
    pdf: bytes
    #: The API's ``x-pdfops-pages`` header: pages in the output, when sent.
    pages: Optional[int] = None
    #: The API's ``x-pdfops-blocked-requests`` header: resource requests the renderer refused, when sent.
    blocked_requests: Optional[int] = None


def _digits(v: Optional[str]) -> Optional[int]:
    return int(v) if v is not None and v.isascii() and v.isdigit() and len(v) <= 9 else None


# Fixed types for the common attachment extensions: ``mimetypes`` reads the machine's
# MIME database, so the same call sent ``application/xml`` on one box and ``text/xml`` on another.
_ATTACHMENT_MIME = {
    ".xml": "application/xml", ".json": "application/json", ".csv": "text/csv", ".txt": "text/plain",
    ".md": "text/markdown", ".html": "text/html", ".pdf": "application/pdf", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _attachment_mime(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _ATTACHMENT_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    return next((v for k, v in headers.items() if k.lower() == name), None)


class PdfOpsError(Exception):
    """Structured API error. ``code`` is the API's stable ``error`` slug. ``body`` is
    the parsed JSON error body when there was one, for fields beyond ``error`` and
    ``details`` (e.g. ``missing`` on ``einvoice_fields_required``, ``nextPage`` on a
    truncated ``no_tables``); otherwise ``None``."""

    def __init__(self, status: int, code: str, details: str, body: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(f"{code}: {details}")
        self.status = status
        self.code = code
        self.details = details
        self.body = body


def _read_pdf(pdf: PdfInput) -> bytes:
    if isinstance(pdf, (bytes, bytearray, memoryview)):
        return bytes(pdf)
    if isinstance(pdf, (str, PurePath)) or hasattr(pdf, "__fspath__"):
        with open(os.fspath(pdf), "rb") as fh:  # type: ignore[arg-type]
            return fh.read()
    if hasattr(pdf, "read"):
        data = pdf.read()
        if isinstance(data, str):
            raise TypeError("PDF file objects must be opened in binary mode ('rb')")
        return bytes(data)
    raise TypeError(f"unsupported PDF input type: {type(pdf).__name__}")


def _multipart(parts: Sequence[Tuple[str, Optional[str], bytes, str]]) -> Tuple[str, bytes]:
    """Encode ``(name, filename, data, content_type)`` parts; returns (content-type, body)."""
    boundary = f"----pdfops-{uuid.uuid4().hex}"
    out = bytearray()
    for name, filename, data, ctype in parts:
        # A name or filename is written into a quoted header value, so none of these may appear:
        # CR, LF, NUL end the header; a quote or backslash would escape the quoting.
        for label, value in (("name", name), ("filename", filename)):
            if value is not None and any(ch in value for ch in '\r\n\0"\\'):
                raise ValueError(f"multipart {label} may not contain CR, LF, NUL, a double quote or a backslash: {value!r}")
        out += f"--{boundary}\r\n".encode()
        disp = f'form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out += f"Content-Disposition: {disp}\r\n".encode()
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None  # the 3xx is then raised as HTTPError and surfaces as PdfOpsError


def _urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]) -> Tuple[int, Mapping[str, str], bytes]:
        req = urlrequest.Request(url, data=body, method=method, headers=dict(headers))
        # No redirects: a 3xx would forward X-API-Key and the body to another origin.
        # Built per call, so proxy settings made after import are honoured as urlopen honours them.
        opener = urlrequest.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=timeout) as res:
                return res.status, dict(res.headers.items()), res.read()
        except urlerror.HTTPError as e:
            return e.code, dict(e.headers.items()), e.read()

    return send


class PdfOps:
    """Client for the PDFops API.

    :param api_key: key from https://pdfops.dev/pricing (free tier: 250 req/mo).
        Omit for keyless trial calls (100 req/IP/mo).
    :param base_url: API origin override (testing).
    :param client_tag: integration identifier sent as ``X-Pdfops-Client`` for
        anonymous usage attribution. Defaults to ``"py"``; set it when embedding
        this client inside another tool (e.g. ``"airflow"``).
    :param timeout: per-request timeout in seconds.
    :param transport: custom transport (tests / instrumentation).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client_tag: Optional[str] = "py",
        timeout: float = 60.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("PDFOPS_API_KEY") or None
        self.base_url = base_url.rstrip("/")
        self.client_tag = client_tag
        self._send = transport or _urllib_transport(timeout)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "application/pdf, application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.client_tag:
            h["X-Pdfops-Client"] = self.client_tag
            # Same rule as the TS SDK: the package version describes the "py"
            # tag only; an embedding tool's tag has its own version.
            if self.client_tag == "py":
                h["X-Pdfops-Client-Version"] = __version__
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, headers: Mapping[str, str], body: Optional[bytes]) -> bytes:
        return self._request_full(method, path, headers, body)[1]

    def _request_full(
        self, method: str, path: str, headers: Mapping[str, str], body: Optional[bytes]
    ) -> Tuple[Mapping[str, str], bytes]:
        status, _res_headers, data = self._send(method, f"{self.base_url}{path}", headers, body)
        if 200 <= status < 300:
            return _res_headers, data
        code, details = f"http_{status}", (data.decode("utf-8", "replace") or f"HTTP {status}")
        err_body: Optional[Dict[str, Any]] = None
        try:
            parsed = json.loads(data.decode("utf-8"))
            if isinstance(parsed, dict):
                err_body = parsed
                code = str(parsed.get("error") or code)
                details = str(parsed.get("details") or details)
        except ValueError:
            pass  # non-JSON error body — keep the status fallback
        raise PdfOpsError(status, code, details, err_body)

    def _post_pdf(self, path: str, parts: Sequence[Tuple[str, Optional[str], bytes, str]]) -> bytes:
        ctype, body = _multipart(parts)
        return self._request("POST", path, self._headers({"Content-Type": ctype}), body)

    def _post_fields(
        self, path: str, pdf: PdfInput, fields: Mapping[str, Any]
    ) -> Tuple[Mapping[str, str], bytes]:
        """POST a PDF plus text fields; fields whose value is ``None`` are omitted
        (a missing field means "default"; an empty string does not)."""
        parts: List[Tuple[str, Optional[str], bytes, str]] = [("pdf", "input.pdf", _read_pdf(pdf), "application/pdf")]
        for name, value in fields.items():
            if value is not None:
                parts.append((name, None, str(value).encode("utf-8"), "text/plain"))
        ctype, body = _multipart(parts)
        return self._request_full("POST", path, self._headers({"Content-Type": ctype}), body)

    # ── API ─────────────────────────────────────────────────────────────────

    def inspect(self, pdf: PdfInput, *, password: Optional[str] = None) -> Dict[str, Any]:
        """List a PDF's AcroForm fields plus a paste-ready ``fillTemplate``.

        Returns ``{"count", "hasXFA", "fields": [...], "fillTemplate": {...}}``.
        A PDF with no form returns ``count == 0`` — not an error. ``password``
        opens a user-password PDF.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [("pdf", "input.pdf", _read_pdf(pdf), "application/pdf")]
        if password is not None:
            parts.append(("password", None, password.encode("utf-8"), "text/plain"))
        data = self._post_pdf("/api/inspect", parts)
        return json.loads(data.decode("utf-8"))

    def fill_form(
        self, pdf: PdfInput, fields: Mapping[str, str], *, flatten: bool = False, password: Optional[str] = None
    ) -> bytes:
        """Fill AcroForm fields; returns the filled PDF bytes.

        Field names must exist in the PDF — use :meth:`inspect` to discover
        them. Checkbox values are the strings ``"true"``/``"false"``; choice
        fields take one of their options. ``flatten=True`` bakes values into
        the page content and drops the form so fields are no longer editable.
        ``password`` opens a user-password PDF.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [
            ("pdf", "input.pdf", _read_pdf(pdf), "application/pdf"),
            ("fields", None, json.dumps(dict(fields)).encode("utf-8"), "application/json"),
        ]
        if flatten:
            parts.append(("flatten", None, b"true", "text/plain"))
        if password is not None:
            parts.append(("password", None, password.encode("utf-8"), "text/plain"))
        return self._post_pdf("/api/fill-form", parts)

    def merge(self, pdfs: Sequence[PdfInput], *, password: Optional[str] = None) -> bytes:
        """Merge two or more PDFs, in order; returns the combined PDF bytes.

        ``password`` opens user-password inputs and applies to every input.
        """
        if len(pdfs) < 2:
            raise PdfOpsError(400, "too_few_pdfs", "merge needs at least 2 PDFs")
        parts: List[Tuple[str, Optional[str], bytes, str]] = [("pdf", f"part-{i}.pdf", _read_pdf(p), "application/pdf") for i, p in enumerate(pdfs)]
        if password is not None:
            parts.append(("password", None, password.encode("utf-8"), "text/plain"))
        return self._post_pdf("/api/merge", parts)

    def invoice(self, request: Mapping[str, Any]) -> bytes:
        """Generate a complete invoice PDF from structured data.

        Deterministic: the same request produces byte-identical output, so it
        is safe to re-render from webhooks and retries. Keys: ``from``, ``to``
        (string or ``{"name", "lines"}``), ``items`` (``description``,
        ``unit_price``, optional ``quantity``), optional ``invoice_number``,
        ``date``, ``due``, ``currency``, ``tax_rate``, ``notes``.

        Set ``"e_invoice": "factur-x"`` for a Factur-X / ZUGFeRD e-invoice (PDF/A-3
        with the data embedded as ``factur-x.xml``, profile EN 16931). That mode
        also needs ``invoice_number``, ``date``, ``currency``, ``due`` (a
        YYYY-MM-DD date) or ``payment_terms``, ``from`` and ``to`` as objects
        (``name``, ``country`` as an ISO 3166-1 alpha-2 code, optional ``street``,
        ``postcode``, ``city``, ``vat_id``, ``tax_id``, ``registration_id``), a
        seller identification (``from.vat_id``, or ``from.tax_id`` with
        ``from.registration_id``), a ``unit`` code (UN/ECE Recommendation 20, e.g.
        ``"C62"``) on every item, and ``tax_category`` (``S|Z|E|AE|O``) when
        ``tax_rate`` is 0 or omitted. Optional: ``tax_exemption_reason`` (needed
        for ``E``, ``AE``, ``O``), ``iban``. Unknown fields are refused.

        Errors with ``e_invoice``: ``einvoice_fields_required`` (the full error
        body is ``PdfOpsError.body``; ``body["missing"]`` lists every missing
        field), ``invalid_einvoice_field``,
        ``unrenderable_text`` (characters outside Latin, Latin Extended-A, euro
        sign and common punctuation).
        """
        body = json.dumps(dict(request)).encode("utf-8")
        return self._request("POST", "/api/invoice", self._headers({"Content-Type": "application/json"}), body)

    def extract_pages(self, pdf: PdfInput, pages: str, *, password: Optional[str] = None) -> bytes:
        """Keep only the listed pages, in the order given (so it also reorders and
        duplicates). ``pages`` is a 1-based spec such as ``"1-3,5,8-"``.

        Errors: ``invalid_pages`` (a page out of range), ``encrypted_pdf``.
        """
        return self._post_fields("/api/extract-pages", pdf, {"pages": pages, "password": password})[1]

    def delete_pages(self, pdf: PdfInput, pages: str, *, password: Optional[str] = None) -> bytes:
        """Remove the listed pages and keep the rest in order. ``pages`` is a
        1-based spec such as ``"2,4-6,9-"``.

        Errors: ``invalid_pages``, ``no_pages_left`` (every page deleted).
        """
        return self._post_fields("/api/delete-pages", pdf, {"pages": pages, "password": password})[1]

    def rotate(
        self, pdf: PdfInput, degrees: int, *, pages: Optional[str] = None, password: Optional[str] = None
    ) -> bytes:
        """Rotate pages clockwise by 90, 180, 270 or -90 degrees, added to each
        page's existing rotation. Omit ``pages`` to rotate every page.

        Errors: ``invalid_degrees``, ``invalid_pages``.
        """
        return self._post_fields("/api/rotate", pdf, {"degrees": degrees, "pages": pages, "password": password})[1]

    def split(
        self,
        pdf: PdfInput,
        *,
        ranges: Optional[str] = None,
        every: Optional[int] = None,
        password: Optional[str] = None,
    ) -> SplitResult:
        """Split one PDF into several, by explicit ``ranges`` (``"1-2,3-5"`` gives
        two parts) or fixed-size chunks (``every``). Give exactly one of the two.

        Errors: ``invalid_split``, ``invalid_every``, ``invalid_pages``,
        ``too_many_parts``, ``output_too_large``.
        """
        _, data = self._post_fields("/api/split", pdf, {"ranges": ranges, "every": every, "password": password})
        body = json.loads(data.decode("utf-8"))
        parts = [
            SplitPart(pages=p["pages"], page_count=p["page_count"], pdf=base64.b64decode(p["pdf"]))
            for p in body["parts"]
        ]
        return SplitResult(count=body["count"], parts=parts)

    def watermark(
        self,
        pdf: PdfInput,
        text: str,
        *,
        opacity: Optional[float] = None,
        size: Optional[float] = None,
        color: Optional[str] = None,
        rotation: Optional[float] = None,
        position: Optional[str] = None,
        pages: Optional[str] = None,
        password: Optional[str] = None,
    ) -> bytes:
        """Stamp a diagonal text watermark (default: centered, 45 degrees, grey
        ``#808080``, 15% opacity, 48 pt). Standard Helvetica only. ``position`` is
        ``"center"``, ``"top"`` or ``"bottom"``.

        Errors: ``missing_text``, ``text_too_long``, ``unsupported_characters``,
        ``invalid_parameter``, ``invalid_pages``.
        """
        fields = {
            "text": text, "opacity": opacity, "size": size, "color": color,
            "rotation": rotation, "position": position, "pages": pages, "password": password,
        }
        return self._post_fields("/api/watermark", pdf, fields)[1]

    def page_numbers(
        self,
        pdf: PdfInput,
        *,
        format: Optional[str] = None,
        position: Optional[str] = None,
        size: Optional[float] = None,
        start: Optional[int] = None,
        margin: Optional[float] = None,
        pages: Optional[str] = None,
        password: Optional[str] = None,
    ) -> bytes:
        """Print page numbers from a template with ``{n}`` and ``{total}`` (default
        ``"{n} / {total}"``, bottom-center, 10 pt). Page i shows ``start + i - 1``.
        ``position`` is one of ``bottom-center|bottom-right|bottom-left|top-center|top-right|top-left``.

        Errors: ``unsupported_characters``, ``invalid_pages``.
        """
        fields = {
            "format": format, "position": position, "size": size, "start": start,
            "margin": margin, "pages": pages, "password": password,
        }
        return self._post_fields("/api/page-numbers", pdf, fields)[1]

    def unlock(self, pdf: PdfInput, password: Optional[str] = None) -> UnlockResult:
        """Save a PDF without encryption so print/copy/modify restrictions are gone.

        Owner-locked files with an empty user password need no ``password``; this
        does not crack unknown passwords (``encrypted_pdf``). ``note`` is
        ``"not-encrypted"`` when the input had no encryption and was only re-saved.
        """
        headers, data = self._post_fields("/api/unlock", pdf, {"password": password})
        note = next((v for k, v in headers.items() if k.lower() == "x-pdfops-note"), None)
        return UnlockResult(pdf=data, note=note or None)

    def protect(
        self,
        pdf: PdfInput,
        *,
        user_password: Optional[str] = None,
        owner_password: Optional[str] = None,
        permissions: Optional[Sequence[str]] = None,
        password: Optional[str] = None,
    ) -> bytes:
        """Encrypt a PDF. Give at least one of ``user_password`` (needed to open the
        result) and ``owner_password`` (lifts restrictions), each 1-32 Latin-1 chars.
        ``permissions`` lists what stays ALLOWED (``print``, ``copy``, ``modify``,
        ``annotate``, ``fill-forms``); omit for the default, ``[]`` allows none.
        ``password`` opens an encrypted input.

        Errors: ``missing_password``, ``invalid_password``, ``invalid_permissions``.
        """
        fields = {
            "user_password": user_password,
            "owner_password": owner_password,
            "permissions": None if permissions is None else ",".join(permissions),
            "password": password,
        }
        return self._post_fields("/api/protect", pdf, fields)[1]

    def metadata(self, pdf: PdfInput, *, password: Optional[str] = None) -> Dict[str, Any]:
        """Read a PDF's page count, per-page size and rotation, document info (title,
        author, dates, ...), encryption flag and form field count.

        Returns ``{"page_count", "pages": [{"width", "height", "rotation"}], "title",
        "author", "subject", "keywords", "creator", "producer", "creation_date",
        "modification_date", "encrypted", "has_form", "form_field_count",
        "form_field_count_capped"}``; absent text/date values are ``None``.
        """
        _, data = self._post_fields("/api/metadata", pdf, {"password": password})
        return json.loads(data.decode("utf-8"))

    def set_metadata(
        self,
        pdf: PdfInput,
        *,
        title: Optional[str] = None,
        author: Optional[str] = None,
        subject: Optional[str] = None,
        creator: Optional[str] = None,
        keywords: Optional[Sequence[str]] = None,
        password: Optional[str] = None,
    ) -> bytes:
        """Write document info fields into a PDF; give at least one. An empty string
        sets an empty value. The output is saved unencrypted.
        """
        fields = {
            "title": title,
            "author": author,
            "subject": subject,
            "creator": creator,
            "keywords": None if keywords is None else ",".join(keywords),
            "password": password,
        }
        return self._post_fields("/api/set-metadata", pdf, fields)[1]

    def extract_text(
        self,
        pdf: PdfInput,
        *,
        pages: Optional[str] = None,
        tables_format: Optional[str] = None,
        password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read the text layer of a born-digital PDF (no OCR): reading-order text,
        positioned lines and detected tables per page. ``pages`` is a 1-based spec
        such as ``"1-3,5,8-"``; default all. ``tables_format="csv"`` adds a ``csv``
        string (RFC 4180) to every table; ``rows`` is unchanged.

        Returns ``{"pageCount", "pages": [{"page", "width", "height", "text", "lines",
        "tables"}], "pagesWithoutText", "truncated"}``. When ``truncated`` is true the
        read stopped at a cap: continue with ``pages=f"{last_page + 1}-"``. For plain
        text see :meth:`extract_text_plain`; for a workbook, :meth:`extract_tables_xlsx`.

        Errors: ``invalid_pages``, ``invalid_tables_format``, ``encrypted_pdf``,
        ``decompressed_too_large``, ``too_many_pages``.
        """
        if tables_format == "xlsx":
            raise ValueError('tables_format="xlsx" returns a workbook, not JSON: use extract_tables_xlsx()')
        _, data = self._post_fields("/api/extract-text", pdf, {"pages": pages, "tables_format": tables_format, "password": password})
        return json.loads(data.decode("utf-8"))

    def extract_text_plain(
        self, pdf: PdfInput, *, pages: Optional[str] = None, password: Optional[str] = None
    ) -> ExtractTextPlainResult:
        """Like :meth:`extract_text` with ``format=text``: the pages' text joined by a
        form feed. ``truncated`` is the ``X-PDFops-Truncated`` header (continue with
        ``pages=`` the page after the last one you got)."""
        headers, data = self._post_fields("/api/extract-text", pdf, {"format": "text", "pages": pages, "password": password})
        return ExtractTextPlainResult(text=data.decode("utf-8"), truncated=_header(headers, "x-pdfops-truncated") == "true")

    def extract_tables_xlsx(
        self, pdf: PdfInput, *, pages: Optional[str] = None, password: Optional[str] = None
    ) -> ExtractTablesXlsxResult:
        """Like :meth:`extract_text` with ``tables_format=xlsx``: one .xlsx workbook with
        a sheet per detected table (``"Page 3 Table 1"``), every cell a string.
        ``truncated``, ``truncated_by`` and ``cells_truncated`` are the
        ``X-PDFops-*`` response headers.

        Errors: ``no_tables`` (no table on the selected pages; if the read was cut
        short, ``PdfOpsError.body`` also has ``truncated`` and ``nextPage``), plus
        those of :meth:`extract_text`.
        """
        headers, data = self._post_fields("/api/extract-text", pdf, {"tables_format": "xlsx", "pages": pages, "password": password})
        by = _header(headers, "x-pdfops-truncated-by") or ""
        return ExtractTablesXlsxResult(
            xlsx=data,
            truncated=_header(headers, "x-pdfops-truncated") == "true",
            truncated_by=[b for b in by.split(",") if b],
            cells_truncated=_header(headers, "x-pdfops-cells-truncated") == "true",
        )

    def find_text(
        self,
        pdf: PdfInput,
        query: str,
        *,
        regex: Optional[bool] = None,
        case_sensitive: Optional[bool] = None,
        whole_word: Optional[bool] = None,
        pages: Optional[str] = None,
        max_hits: Optional[int] = None,
        password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search the text layer for a literal string (or, with ``regex=True``, a
        pattern from a linear-time regex subset). Matches within one line, case
        insensitive unless ``case_sensitive=True``.

        Returns ``{"count", "truncated", "hits": [{"page", "text", "line", "x", "y",
        "width", "height"}], "pagesWithoutText", "pagesScanned", "nextPage"}``;
        x/y/width/height are PDF points in the :meth:`edit` frame (y is the baseline).
        When ``nextPage`` is not ``None`` the scan stopped early: repeat with
        ``pages=f"{nextPage}-"``. ``pagesSkipped`` and ``linesSkipped`` appear only
        when non-empty.

        Errors: ``missing_query``, ``query_too_long``, ``invalid_regex``,
        ``invalid_parameter``, ``invalid_pages``.
        """
        fields = {
            "query": query,
            "regex": None if regex is None else str(bool(regex)).lower(),
            "case_sensitive": None if case_sensitive is None else str(bool(case_sensitive)).lower(),
            "whole_word": None if whole_word is None else str(bool(whole_word)).lower(),
            "pages": pages, "max_hits": max_hits, "password": password,
        }
        _, data = self._post_fields("/api/find-text", pdf, fields)
        return json.loads(data.decode("utf-8"))

    def edit(
        self,
        pdf: PdfInput,
        items: Sequence[Mapping[str, Any]],
        *,
        images: Optional[Mapping[str, PdfInput]] = None,
        password: Optional[str] = None,
    ) -> bytes:
        """Stamp text, images and barcodes onto pages (1-100 ``items``). Coordinates
        are PDF points from the bottom-left of the page as displayed.

        Each item is a dict with ``type`` and ``page`` (1-based), ``x``, ``y``:
        ``"text"`` (``text``, optional ``size``, ``color``, ``font``, ``opacity``,
        ``rotation``, ``maxWidth``, ``lineHeight``), ``"image"`` (``image`` names a key
        of ``images``; optional ``width``, ``height``, ``opacity``) or ``"barcode"``
        (``format``, ``value``, optional ``width``, ``height``, ``color``,
        ``background``, ``includeText``, ``errorCorrection``, ``opacity``).
        ``images`` maps those names to PNG or JPEG data (bytes, path or binary file
        object, 2 MB each, at most 10). Keys must match ``[A-Za-z0-9_.-]{1,64}`` and
        may not be ``pdf``, ``password`` or ``items`` (``ValueError`` before sending).

        Errors: ``invalid_items``, ``too_many_items``, ``unsupported_characters``,
        ``invalid_pages``, ``invalid_image``.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [
            ("pdf", "input.pdf", _read_pdf(pdf), "application/pdf"),
            ("items", None, json.dumps(list(items)).encode("utf-8"), "application/json"),
        ]
        if password is not None:
            parts.append(("password", None, password.encode("utf-8"), "text/plain"))
        for name, image in (images or {}).items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or name in ("pdf", "password", "items"):
                raise ValueError(f"image key {name!r} must match [A-Za-z0-9_.-]{{1,64}} and not be pdf, password or items")
            parts.append((name, name, _read_pdf(image), "application/octet-stream"))
        return self._post_pdf("/api/edit", parts)

    def add_fields(
        self, pdf: PdfInput, fields: Sequence[Mapping[str, Any]], *, password: Optional[str] = None
    ) -> AddFieldsResult:
        """Create AcroForm fields (1-200) on a PDF. Each field is a dict with ``type``
        (``"text"``, ``"checkbox"``, ``"radio"``, ``"dropdown"``, ``"signature"``),
        ``name`` (unique, no ``"."``), ``page`` (1-based), ``x``, ``y``, ``width``,
        ``height`` (points from the bottom-left of the displayed page, the :meth:`edit`
        frame), optional ``required``, ``read_only``, ``border``, plus per type:
        text ``multiline``, ``max_length``, ``default``, ``font_size`` (number or
        ``"auto"``); checkbox ``checked``; radio ``options`` (``[{"value", "x", "y",
        "width", "height"}]``) and ``default``; dropdown ``options`` (strings),
        ``default``, ``font_size``. ``warning`` on the result is the
        ``x-pdfops-warning`` header (an XFA layer was dropped).

        Errors: ``invalid_fields``, ``too_many_fields``, ``duplicate_field``,
        ``invalid_pages``, ``unsupported_characters``.
        """
        headers, data = self._post_fields("/api/add-fields", pdf, {"fields": json.dumps(list(fields)), "password": password})
        return AddFieldsResult(pdf=data, warning=_header(headers, "x-pdfops-warning"))

    def attach(
        self,
        pdf: PdfInput,
        files: Sequence[Union[Tuple[str, PdfInput], List[Any], PdfInput]],
        *,
        options: Optional[Sequence[Mapping[str, str]]] = None,
        password: Optional[str] = None,
    ) -> AttachResult:
        """Embed files in a PDF (e.g. a ZUGFeRD XML). Each of ``files`` is a
        ``(filename, data)`` pair (a 2-item list works too), or a bare path (``str`` or
        ``os.PathLike``; its basename is the name). In a pair ``data`` is bytes, a
        bytearray, a binary file object or an ``os.PathLike`` to read; a plain ``str``
        is refused (``TypeError``) so text is never mistaken for a path: pass bytes, or
        ``pathlib.Path(...)`` to read a file. Bare bytes or file objects are named
        ``file-<i>``. A filename containing ``"`` is sent as the safe part name
        ``file-<i>`` with the real name in ``options[i]["name"]`` (unless you set one);
        a backslash, CR, LF or NUL in a name raise ``ValueError`` (the API refuses
        backslashes in attachment names). ``options`` is a list whose i-th
        entry applies to the i-th file: ``{"name", "mime_type", "description",
        "relationship"}`` (``relationship`` is ``Alternative|Data|Source|Supplement|
        Unspecified``). ``attachment_keys`` on the result is the
        ``x-pdfops-attachment-keys`` header, parsed.

        Errors: ``missing_files``, ``too_many_files``, ``files_too_large``,
        ``invalid_options``, ``invalid_name``, ``invalid_relationship``,
        ``duplicate_name``, ``too_many_attachments``.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [("pdf", "input.pdf", _read_pdf(pdf), "application/pdf")]
        opts: List[Dict[str, Any]] = [dict(o or {}) for o in options] if options is not None else []
        for i, f in enumerate(files):
            if isinstance(f, (tuple, list)):
                if len(f) != 2:
                    raise ValueError("a file pair must be (filename, data)")
                name, data = f
                if hasattr(name, "__fspath__"):
                    name = os.path.basename(os.fspath(name))
                if not isinstance(name, str):
                    raise TypeError("the filename in a (filename, data) pair must be a str or a path")
                if isinstance(data, str):
                    raise TypeError("pass bytes, or pathlib.Path(...) to read a file")
            elif isinstance(f, str) or hasattr(f, "__fspath__"):
                name, data = os.path.basename(os.fspath(f)), f  # type: ignore[arg-type]
            else:
                name, data = f"file-{i}", f
            if any(ch in name for ch in "\r\n\0\\"):
                raise ValueError(f"file name may not contain a backslash, CR, LF or NUL: {name!r}")
            part_name = name
            if '"' in name:
                # The handler takes the attachment name from options[i].name over the part filename.
                part_name = f"file-{i}"
                while len(opts) <= i:
                    opts.append({})
                opts[i].setdefault("name", name)
            parts.append(("file", part_name, _read_pdf(data), _attachment_mime(name)))
        if opts:
            parts.append(("options", None, json.dumps(opts).encode("utf-8"), "application/json"))
        if password is not None:
            parts.append(("password", None, password.encode("utf-8"), "text/plain"))
        ctype, body = _multipart(parts)
        headers, data_out = self._request_full("POST", "/api/attach", self._headers({"Content-Type": ctype}), body)
        try:  # a response header is untrusted: never raise after a successful call
            parsed = json.loads(_header(headers, "x-pdfops-attachment-keys") or "[]")
        except ValueError:
            parsed = []
        # All or nothing: dropping one bad entry would shift the rest against the files sent.
        ok = (
            isinstance(parsed, list)
            and len(parsed) == len(files)
            and all(isinstance(k, dict) and isinstance(k.get("name"), str) and isinstance(k.get("key"), str) for k in parsed)
        )
        keys = [{"name": k["name"], "key": k["key"]} for k in parsed] if ok else []
        return AttachResult(pdf=data_out, attachment_keys=keys)

    def html_to_pdf(
        self,
        html: str,
        *,
        page_size: Optional[str] = None,
        landscape: Optional[bool] = None,
        margin: Optional[Union[float, Mapping[str, float]]] = None,
        print_background: Optional[bool] = None,
        scale: Optional[float] = None,
    ) -> HtmlToPdfResult:
        """Render an HTML document to PDF in a headless browser. ``page_size`` is
        ``"letter"`` (default) or ``"a4"``; ``margin`` is points (0-144) or a
        ``{"top", "right", "bottom", "left"}`` dict (default 36); ``print_background``
        defaults to true; ``scale`` is 0.1-2. ``pages`` and ``blocked_requests`` on the
        result are the ``x-pdfops-pages`` and ``x-pdfops-blocked-requests`` headers.
        Each successful call costs more quota than the other endpoints.

        Errors: ``invalid_html``, ``html_too_large``, ``invalid_page_size``,
        ``invalid_parameter``, ``too_many_pages``, ``too_many_resources``,
        ``resources_too_large``, ``output_too_large``, ``image_too_large``,
        ``unsupported_image``, ``render_timeout``, ``renderer_busy`` and
        ``renderer_unavailable`` (retry shortly).
        """
        opts = {"page_size": page_size, "landscape": landscape, "margin": margin, "print_background": print_background, "scale": scale}
        body = json.dumps({"html": html, **{k: v for k, v in opts.items() if v is not None}}).encode("utf-8")
        headers, data = self._request_full("POST", "/api/html-to-pdf", self._headers({"Content-Type": "application/json"}), body)
        pages, blocked = _header(headers, "x-pdfops-pages"), _header(headers, "x-pdfops-blocked-requests")
        return HtmlToPdfResult(
            pdf=data,
            pages=_digits(pages),
            blocked_requests=_digits(blocked),
        )

    def image_to_pdf(
        self,
        images: Sequence[PdfInput],
        *,
        page_size: Optional[str] = None,
        orientation: Optional[str] = None,
        margin: Optional[float] = None,
    ) -> bytes:
        """Make a PDF with one page per JPG or PNG, in order. ``page_size`` is
        ``"fit"`` (default: page = image size), ``"a4"`` or ``"letter"``;
        ``orientation`` is ``"auto"`` (default), ``"portrait"`` or ``"landscape"``
        (ignored for ``fit``); ``margin`` is points, 0-200.

        Errors: ``missing_images``, ``too_many_images``, ``unsupported_image_format``,
        ``invalid_image``, ``image_too_large``, ``images_too_large_together``,
        ``image_aspect_ratio``, ``invalid_parameter``.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [
            ("pdf", f"image-{i}", _read_pdf(img), "application/octet-stream") for i, img in enumerate(images)
        ]
        for name, value in {"page_size": page_size, "orientation": orientation, "margin": margin}.items():
            if value is not None:
                parts.append((name, None, str(value).encode("utf-8"), "text/plain"))
        return self._post_pdf("/api/image-to-pdf", parts)

    def text_to_pdf(
        self,
        content: str,
        *,
        format: Optional[str] = None,
        page_size: Optional[str] = None,
        margin: Optional[float] = None,
        font_size: Optional[float] = None,
        title: Optional[str] = None,
        page_numbers: Optional[bool] = None,
        monospace: Optional[bool] = None,
    ) -> bytes:
        """Lay out Markdown, plain text or CSV as a PDF (not HTML rendering).
        ``format`` is ``"markdown"`` (default), ``"text"`` or ``"csv"``; ``page_size``
        ``"letter"`` (default) or ``"a4"``; ``margin`` 18-144 pt (default 54);
        ``font_size`` 6-36 pt (default 11); ``title`` sets the PDF title;
        ``page_numbers`` adds a "Page n of N" footer; ``monospace`` applies to
        ``"text"``. Deterministic: the same request gives the same bytes. Standard
        fonts only, so characters outside WinAnsi are refused.

        Errors: ``missing_content``, ``content_too_large``, ``invalid_format``,
        ``invalid_page_size``, ``invalid_parameter``, ``unsupported_characters``,
        ``invalid_csv``, ``too_many_columns``, ``csv_too_large``, ``table_too_wide``,
        ``table_too_large``, ``content_too_complex``, ``too_many_pages``.
        """
        opts = {
            "format": format, "page_size": page_size, "margin": margin, "font_size": font_size,
            "title": title, "page_numbers": page_numbers, "monospace": monospace,
        }
        body = json.dumps({"content": content, **{k: v for k, v in opts.items() if v is not None}}).encode("utf-8")
        return self._request("POST", "/api/text-to-pdf", self._headers({"Content-Type": "application/json"}), body)

    def usage(self) -> Dict[str, Any]:
        """Quota state for the configured key: tier, limit, used, remaining, period, resets_at."""
        if not self.api_key:
            raise PdfOpsError(401, "missing_api_key", "usage() requires an api_key")
        data = self._request("GET", "/api/usage", self._headers(), None)
        return json.loads(data.decode("utf-8"))

    def signup(self, email: str) -> None:
        """Request a free API key (250 req/mo). The key is emailed, never returned."""
        body = json.dumps({"email": email}).encode("utf-8")
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        self._request("POST", "/api/signup", headers, body)
