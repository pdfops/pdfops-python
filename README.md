# pdfops

Python client for the [PDFops API](https://pdfops.dev): **fill** AcroForm PDFs, **merge** and **split** PDFs, run **page operations** (extract, delete, rotate, watermark, page numbers), **protect / unlock** and edit **metadata**, **inspect** form fields, and **generate invoices** with one HTTP call. No native dependencies, no headless browser, nothing to install but this package.

Built for the moment `pypdf` / `PyPDFForm` stop cooperating: appearance streams that never render, `NeedAppearances` flags, encrypted government blanks, checkboxes that will not tick. It also fits runtimes where a PDF toolchain does not: Lambda, Cloud Functions, Airflow tasks, notebooks.

```bash
pip install pdfops
```

Zero dependencies (standard-library `urllib`), Python 3.9+, fully typed.

## Quick start

```python
from pdfops import PdfOps

client = PdfOps()   # keyless trial (100 req/IP/month); with a free key from https://pdfops.dev/pricing: PdfOps(api_key="...")

# 1. Discover the field names: you cannot fill fields you cannot name.
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

# 4. Invoice from data: deterministic, safe to re-render from a webhook.
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

Reading, stamping, building and attaching:

```python
doc = client.extract_text("report.pdf", pages="1-3", tables_format="csv")   # dict: pages[].text, lines, tables
print(doc["pages"][0]["text"], doc["truncated"])
plain = client.extract_text_plain("report.pdf")     # plain.text (pages joined by "\f"), plain.truncated
book = client.extract_tables_xlsx("report.pdf")     # book.xlsx bytes; book.truncated, book.truncated_by, book.cells_truncated
hits = client.find_text("report.pdf", "Total", whole_word=True)   # hits["hits"][0]["page"], ["x"], ["y"]; hits["nextPage"] is None when done
stamped = client.edit("in.pdf", [
    {"type": "text", "page": 1, "x": 72, "y": 700, "text": "APPROVED", "size": 18, "color": "#cc0000"},
    {"type": "image", "page": 1, "x": 400, "y": 40, "width": 120, "image": "sig"},
], images={"sig": "signature.png"})
form = client.add_fields("in.pdf", [
    {"type": "text", "name": "Email", "page": 1, "x": 72, "y": 600, "width": 240, "height": 20},
    {"type": "checkbox", "name": "Agree", "page": 1, "x": 72, "y": 560, "width": 14, "height": 14},
])                                                  # form.pdf bytes; form.warning is the XFA header or None
flat = client.detect_fields("flat.pdf")             # dict: fields[] (type, name, page, x, y, width, height, label, confidence), count, truncated; review it
fillable = client.make_fillable("flat.pdf")         # fillable.pdf bytes; fillable.count, fillable.names, fillable.truncated
sealed = client.attach("invoice.pdf", [("factur-x.xml", xml_bytes)], options=[{"relationship": "Data"}])
print(sealed.attachment_keys)                       # [{"name": "factur-x.xml", "key": ...}]
html = client.html_to_pdf("<h1>Report</h1>", page_size="a4", margin=24)   # html.pdf bytes; html.pages, html.blocked_requests
album = client.image_to_pdf(["scan1.jpg", "scan2.png"], page_size="a4")
memo = client.text_to_pdf("# Notes\n\nSome *text*.", format="markdown", page_numbers=True)
```

`edit()` image keys must match `[A-Za-z0-9_.-]{1,64}` and not be `pdf`, `password` or `items`. In `attach()`, a file pair is `(filename, data)` where `data` is bytes, a file object or a `pathlib.Path` (a plain `str` is refused so text is never read as a path); a bare `str` entry is a path.

E-invoices: add `"e_invoice": "factur-x"` to an `invoice()` request to get a PDF/A-3 Factur-X (ZUGFeRD, profile EN 16931) file. That mode needs more fields, and a missing one raises `PdfOpsError` with code `einvoice_fields_required`; `e.body["missing"]` lists them all.

```python
from pdfops import PdfOps, PdfOpsError

client = PdfOps()
try:
    pdf = client.invoice({
        "e_invoice": "factur-x",
        "from": {"name": "Acme GmbH", "street": "Hauptstr. 1", "postcode": "10115", "city": "Berlin", "country": "DE", "vat_id": "DE123456789"},
        "to": {"name": "Client SARL", "country": "FR"},
        "items": [{"description": "Consulting", "quantity": 3, "unit": "HUR", "unit_price": 120}],
        "invoice_number": "2026-0042", "date": "2026-11-01", "due": "2026-12-01",
        "currency": "EUR", "tax_rate": 19, "iban": "DE89370400440532013000",
    })
except PdfOpsError as e:
    print(e.code, (e.body or {}).get("missing"))
```

PDF inputs may be `bytes`, a path (`str` / `pathlib.Path`), or a binary file object. Most calls that produce a PDF return `bytes`; the ones that also report response headers (`unlock`, `add_fields`, `detect_fields_pdf`, `attach`, `html_to_pdf`) return a small result object with the PDF in `.pdf`.

## API

| Method | Endpoint | Returns |
|---|---|---|
| `inspect(pdf, *, password=None)` | `POST /api/inspect` | `dict`: `count`, `hasXFA`, `fields[]`, `fillTemplate` |
| `fill_form(pdf, fields, *, flatten=False, password=None)` | `POST /api/fill-form` | filled PDF `bytes` |
| `merge([pdf, pdf, ...], *, password=None)` | `POST /api/merge` | merged PDF `bytes` |
| `invoice(request)` | `POST /api/invoice` | invoice PDF `bytes` |
| `extract_pages(pdf, pages, *, password=None)` | `POST /api/extract-pages` | PDF `bytes` with only those pages, in that order |
| `delete_pages(pdf, pages, *, password=None)` | `POST /api/delete-pages` | PDF `bytes` without those pages |
| `rotate(pdf, degrees, *, pages=None, password=None)` | `POST /api/rotate` | rotated PDF `bytes` (90, 180, 270, -90) |
| `split(pdf, *, ranges=None, every=None, password=None)` | `POST /api/split` | `SplitResult`: `count`, `parts[]` of `pages`, `page_count`, `pdf` bytes |
| `watermark(pdf, text, *, opacity, size, color, rotation, position, pages, password)` | `POST /api/watermark` | watermarked PDF `bytes` |
| `page_numbers(pdf, *, format, position, size, start, margin, pages, password)` | `POST /api/page-numbers` | numbered PDF `bytes` |
| `unlock(pdf, password=None)` | `POST /api/unlock` | `UnlockResult`: `pdf` bytes plus optional `note` |
| `protect(pdf, *, user_password, owner_password, permissions, password)` | `POST /api/protect` | encrypted PDF `bytes` |
| `metadata(pdf, *, password=None)` | `POST /api/metadata` | `dict`: page count, page sizes, title, author, dates, form field count |
| `set_metadata(pdf, *, title, author, subject, creator, keywords, password)` | `POST /api/set-metadata` | PDF `bytes` with the info fields written |
| `extract_text(pdf, *, pages, tables_format, password)` | `POST /api/extract-text` | `dict`: `pageCount`, `pages[]` (text, lines, tables), `pagesWithoutText`, `truncated`. `tables_format="csv"` adds a `csv` string per table |
| `extract_text_plain(pdf, *, pages, password)` | `POST /api/extract-text` (`format=text`) | `ExtractTextPlainResult`: `text`, `truncated` |
| `extract_tables_xlsx(pdf, *, pages, password)` | `POST /api/extract-text` (`tables_format=xlsx`) | `ExtractTablesXlsxResult`: `xlsx` bytes, `truncated`, `truncated_by`, `cells_truncated` |
| `find_text(pdf, query, *, regex, case_sensitive, whole_word, pages, max_hits, password)` | `POST /api/find-text` | `dict`: `count`, `truncated`, `hits[]` (page, text, line, x, y, width, height), `pagesScanned`, `nextPage` |
| `edit(pdf, items, *, images, password)` | `POST /api/edit` | PDF `bytes` with text, image and barcode stamps |
| `add_fields(pdf, fields, *, password)` | `POST /api/add-fields` | `AddFieldsResult`: `pdf` bytes plus optional `warning` |
| `detect_fields(pdf, *, pages, max_pages, min_confidence, password)` | `POST /api/detect-fields` | `dict`: `count`, `truncated`, `nextPages`, `fields[]` (each one is valid for `add_fields`). Detects fields drawn as lines, boxes, underscores and checkboxes; review the result |
| `detect_fields_pdf(pdf, *, pages, max_pages, min_confidence, password)` | `POST /api/detect-fields` (`format=pdf`) | `DetectFieldsPdfResult`: fillable `pdf` bytes plus `count`, `names`, `truncated`, `truncated_by`, `next_pages`, `pages_skipped`, `warning`. Raises `no_fields` when nothing is found |
| `make_fillable(pdf, *, pages, max_pages, min_confidence, password, max_rounds=25)` | `POST /api/detect-fields` (`format=pdf`), repeated | `DetectFieldsPdfResult` after sending the result back with `pages=next_pages` until `truncated` is false (each round is one metered call). Continues past `no_fields` while pages are left; when the loop ends early you get a partial result (the PDF so far with `truncated` true, `next_pages` and `stopped_by` = `{code, message}`; code is `stalled`, `max_rounds` or the API error code of the failed round) only if at least one field was added; otherwise it raises: `PdfOpsError` `no_fields` naming the stop (`stalled`, `max_rounds`) and the pages left in `body["nextPages"]`, or the failed round's own error unchanged |
| `attach(pdf, files, *, options, password)` | `POST /api/attach` | `AttachResult`: `pdf` bytes plus `attachment_keys` |
| `html_to_pdf(html, *, page_size, landscape, margin, print_background, scale)` | `POST /api/html-to-pdf` | `HtmlToPdfResult`: `pdf` bytes plus `pages`, `blocked_requests` |
| `image_to_pdf(images, *, page_size, orientation, margin)` | `POST /api/image-to-pdf` | PDF `bytes`, one page per image |
| `text_to_pdf(content, *, format, page_size, margin, font_size, title, page_numbers, monospace)` | `POST /api/text-to-pdf` | PDF `bytes` laid out from Markdown, text or CSV |
| `usage()` | `GET /api/usage` | `dict`: tier, limit, used, remaining, resets_at |
| `signup(email)` | `POST /api/signup` | `None`: the key is emailed |

Full request/response schemas: [pdfops.dev/docs](https://pdfops.dev/docs) · [OpenAPI](https://pdfops.dev/openapi.json).

## Errors

Every non-2xx response raises `PdfOpsError` carrying the API's stable error slug:

```python
from pdfops import PdfOps, PdfOpsError

try:
    client.fill_form("form.pdf", {"Nmae": "typo"})
except PdfOpsError as e:
    print(e.status, e.code, e.details)   # 400 unknown_field No field named Nmae
```

Common codes: `unknown_field`, `exceeds_max_length`, `invalid_option`, `encrypted_pdf`, `rate_limited`, `invalid_api_key`. E-invoice requests add `einvoice_fields_required`, `invalid_einvoice_field` and `unrenderable_text`.

`e.body` holds the full parsed JSON error body (or `None` for a non-JSON error), for fields beyond `error` and `details`, such as `missing` on `einvoice_fields_required`.

## Configuration

- `PdfOps(api_key=...)` or the `PDFOPS_API_KEY` environment variable. A free key (250 req/month, no card) is one field at [pdfops.dev/pricing](https://pdfops.dev/pricing).
- `client_tag="..."` sets the `X-Pdfops-Client` header for anonymous usage attribution when you embed this client in another tool. Defaults to `"py"`.
- `timeout=60.0` seconds per request; `base_url=` overrides the API origin for testing.
- `transport=` injects a `(method, url, headers, body) -> (status, headers, body)` callable: the test suite uses this; no network needed.

## Why an API instead of a library

`pdf-lib` and `pypdf` are excellent when the PDF is well-behaved. PDFops exists for the other ones: it renders appearance streams server-side so every viewer shows the values, handles the encrypted-with-empty-password blanks governments publish, flattens deterministically, and is byte-for-byte reproducible: the same request yields the same file. The [compatibility registry](https://pdfops.dev/registry) records which public forms fill cleanly with which toolchain.

## Links

[Fill a PDF in Python](https://pdfops.dev/fill-pdf-in-python) · [Docs](https://pdfops.dev/docs) · [MCP server for AI agents](https://www.npmjs.com/package/pdfops-mcp) · [TypeScript SDK](https://www.npmjs.com/package/pdfops-sdk) · [n8n node](https://www.npmjs.com/package/n8n-nodes-pdfops) · hello@pdfops.dev

MIT © PDFops
