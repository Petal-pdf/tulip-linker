import json
import re
import tempfile
import uuid
import threading
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import fitz
from flask import Flask, request, send_file, redirect

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
ROOT = Path(tempfile.gettempdir()) / "dm_jobs"
ROOT.mkdir(exist_ok=True)

# -------------------------
# JSON SAFE
# -------------------------
def job_file(job_id):
    return ROOT / job_id / "job.json"

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)

def load_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        t = path.read_text().strip()
        if not t:
            return fallback
        return json.loads(t)
    except:
        return fallback

def save_job(job_id, data):
    save_json(job_file(job_id), data)

def load_job(job_id):
    return load_json(job_file(job_id), {"status": "expired"})

def update_job(job_id, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)

# -------------------------
# FIND ARTICLES
# -------------------------
def find_articles(text):
    return list(set(ARTICLE_RE.findall(text or "")))

def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"

# -------------------------
# HERO MODE (stora ytor)
# -------------------------
def hero_rect(page, article):
    words = page.get_text("words")
    hits = [w for w in words if w[4] == article]
    if not hits:
        return None

    x0, y0, x1, y1 = hits[0][:4]

    return fitz.Rect(
        max(0, x0 - 400),
        max(0, y0 - 300),
        min(page.rect.width, x1 + 400),
        min(page.rect.height, y1 + 300),
    )

# -------------------------
# GRID MODE (hela rutor)
# -------------------------
def build_grid(page, cols=3, rows=4):
    w = page.rect.width
    h = page.rect.height

    cell_w = w / cols
    cell_h = h / rows

    grid = []
    for i in range(cols):
        for j in range(rows):
            grid.append(fitz.Rect(
                i * cell_w,
                j * cell_h,
                (i + 1) * cell_w,
                (j + 1) * cell_h
            ))
    return grid

def match_grid(page, article, grid):
    words = page.get_text("words")

    for w in words:
        if w[4] == article:
            px, py = w[0], w[1]
            for rect in grid:
                if rect.contains(fitz.Point(px, py)):
                    return rect
    return None

# -------------------------
# JOB ENGINE
# -------------------------
def run_job(job_id, pdf_path):
    try:
        update_job(job_id, status="running")

        doc = fitz.open(pdf_path)

        for page in doc:
            # hitta artiklar på sidan
            page_articles = []
            for block in page.get_text("blocks"):
                page_articles += find_articles(block[4])

            page_articles = list(set(page_articles))

            # välj mode automatiskt
            mode = "grid" if len(page_articles) > 4 else "hero"

            if mode == "grid":
                grid = build_grid(page)

                for article in page_articles:
                    rect = match_grid(page, article, grid)
                    if rect:
                        page.insert_link({
                            "kind": fitz.LINK_URI,
                            "from": rect,
                            "uri": lookup(article)
                        })

            else:
                for article in page_articles:
                    rect = hero_rect(page, article)
                    if rect:
                        page.insert_link({
                            "kind": fitz.LINK_URI,
                            "from": rect,
                            "uri": lookup(article)
                        })

        out = Path(pdf_path).with_name("output.pdf")
        doc.save(out)
        doc.close()

        update_job(job_id, status="done", output=str(out))

    except Exception:
        update_job(job_id, status="error", error=traceback.format_exc())

# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def index():
    return '''
    <form method="post" enctype="multipart/form-data" action="/link">
        <input type="file" name="pdf">
        <button>Start</button>
    </form>
    '''

@app.route("/link", methods=["POST"])
def link():
    pdf = request.files["pdf"]

    job_id = str(uuid.uuid4())
    folder = ROOT / job_id
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

# -------------------------
if __name__ == "__main__":
    app.run()
