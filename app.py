import os, io, json, tempfile, asyncio, requests
from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials
from dotenv import load_dotenv

from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/drive"]

app = FastAPI()
tasks = {}
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# -------------------------
# GOOGLE AUTH
# -------------------------
def exchange_code_for_token(code: str) -> Credentials:
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    r = requests.post(GOOGLE_TOKEN_URI, data=data)
    r.raise_for_status()
    token = r.json()
    return Credentials(
        token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GOOGLE_SCOPES,
    )

def get_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)

def get_folder_name(service, folder_id):
    return service.files().get(fileId=folder_id, fields="name").execute()["name"]

def list_files(service, folder_id):
    q = f"'{folder_id}' in parents and trashed=false"
    files = []
    res = service.files().list(q=q, fields="files(id,name,mimeType,parents)").execute()
    for f in res.get("files", []):
        files.append(f)
        if f["mimeType"] == "application/vnd.google-apps.folder":
            files.extend(list_files(service, f["id"]))
    return files

# -------------------------
# WATERMARK
# -------------------------
def create_watermark(logo_path, out_pdf):
    c = canvas.Canvas(out_pdf, pagesize=letter)
    w, h = letter
    img = ImageReader(logo_path)
    iw, ih = img.getSize()
    c.setFillAlpha(0.3)
    c.drawImage(img, (w - iw) / 2, (h - ih) / 2, iw * 0.9, ih * 0.9, mask="auto")
    c.showPage()
    c.save()

def apply_wm(src, wm, dst):
    reader = PdfReader(src)
    writer = PdfWriter()
    watermark_page = PdfReader(wm).pages[0]
    for page in reader.pages:
        page.merge_page(watermark_page)
        writer.add_page(page)
    with open(dst, "wb") as f:
        writer.write(f)

# -------------------------
# PROCESS TASK
# -------------------------
async def process_task(task_id):
    task = tasks[task_id]
    service = task["service"]
    files = task["files"]
    wm_pdf = task["wm_pdf"]

    for i, f in enumerate(files):
        while task["paused"]:
            await asyncio.sleep(1)
        if task["cancelled"]:
            task["status"] = "cancelled"
            return
        if f["mimeType"] == "application/pdf":
            request = service.files().get_media(fileId=f["id"])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as inp:
                inp.write(fh.getvalue())
                outp = inp.name.replace(".pdf", "_wm.pdf")
            apply_wm(inp.name, wm_pdf, outp)
            media = MediaFileUpload(outp, mimetype="application/pdf")
            service.files().create(
                media_body=media,
                body={
                    "name": "watermarked_" + f["name"],
                    "parents": [task["folder_map"][f["parents"][0]]],
                },
            ).execute()
            os.remove(inp.name)
            os.remove(outp)
        task["progress"] = int((i + 1) / len(files) * 100)
    task["status"] = "completed"

# -------------------------
# ROUTES
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}&redirect_uri={GOOGLE_REDIRECT_URI}&response_type=code&scope={' '.join(GOOGLE_SCOPES)}&access_type=offline&prompt=consent"
    return templates.TemplateResponse("index.html", {"request": request, "oauth_url": oauth_url})

@app.post("/start")
async def start(source_folder_id: str = Form(...), code: str = Form(...), logo: UploadFile = Form(...)):
    creds = exchange_code_for_token(code)
    service = get_drive_service(creds)
    src_name = get_folder_name(service, source_folder_id)
    dest = service.files().create(
        body={"name": f"{src_name} (Watermarked)", "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    files = list_files(service, source_folder_id)
    folder_map = {source_folder_id: dest["id"]}
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            parent = folder_map[f["parents"][0]]
            new = service.files().create(
                body={"name": f["name"], "mimeType": f["mimeType"], "parents": [parent]}, fields="id"
            ).execute()
            folder_map[f["id"]] = new["id"]
    logo_file = tempfile.NamedTemporaryFile(delete=False)
    logo_file.write(await logo.read())
    logo_file.close()
    wm_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
    create_watermark(logo_file.name, wm_pdf)
    task_id = str(len(tasks) + 1)
    tasks[task_id] = {"service": service, "files": files, "folder_map": folder_map, "wm_pdf": wm_pdf, "progress": 0, "paused": False, "cancelled": False, "status": "running"}
    asyncio.create_task(process_task(task_id))
    return {"task_id": task_id}
