"""
Servicio de estampado de firma para certificaciones EPC Workflow.

Estampa la imagen de firma en la casilla del rol (SUPERVISOR / SITE MANAGER /
PROJECT MANAGER) localizándola por texto ancla, y guarda con actualización
incremental para no invalidar las firmas digitales previas (contratista, N1, N2).

POST /stamp  (multipart/form-data, cabecera X-Api-Key)
  pdf        archivo PDF vigente de la certificación
  signature  PNG de la firma (fondo transparente)
  role       N1 | N2 | N3
  signer     nombre a imprimir bajo la firma
Devuelve el PDF estampado. Cabeceras: X-Sha256-Before, X-Sha256-After, X-Page.
"""
import hashlib
import io
import os
import shutil
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pymupdf
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response

API_KEY = os.environ.get("STAMP_API_KEY", "")
MAX_BYTES = 20 * 1024 * 1024
TZ = ZoneInfo("Europe/Madrid")

# Texto ancla de cada rol en la plantilla de certificación.
ANCHORS = {"N1": "SUPERVISOR —", "N2": "SITE MANAGER —", "N3": "PROJECT MANAGER —"}
# Geometría de la casilla respecto al ancla (puntos PDF, medida sobre
# NVS-CN-PU-CON-0147B-CERT-005): x fija, parte superior = parte superior del ancla.
BOX_X0, BOX_X1, BOX_H = 427.1, 544.1, 59.3
PAD = 3.0

app = FastAPI()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def find_box(doc: pymupdf.Document, role: str):
    anchor = ANCHORS[role]
    hits = []
    for page in doc:
        for r in page.search_for(anchor):
            hits.append((page.number, r))
    if not hits:
        raise HTTPException(422, f"No se encuentra el ancla '{anchor}' en el PDF")
    if len(hits) > 1:
        raise HTTPException(422, f"Ancla '{anchor}' duplicada ({len(hits)} veces)")
    pno, r = hits[0]
    top = r.y0 - 0.1
    return pno, pymupdf.Rect(BOX_X0, top, BOX_X1, top + BOX_H)


def box_is_empty(page: pymupdf.Page, box: pymupdf.Rect) -> bool:
    inner = box + (PAD, PAD, -PAD, -PAD)
    if page.get_text("text", clip=inner).strip():
        return False
    for img in page.get_image_info():
        if pymupdf.Rect(img["bbox"]).intersects(inner):
            return False
    for w in page.widgets() or []:
        if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE and w.rect.intersects(inner):
            return False
    return True


@app.post("/stamp")
async def stamp(
    pdf: UploadFile = File(...),
    signature: UploadFile = File(...),
    role: str = Form(...),
    signer: str = Form(...),
    x_api_key: str = Header(default=""),
):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(401, "API key no válida")
    if role not in ANCHORS:
        raise HTTPException(400, "role debe ser N1, N2 o N3")

    pdf_bytes = await pdf.read()
    sig_bytes = await signature.read()
    if not pdf_bytes.startswith(b"%PDF") or len(pdf_bytes) > MAX_BYTES:
        raise HTTPException(400, "PDF no válido o mayor de 20 MB")
    try:
        pymupdf.Pixmap(sig_bytes)
    except Exception:
        raise HTTPException(400, "La firma no es una imagen válida")

    before = sha256(pdf_bytes)
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "doc.pdf")
    try:
        with open(path, "wb") as f:
            f.write(pdf_bytes)
        doc = pymupdf.open(path)
        if doc.needs_pass:
            raise HTTPException(400, "PDF protegido con contraseña")

        pno, box = find_box(doc, role)
        page = doc[pno]
        if not box_is_empty(page, box):
            raise HTTPException(409, f"La casilla {role} ya contiene una firma")

        inner = box + (PAD, PAD, -PAD, -PAD)
        text_h = 18
        img_rect = pymupdf.Rect(inner.x0, inner.y0, inner.x1, inner.y1 - text_h)
        txt_rect = pymupdf.Rect(inner.x0, inner.y1 - text_h, inner.x1, inner.y1)
        page.insert_image(img_rect, stream=sig_bytes, keep_proportion=True)
        stamp_txt = f"{signer}\n{datetime.now(TZ):%d/%m/%Y %H:%M} · EPC Workflow"
        if page.insert_textbox(txt_rect, stamp_txt, fontsize=5.5, fontname="helv", align=1) < 0:
            raise HTTPException(422, "El nombre del firmante no cabe en la casilla")

        # Actualización incremental: los bytes originales quedan intactos.
        doc.saveIncr()
        doc.close()
        with open(path, "rb") as f:
            out = f.read()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error estampando: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not out.startswith(pdf_bytes):
        raise HTTPException(500, "El PDF resultante no conserva la revisión original")

    return Response(
        content=out,
        media_type="application/pdf",
        headers={
            "X-Sha256-Before": before,
            "X-Sha256-After": sha256(out),
            "X-Page": str(pno + 1),
        },
    )


@app.get("/health")
def health():
    return {"ok": True}
