import json
import re
import tempfile
import uuid
import threading
import traceback
import html as html_lib
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import fitz  # PyMuPDF
import requests
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort

app = Flask(__name__)
app.secret_key = "dm-linker-v11-lightweight"

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
JOBS_ROOT = Path(tempfile.gettempdir()) / "dm_linker_jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)
CACHE_FILE = JOBS_ROOT / "url_cache.json"

COUNTRY_BASE = {
    "SE": "https://www.jula.se",
    "NO": "https://www.jula.no",
    "FI": "https://www.jula.fi",
    "PL": "https://www.jula.pl",
}

ALT_SEARCH_URLS = {
    "SE": ["https://www.jula.se/search/?query={article}"],
    "NO": ["https://www.jula.no/search/?query={article}"],
    "FI": ["https://www.jula.fi/search/?query={article}"],
    "PL": ["https://www.jula.pl/search/?query={article}"],
}

def job_dir(job_id):
    return JOBS_ROOT / job_id

def job_json_path(job_id):
    return job_dir(job_id) / "job.json"

# ✅ FIX 1: SAFE SAVE (atomic write)
def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

# ✅ FIX 2: SAFE LOAD (no crash)
def load_json(path, fallback):
    if not path.exists():
        return fallback

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return fallback
        return json.loads(text)
    except Exception:
        return fallback

def save_job(job_id, job):
    save_json(job_json_path(job_id), job)

# ✅ EXTRA SKYDD
def load_job(job_id):
    return load_json(
        job_json_path(job_id),
        {
            "status": "expired",
            "message": "Jobbet hittades inte.",
            "country": "",
            "pages_read": 0,
            "pages_zero": [],
            "total_articles": 0,
            "inserted": 0,
            "missing": [],
            "output": None,
            "error": "",
        },
    )

def update_job(job_id, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)

def load_cache():
    return load_json(CACHE_FILE, {})

def save_cache(cache):
    save_json(CACHE_FILE, cache)

def find_articles(text):
    return list(dict.fromkeys(ARTICLE_RE.findall(text or "")))

def clean_url(url):
    return html_lib.unescape(url).split("?")[0].split("#")[0].rstrip("/") + "/"

def is_valid_product_url(url, country, article):
    if not url:
        return False
    base = COUNTRY_BASE[country]
    c = clean_url(url)
    return c.startswith(base) and "/catalog/" in c and article in c

def fetch(session, url):
    try:
        return session.get(url, timeout=10)
    except:
        return None

def lookup_article(country, article, session, cache):
    key = f"{country}_{article}"
    if key in cache:
        return cache[key]

    for template in ALT_SEARCH_URLS[country]:
        url = template.format(article=quote_plus(article))
        r = fetch(session, url)
        if not r:
            continue
        final = clean_url(r.url)
        if is_valid_product_url(final, country, article):
            cache[key] = final
            return final

    return None

def lookup_all(country, articles, job_id):
    cache = load_cache()
    session = requests.Session()
    results = {}

    for i, a in enumerate(articles):
        update_job(job_id, message=f"Slår upp {a}")
        results[a] = lookup_article(country, a, session, cache)

    save_cache(cache)
    return results

def run_job(job_id, input_path, country):
    try:
        update_job(job_id, status="running", message="Läser PDF...")

        doc = fitz.open(input_path)
        articles = []
        blocks_info = []

        for pi, page in enumerate(doc):
            for block in page.get_text("blocks"):
                found = find_articles(block[4])
                if found:
                    a = found[0]
                    blocks_info.append((pi, block, a))
                    for x in found:
                        if x not in articles:
                            articles.append(x)

        update_job(job_id, total_articles=len(articles))

        lookup = lookup_all(country, articles, job_id)

        inserted = 0
        missing = set()

        for pi, block, article in blocks_info:
            url = lookup.get(article)
            if not url:
                missing.add(article)
                continue

            doc[pi].insert_link({
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(block[:4]),
                "uri": url,
            })
            inserted += 1

        out = job_dir(job_id) / "output.pdf"
        doc.save(out)
        doc.close()

        update_job(
            job_id,
            status="done",
            output=str(out),
            inserted=inserted,
            missing=list(missing),
            message="Klart"
        )

    except Exception:
        update_job(
            job_id,
            status="error",
            error=traceback.format_exc(),
            message="CRASH"
        )

@app.route("/")
def index():
    return '''
    <form method="post" enctype="multipart/form-data" action="/link">
    <input type="file" name="pdf">
    <select name="country">
        <option>SE</option>
        <option>NO</option>
    </select>
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

    save_job(job_id, {"status": "queued", "message": "Start..."})

    threading.Thread(target=run_job, args=(job_id, path, "SE")).start()

    return redirect(f"/status/{job_id}")

@app.route("/status/<job_id>")
def status(job_id):
    job = load_job(job_id)
    return f"<pre>{job}</pre>"

@app.route("/download/<job_id>")
def download(job_id):
    job = load_job(job_id)
    if job.get("output"):
        return send_file(job["output"])
    return "Not ready"

if __name__ == "__main__":
    app.run()
