import json
import re
import tempfile
import uuid
import threading
import traceback
import html as html_lib
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import fitz
import requests
from flask import Flask, request, send_file, redirect, url_for

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
JOBS_ROOT = Path(tempfile.gettempdir()) / "dm_linker_jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

COUNTRY_BASE = {
    "SE": "https://www.jula.se",
}

def job_dir(job_id):
    return JOBS_ROOT / job_id

def job_json_path(job_id):
    return job_dir(job_id) / "job.json"

# ✅ SAFE SAVE
def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(path)

# ✅ SAFE LOAD
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

def save_job(job_id, job):
    save_json(job_json_path(job_id), job)

def load_job(job_id):
    return load_json(job_json_path(job_id), {"status": "expired"})

def update_job(job_id, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)

def find_articles(text):
    return list(dict.fromkeys(ARTICLE_RE.findall(text or "")))

def clean_url(url):
    return html_lib.unescape(url).split("?")[0].strip()

def lookup_article(article):
    return f"https://www.jula.se/search/?query={article}"

# ✅ MAGIC: MERGE BLOCKS PER ARTIKEL
def merge_blocks(blocks):
    rects = [fitz.Rect(b[:4]) for b in blocks]
    combined = rects[0]
    for r in rects[1:]:
        combined |= r

    # Expand lite för att täcka hela rutan
    return fitz.Rect(
        combined.x0 - 20,
        combined.y0 - 20,
        combined.x1 + 20,
        combined.y1 + 20,
    )

def run_job(job_id, input_path):
    try:
        update_job(job_id, status="running")

        doc = fitz.open(input_path)

        articles = []
        blocks_map = {}  # (page, article) -> blocks

        for pi, page in enumerate(doc):
            for block in page.get_text("blocks"):
                found = find_articles(block[4])
                if found:
                    article = found[0]

                    key = (pi, article)
                    blocks_map.setdefault(key, []).append(block)

                    if article not in articles:
                        articles.append(article)

        for (pi, article), blocks in blocks_map.items():
            rect = merge_blocks(blocks)
            url = lookup_article(article)

            doc[pi].insert_link({
                "kind": fitz.LINK_URI,
                "from": rect,
                "uri": url,
            })

        out = job_dir(job_id) / "output.pdf"
        doc.save(out)
        doc.close()

        update_job(job_id, status="done", output=str(out))

    except Exception:
        update_job(job_id, status="error", error=traceback.format_exc())

# ROUTES

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
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)

    path = d / pdf.filename
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

if __name__ == "__main__":
    app.run()
``
