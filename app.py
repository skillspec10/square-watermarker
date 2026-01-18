import os, io, tempfile, re, uuid
from fastapi import FastAPI, UploadFile, Form, Header
from fastapi.responses import JSONResponse
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader

SCOPES = ["https://www.googleapis.com/auth/drive"]

app = FastAPI()
tasks = {}

# ---------------- HELPERS ----------------

def extract_folder_id(v: str):
    m = re.search(r"folders/([a-zA-Z0-9_-]+)", v)
    return m.group(1) if m else v.strip()

def drive(access_token: str):
    return build("drive", "v3", credentials=Credentials(token=access_token, scopes=SCOPES))

# ---------------- WATERMARK ----------------

def create_watermark(logo, out):
    c = canvas.Canvas(out, pagesize=letter)
    w, h = letter
    img = ImageReader(logo)
    iw, ih = img.getSize()

    scale = 0.7
    nw, nh = iw * scale, ih * scale

    c.setFillAlpha(0.4)
    c.drawImage(
        img,
        (w - nw) / 2,
        (h - nh) / 2,
        nw,
        nh,
        mask="auto"
    )
    c.showPage()
    c.save()

def apply_wm(src, wm, dst):
    r = PdfReader(src)
    w = PdfWriter()
    wm_page = PdfReader(wm).pages[0]

    for p in r.pages:
        p.merge_page(wm_page)
        w.add_page(p)

    with open(dst, "wb") as f:
        w.write(f)

# ---------------- CORE ----------------

def process_folder(service, src_id, dst_id, wm_pdf, task_id):
    items = service.files().list(
        q=f"'{src_id}' in parents and trashed=false",
        fields="files(id,name,mimeType)"
    ).execute()["files"]

    total = len(items)
    done = 0

    for item in items:
        if item["mimeType"] == "application/vnd.google-apps.folder":
            new_dst = service.files().create(
                body={"name": item["name"], "mimeType": item["mimeType"], "parents": [dst_id]},
                fields="id"
            ).execute()["id"]

            process_folder(service, item["id"], new_dst, wm_pdf, task_id)

        elif item["mimeType"] == "application/pdf":
            req = service.files().get_media(fileId=item["id"])
            fh = io.BytesIO()
            MediaIoBaseDownload(fh, req).next_chunk()

            src = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            src.write(fh.getvalue())
            src.close()

            out = src.name.replace(".pdf", "_wm.pdf")
            apply_wm(src.name, wm_pdf, out)

            service.files().create(
                media_body=MediaFileUpload(out),
                body={"name": "wm_" + item["name"], "parents": [dst_id]}
            ).execute()

        done += 1
        tasks[task_id]["progress"] = int((done / total) * 100)

    tasks[task_id]["status"] = "completed"

# ---------------- API ----------------

@app.post("/watermark/start")
async def start(
    folder: str = Form(...),
    logo: UploadFile = Form(...),
    authorization: str = Header(...)
):
    token = authorization.replace("Bearer ", "")
    service = drive(token)

    src_id = extract_folder_id(folder)
    src_name = service.files().get(fileId=src_id, fields="name").execute()["name"]

    dst_id = service.files().create(
        body={"name": f"wm_{src_name}", "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()["id"]

    logo_tmp = tempfile.NamedTemporaryFile(delete=False)
    logo_tmp.write(await logo.read())
    logo_tmp.close()

    wm_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    create_watermark(logo_tmp.name, wm_pdf)

    task_id = str(uuid.uuid4())
    tasks[task_id] = {"progress": 0, "status": "running"}

    import threading
    threading.Thread(
        target=process_folder,
        args=(service, src_id, dst_id, wm_pdf, task_id)
    ).start()

    return {"task_id": task_id}

@app.get("/watermark/progress/{task_id}")
def progress(task_id: str):
    return tasks.get(task_id, {"error": "Invalid task"})

