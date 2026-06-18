import json
import re
import uuid
import threading
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import fitz
from flask import Flask, request, send_file, redirect

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
ROOT = Path("./jobs")
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
# 🔥 BEST FUNCTION (din typ av layout)
# -------------------------
def find_product_rect(page, article):
    words = page.get_text("words")

    anchors = [w for w in words if w[4] == article]
    if not anchors:
        return None

    ax0, ay0, ax1, ay1 = anchors[0][:4]

    # samla ord nära artikeln (samma "rad/produkt")
    cluster = []
    for w in words:
        x0, y0, x1, y1 = w[:4]

        if (
            abs(y0 - ay0) < 120 and
            abs(x0 - ax0) < 400
        ):
            cluster.append(fitz.Rect(x0, y0, x1, y1))

    if not cluster:
        return None

    rect = cluster[0]
    for r in cluster[1:]:
        rect |= r

    # 🔥 expansion (detta gör hela produkten klickbar)
    rect = fitz.Rect(
        rect.x0 - 180,
        rect.y0 - 220,
        rect.x1 + 180,
        rect.y1 + 180,
    )

    # håll inom sidan
    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect

# -------------------------
# JOB ENGINE
# -------------------------
def run_job(job_id, pdf_path):
    try:
        update_job(job_id, status="running")

        doc = fitz.open(pdf_path)

        for page in doc:
            for block in page.get_text("blocks"):
                articles = find_articles(block[4])

                for article in articles:
                    rect = find_product_rect(page, article)
                    if not rect:
                        continue

                    page.insert_link({
                        "kind": fitz.LINK_URI,
                        "from": rect,
                        "uri": lookup(article)
                    })

        out = Path(pdf_path).parent / "output.pdf"
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
    <form action="/link" method="post" enctype="multipart/form-data">
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
    print("JOB:", job_id)

    threading.Thread(target=run_job, args=(job_id, path), daemon=True).start()

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
