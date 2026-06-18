import json
import re
import tempfile
import uuid
import threading
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import fitz
import requests
from flask import Flask, request, send_file, redirect, url_for

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
JOBS_ROOT = Path(tempfile.gettempdir()) / "dm_jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

# ------------------------
# JSON SAFE
# ------------------------
def job_path(job_id):
    return JOBS_ROOT / job_id / "job.json"

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)

def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        text = path.read_text().strip()
        if not text:
            return fallback
        return json.loads(text)
    except:
        return fallback

# ------------------------
# JOB STATE
# ------------------------
def save_job(job_id, data):
    save_json(job_path(job_id), data)

def load_job(job_id):
    return load_json(job_path(job_id), {"status": "expired"})

def update_job(job_id, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)

# ------------------------
# ARTICLE FINDING
# ------------------------
def find_articles(text):
    return list(dict.fromkeys(ARTICLE_RE.findall(text or "")))

def lookup_article(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"

# ------------------------
# 🤖 AI-LITE: FIND PRODUCT RECT
# ------------------------
def find_product_rect(page, article):
    words = page.get_text("words")

    anchors = [w for w in words if w[4] == article]
    if not anchors:
        return None

    ax0, ay0, ax1, ay1 = anchors[0][:4]

    # ✅ samla alla ord som ligger nära artikeln
    cluster = []
    for w in words:
        x0, y0, x1, y1 = w[:4]

        if abs(x0 - ax0) < 220 and abs(y0 - ay0) < 160:
            cluster.append(fitz.Rect(x0, y0, x1, y1))

    if not cluster:
        return None

    rect = cluster[0]
    for r in cluster[1:]:
        rect |= r

    # ✅ EXPANDERA till hela rutan
    rect = fitz.Rect(
        rect.x0 - 80,
        rect.y0 - 80,
        rect.x1 + 80,
        rect.y1 + 80,
    )

    # clamp till sida
    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect

# ------------------------
# JOB
# ------------------------
def run_job(job_id, pdf_path):
    try:
        update_job(job_id, status="running")

        doc = fitz.open(pdf_path)
        articles = set()

        # hitta artiklar
        for page in doc:
            for block in page.get_text("blocks"):
                for a in find_articles(block[4]):
                    articles.add(a)

        # insert links
        for pi, page in enumerate(doc):
            for article in articles:
                rect = find_product_rect(page, article)
                if not rect:
                    continue

                url = lookup_article(article)

                page.insert_link({
                    "kind": fitz.LINK_URI,
                    "from": rect,
                    "uri": url,
                })

        output = Path(pdf_path).with_name("output.pdf")
        doc.save(output)
        doc.close()

        update_job(job_id, status="done", output=str(output))

    except Exception:
        update_job(job_id, status="error", error=traceback.format_exc())

# ------------------------
# ROUTES
# ------------------------
@app.route("/")
def index():
    return '''
    <form method="post" action="/link" enctype="multipart/form-data">
        <input type="file" name="pdf">
        <button>Start</button>
    </form>
    '''

@app.route("/link", methods=["POST"])
def link():
    pdf = request.files["pdf"]

    job_id = str(uuid.uuid4())
    folder = JOBS_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)

    path = folder / pdf.filename
    pdf.save(path)

    save_job(job_id, {"status": "queued"})

    threading.Thread(target=run_job, args=(job_id, path)).start()

    return redirect(f"/status/{job_id}")

@app.route("/status/<job_id>")
def status(job_id):
    return str(load_job(job_id))

@app.route("/download/<job_id>")
def download(job_id):
    job = load_job(job_id)
    if job.get("output"):
        return send_file(job["output"])
    return "not ready"

# ------------------------
if __name__ == "__main__":
    app.run()
