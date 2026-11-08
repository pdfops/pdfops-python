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
import os
import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib import error as urlerror
from urllib import request as urlrequest

__all__ = ["PdfOps", "PdfOpsError", "PdfInput", "SplitPart", "SplitResult", "UnlockResult", "__version__"]
__version__ = "0.2.0"

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


class PdfOpsError(Exception):
    """Structured API error. ``code`` is the API's stable ``error`` slug."""

    def __init__(self, status: int, code: str, details: str) -> None:
        super().__init__(f"{code}: {details}")
        self.status = status
        self.code = code
        self.details = details


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


def _urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]) -> Tuple[int, Mapping[str, str], bytes]:
        req = urlrequest.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urlrequest.urlopen(req, timeout=timeout) as res:  # noqa: S310 — fixed https origin
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
        try:
            parsed = json.loads(data.decode("utf-8"))
            if isinstance(parsed, dict):
                code = str(parsed.get("error") or code)
                details = str(parsed.get("details") or details)
        except ValueError:
            pass  # non-JSON error body — keep the status fallback
        raise PdfOpsError(status, code, details)

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

    def inspect(self, pdf: PdfInput) -> Dict[str, Any]:
        """List a PDF's AcroForm fields plus a paste-ready ``fillTemplate``.

        Returns ``{"count", "hasXFA", "fields": [...], "fillTemplate": {...}}``.
        A PDF with no form returns ``count == 0`` — not an error.
        """
        data = self._post_pdf("/api/inspect", [("pdf", "input.pdf", _read_pdf(pdf), "application/pdf")])
        return json.loads(data.decode("utf-8"))

    def fill_form(self, pdf: PdfInput, fields: Mapping[str, str], *, flatten: bool = False) -> bytes:
        """Fill AcroForm fields; returns the filled PDF bytes.

        Field names must exist in the PDF — use :meth:`inspect` to discover
        them. Checkbox values are the strings ``"true"``/``"false"``; choice
        fields take one of their options. ``flatten=True`` bakes values into
        the page content and drops the form so fields are no longer editable.
        """
        parts: List[Tuple[str, Optional[str], bytes, str]] = [
            ("pdf", "input.pdf", _read_pdf(pdf), "application/pdf"),
            ("fields", None, json.dumps(dict(fields)).encode("utf-8"), "application/json"),
        ]
        if flatten:
            parts.append(("flatten", None, b"true", "text/plain"))
        return self._post_pdf("/api/fill-form", parts)

    def merge(self, pdfs: Sequence[PdfInput]) -> bytes:
        """Merge two or more PDFs, in order; returns the combined PDF bytes."""
        if len(pdfs) < 2:
            raise PdfOpsError(400, "too_few_pdfs", "merge needs at least 2 PDFs")
        parts = [("pdf", f"part-{i}.pdf", _read_pdf(p), "application/pdf") for i, p in enumerate(pdfs)]
        return self._post_pdf("/api/merge", parts)

    def invoice(self, request: Mapping[str, Any]) -> bytes:
        """Generate a complete invoice PDF from structured data.

        Deterministic: the same request produces byte-identical output, so it
        is safe to re-render from webhooks and retries. Keys: ``from``, ``to``
        (string or ``{"name", "lines"}``), ``items`` (``description``,
        ``unit_price``, optional ``quantity``), optional ``invoice_number``,
        ``date``, ``due``, ``currency``, ``tax_rate``, ``notes``.
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
