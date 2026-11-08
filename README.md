# pdfops

Python client for the [PDFops API](https://pdfops.dev): **fill** AcroForm PDFs, **merge** and **split** PDFs, run **page operations** (extract, delete, rotate, watermark, page numbers), **protect / unlock** and edit **metadata**, **inspect** form fields, and **generate invoices** with one HTTP call. No native dependencies, no headless browser, nothing to install but this package.

Built for the moment `pypdf` / `PyPDFForm` stop cooperating — appearance streams that never render, `NeedAppearances` flags, encrypted government blanks, checkboxes that will not tick — and for runtimes where a PDF toolchain does not fit: Lambda, Cloud Functions, Airflow tasks, notebooks.

```bash
pip install pdfops
```

Zero dependencies (standard-library `urllib`), Python 3.9+, fully typed.

## Quick start

```python
from pdfops import PdfOps

client = PdfOps(api_key="pdfops_live_...")   # or PdfOps() for the keyless trial (100 req/IP/month)

# 1. Discover the field names — you cannot fill fields you cannot name.
info = client.inspect("blank-w9.pdf")
print(info["count"], "fields")
fields = info["fillTemplate"]                  # paste-ready {name: ""} dict

# 2. Fill, optionally flattening so the result is no longer editable.
fields["Name"] = "Ada Lovelace"
fields["Agree"] = "true"                       # checkboxes take "true" / "false"
pdf_bytes = client.fill_form("blank-w9.pdf", fields, flatten=True)
open("w9-filled.pdf", "wb").write(pdf_bytes)

# 3. Merge, in order.
packet = client.merge(["cover.pdf", pdf_bytes, "terms.pdf"])

# 4. Invoice from data — deterministic, safe to re-render from a webhook.
invoice = client.invoice({
    "from": "Acme Ltd",
    "to": {"name": "Ada Lovelace", "lines": ["12 Analytical Way", "London"]},
    "items": [{"description": "Consulting", "quantity": 3, "unit_price": 120.0}],
    "invoice_number": "2026-0042",
    "date": "2026-09-05",
    "currency": "GBP",
})
```

Page operations, one line each (all keyword-only options are optional and omitted when unset):

```python
first_two = client.extract_pages("in.pdf", "1-2")
trimmed = client.delete_pages("in.pdf", "2,4-6")
turned = client.rotate("in.pdf", 90, pages="1")
res = client.split("in.pdf", ranges="1-2,3-")       # or every=5; res.parts[0].pdf is bytes
marked = client.watermark("in.pdf", "DRAFT", opacity=0.2)
numbered = client.page_numbers("in.pdf", format="Page {n} of {total}", position="bottom-right")
opened = client.unlock("locked.pdf")                # opened.pdf, opened.note ("not-encrypted" or None)
sealed = client.protect("in.pdf", user_password="s3cret", permissions=["print"])   # [] allows none
info = client.metadata("in.pdf")                    # info["page_count"], info["title"], ...
titled = client.set_metadata("in.pdf", title="Q3 report", keywords=["finance", "q3"])
```

PDF inputs may be `bytes`, a path (`str` / `pathlib.Path`), or a binary file object. Every PDF-producing call returns `bytes`.

## API

| Method | Endpoint | Returns |
|---|---|---|
| `inspect(pdf)` | `POST /api/inspect` | `dict` — `count`, `hasXFA`, `fields[]`, `fillTemplate` |
| `fill_form(pdf, fields, *, flatten=False)` | `POST /api/fill-form` | filled PDF `bytes` |
| `merge([pdf, pdf, ...])` | `POST /api/merge` | merged PDF `bytes` |
| `invoice(request)` | `POST /api/invoice` | invoice PDF `bytes` |
| `extract_pages(pdf, pages, *, password=None)` | `POST /api/extract-pages` | PDF `bytes` with only those pages, in that order |
| `delete_pages(pdf, pages, *, password=None)` | `POST /api/delete-pages` | PDF `bytes` without those pages |
| `rotate(pdf, degrees, *, pages=None, password=None)` | `POST /api/rotate` | rotated PDF `bytes` (90, 180, 270, -90) |
| `split(pdf, *, ranges=None, every=None, password=None)` | `POST /api/split` | `SplitResult` — `count`, `parts[]` of `pages`, `page_count`, `pdf` bytes |
| `watermark(pdf, text, *, opacity, size, color, rotation, position, pages, password)` | `POST /api/watermark` | watermarked PDF `bytes` |
| `page_numbers(pdf, *, format, position, size, start, margin, pages, password)` | `POST /api/page-numbers` | numbered PDF `bytes` |
| `unlock(pdf, password=None)` | `POST /api/unlock` | `UnlockResult` — `pdf` bytes plus optional `note` |
| `protect(pdf, *, user_password, owner_password, permissions, password)` | `POST /api/protect` | encrypted PDF `bytes` |
| `metadata(pdf, *, password=None)` | `POST /api/metadata` | `dict` — page count, page sizes, title, author, dates, form field count |
| `set_metadata(pdf, *, title, author, subject, creator, keywords, password)` | `POST /api/set-metadata` | PDF `bytes` with the info fields written |
| `usage()` | `GET /api/usage` | `dict` — tier, limit, used, remaining, resets_at |
| `signup(email)` | `POST /api/signup` | `None` — the key is emailed |

Full request/response schemas: [pdfops.dev/docs](https://pdfops.dev/docs) · [OpenAPI](https://pdfops.dev/openapi.json).

## Errors

Every non-2xx response raises `PdfOpsError` carrying the API's stable error slug:

```python
from pdfops import PdfOps, PdfOpsError

try:
    client.fill_form("form.pdf", {"Nmae": "typo"})
except PdfOpsError as e:
    print(e.status, e.code, e.details)   # 422 unknown_field No field named Nmae
```

Common codes: `unknown_field`, `exceeds_max_length`, `invalid_option`, `encrypted_pdf`, `rate_limited`, `invalid_api_key`.

## Configuration

- `PdfOps(api_key=...)` or the `PDFOPS_API_KEY` environment variable. A free key (250 req/month, no card) is one field at [pdfops.dev/pricing](https://pdfops.dev/pricing).
- `client_tag="..."` sets the `X-Pdfops-Client` header for anonymous usage attribution when you embed this client in another tool. Defaults to `"py"`.
- `timeout=60.0` seconds per request; `base_url=` overrides the API origin for testing.
- `transport=` injects a `(method, url, headers, body) -> (status, headers, body)` callable — the test suite uses this; no network needed.

## Why an API instead of a library

`pdf-lib` and `pypdf` are excellent when the PDF is well-behaved. PDFops exists for the other ones: it renders appearance streams server-side so every viewer shows the values, handles the encrypted-with-empty-password blanks governments publish, flattens deterministically, and is byte-for-byte reproducible — the same request yields the same file. The [compatibility registry](https://pdfops.dev/registry) records which public forms fill cleanly with which toolchain.

## Links

[Fill a PDF in Python](https://pdfops.dev/fill-pdf-in-python) · [Docs](https://pdfops.dev/docs) · [MCP server for AI agents](https://www.npmjs.com/package/pdfops-mcp) · [TypeScript SDK](https://www.npmjs.com/package/pdfops-sdk) · [n8n node](https://www.npmjs.com/package/n8n-nodes-pdfops) · hello@pdfops.dev

MIT © PDFops
