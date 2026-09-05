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

import json
import os
import uuid
from pathlib import PurePath
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib import error as urlerror
from urllib import request as urlrequest

__all__ = ["PdfOps", "PdfOpsError", "PdfInput", "__version__"]
__version__ = "0.1.0"

#: A PDF given as raw bytes, a filesystem path, or a binary file object.
PdfInput = Union[bytes, bytearray, memoryview, str, "os.PathLike[str]", IO[bytes]]

#: ``transport(method, url, headers, body) -> (status, headers, body)``.
#: Injected in tests; the default speaks HTTP through urllib.
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, Mapping[str, str], bytes]]

DEFAULT_BASE_URL = "https://pdfops.dev"
USER_AGENT = f"pdfops-python/{__version__}"


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
        status, _res_headers, data = self._send(method, f"{self.base_url}{path}", headers, body)
        if 200 <= status < 300:
            return data
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
