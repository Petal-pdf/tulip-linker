
import re
import json
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

import fitz
import requests
from flask import Flask, request, send_file, redirect

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6}\b")
OUTPUTS = {}
CACHE_PATH = Path("data/cache.json")

COUNTRY_BASE = {
    "SE": "https://www.jula.se",
    "NO": "https://www.jula.no",
    "FI": "https://www.jula.fi",
    "PL": "https://www.jula.pl",
}

SEARCH_URL = {
    "SE": "https://www.jula.se/search/?query={}",
    "NO": "https://www.jula.no/search/?query={}",
    "FI": "https://www.jula.fi/search/?query={}",
    "PL": "https://www.jula.pl/search/?query={}",
}

# ---------- CACHE ----------
def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(c):
    CACHE_PATH.write_text(json.dumps(c, indent=2))


# ---------- LOOKUP ----------
def lookup(country, article, cache):
    key = f"{country}_{article}"

    if key in cache:
        return cache[key]

    try:
        r = requests.get(SEARCH_URL[country].format(article), timeout=8)
        matches = re.findall(r'href="(/catalog/[^"]+)"', r.text)

        for m in matches:
            url = urljoin(COUNTRY_BASE[country], m)
            if article in url and "search" not in url:
                cache[key] = url
                return url

    except:
        pass

    return None


# ---------- PDF ----------
def process(pdf, country):
    doc = fitz.open(pdf)
    cache = load_cache()

    articles = []
    blocks = []

    for i, page in enumerate(doc):
        for b in page.get_text("blocks"):
            text = b[4]
            m = ARTICLE_RE.search(text or "")
            if m:
                article = m.group(0)
                articles.append(article)
                blocks.append((i, b, article))

    def worker(a):
        return a, lookup(country, a, cache)

    results = dict(ThreadPoolExecutor(max_workers=6).map(worker, set(articles)))

    inserted = 0
    missing = []

    for i, b, art in blocks:
        rect = fitz.Rect(b[:4])
        url = results.get(art)

        if not url:
            missing.append(art)
            continue

        doc[i].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url})
        inserted += 1

    out = Path(tempfile.mkdtemp()) / f"linked_{Path(pdf).name}"
    doc.save(out)
    save_cache(cache)

    return out, inserted, len(set(articles)), missing


# ---------- ROUTES ----------
@app.route("/")
def index():
    return "<form method=post enctype=multipart/form-data action='/link'>PDF:<input type=file name=pdf><select name=country><option>SE</option><option>NO</option><option>FI</option><option>PL</option></select><button>Länk</button></form>"


@app.route("/link", methods=["POST"])
def link():
    f = request.files["pdf"]
    country = request.form.get("country", "SE")

    temp = Path(tempfile.mkdtemp()) / f.filename
    f.save(temp)

    out, ins, total, miss = process(temp, country)

    fid = str(uuid.uuid4())
    OUTPUTS[fid] = out

    return f"Inserted {ins}/{total}<br>Missing {len(miss)}<br><a href='/download/{fid}'>Download</a>"


@app.route("/download/<fid>")
def download(fid):
    return send_file(OUTPUTS[fid], as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
