
from pathlib import Path
from io import BytesIO
from hashlib import sha256
import re
import os

import pymupdf
from openpyxl import load_workbook
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field
from ollama import Client

app = FastAPI(title="SustivioLabs Extraction API", version="0.1.0")

MODEL = os.getenv("SUSTIVIO_MODEL", "qwen3-vl:4b")
ai = Client(host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"), timeout=300)

MAX_UPLOAD = 20 * 1024 * 1024


class ESGField(BaseModel):
    metric: str
    value: str
    unit: str | None = None
    period: str | None = None
    evidence_quote: str | None = None


class Extraction(BaseModel):
    fields: list[ESGField] = Field(default_factory=list)


PROMPT = """
You extract ESG and sustainability data from business records.

Extract up to 15 explicit numerical measurements, prioritizing:
- Electricity and energy consumption
- Fuel quantities
- Scope 1, 2, or 3 emissions
- Water and waste
- Purchased material quantities
- Transport activity and distances

Rules:
1. Copy reported numbers exactly as displayed.
2. Never calculate, estimate, or invent values.
3. Never infer units or reporting periods that are not explicit.
4. For each value, include a short exact evidence_quote.
5. Return an empty fields list if no relevant values exist.
6. Ignore numbers that only identify addresses or documents.
7. Do not treat unverified source values as approved.
"""


def ask_model(text=None, image=None):
    if text is None and image is None:
        return Extraction()

    message = {"role": "user", "content": PROMPT}

    if text is not None:
        message["content"] += "\nSOURCE TEXT:\n" + text[:12000]

    if image is not None:
        message["images"] = [image]

    try:
        response = ai.chat(
            model=MODEL,
            messages=[message],
            format=Extraction.model_json_schema(),
            options={"temperature": 0, "num_ctx": 6144},
        )
        return Extraction.model_validate_json(
            response.message.content
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Local AI extraction failed: {exc}",
        ) from exc


def normalized(text):
    return " ".join((text or "").lower().split())


def extract_pdf(blob, page_number, use_vision):
    try:
        doc = pymupdf.open(stream=blob, filetype="pdf")
    except Exception as exc:
        raise HTTPException(422, f"Invalid PDF: {exc}") from exc

    with doc:
        if doc.needs_pass:
            raise HTTPException(422, "Password-protected PDF")

        if not 1 <= page_number <= len(doc):
            raise HTTPException(
                422, f"Page must be between 1 and {len(doc)}"
            )

        page = doc[page_number - 1]
        text = page.get_text("text", sort=True)

        # Scan or visually inspect complex tables when requested.
        vision_used = use_vision or len(text.strip()) < 60

        if vision_used:
            image = page.get_pixmap(
                dpi=140, alpha=False
            ).tobytes("png")
            result = ask_model(image=image)
        else:
            result = ask_model(text=text)

        records = []

        for field in result.fields:
            quote = field.evidence_quote or ""
            quote_found = bool(
                not vision_used
                and quote
                and normalized(quote) in normalized(text)
            )

            records.append({
                **field.model_dump(),
                "source_page": page_number,
                "source_type": "pdf",
                "quote_matched": quote_found,
                "review_status": "needs_review",
            })

        return {
            "pages_in_document": len(doc),
            "page_processed": page_number,
            "vision_used": vision_used,
            "records": records,
        }


def extract_image(blob):
    result = ask_model(image=blob)

    return {
        "vision_used": True,
        "records": [
            {
                **field.model_dump(),
                "source_type": "image",
                "quote_matched": False,
                "review_status": "needs_review",
            }
            for field in result.fields
        ],
    }


NUMBER_PATTERN = re.compile(
    r"^\s*[₹$]?\s*-?\d[\d,]*(?:\.\d+)?\s*%?\s*$"
)


def extract_excel(blob, sheet_name=None):
    try:
        workbook = load_workbook(
            BytesIO(blob),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception as exc:
        raise HTTPException(
            422, f"Cannot read workbook: {exc}"
        ) from exc

    try:
        if sheet_name and sheet_name not in workbook.sheetnames:
            raise HTTPException(
                422, f"Unknown sheet. Available: {workbook.sheetnames}"
            )

        ws = workbook[sheet_name] if sheet_name else workbook.active
        records = []

        # Limited initial sample for fast local tests.
        for row in ws.iter_rows(
            min_row=1,
            max_row=min(ws.max_row or 1, 200),
            max_col=min(ws.max_column or 1, 40),
        ):
            for index, cell in enumerate(row):
                value = cell.value

                if value is None or isinstance(value, bool):
                    continue

                is_number = (
                    isinstance(value, (int, float))
                    or (
                        isinstance(value, str)
                        and bool(NUMBER_PATTERN.fullmatch(value))
                    )
                )

                if not is_number:
                    continue

                previous = row[max(0, index - 4):index]

                labels = [
                    str(c.value).strip()
                    for c in previous
                    if isinstance(c.value, str)
                    and not c.value.startswith("=")
                ]

                records.append({
                    "metric": labels[-1] if labels else "Unclassified value",
                    "value": str(value),
                    "unit": None,
                    "period": None,
                    "source_type": "excel",
                    "sheet": ws.title,
                    "cell": cell.coordinate,
                    "review_status": "needs_mapping",
                })

                if len(records) >= 150:
                    break

            if len(records) >= 150:
                break

        return {
            "sheet_processed": ws.title,
            "available_sheets": workbook.sheetnames,
            "records": records,
            "note": (
                "Raw numeric cells only. ESG metric classification "
                "and formula evaluation are not implemented yet."
            ),
        }
    finally:
        workbook.close()


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.post("/extract")
def extract(
    file: UploadFile = File(...),
    page: int = Form(1),
    vision: bool = Form(False),
    sheet: str | None = Form(None),
):
    filename = file.filename or "unnamed"
    extension = Path(filename).suffix.lower()

    blob = file.file.read(MAX_UPLOAD + 1)

    if len(blob) > MAX_UPLOAD:
        raise HTTPException(413, "File exceeds 20 MB limit")

    if not blob:
        raise HTTPException(422, "Empty file")

    digest = sha256(blob).hexdigest()

    if extension == ".pdf":
        result = extract_pdf(blob, page, vision)

    elif extension in {".png", ".jpg", ".jpeg"}:
        result = extract_image(blob)

    elif extension in {".xlsx", ".xlsm"}:
        result = extract_excel(blob, sheet)

    else:
        raise HTTPException(
            415,
            "Supported: PDF, PNG, JPG, JPEG, XLSX, XLSM",
        )

    return {
        "filename": filename,
        "sha256": digest,
        "model": MODEL if extension not in {".xlsx", ".xlsm"} else None,
        **result,
    }
