
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz
import requests
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort

app = Flask(__name__)
app.secret_key = "dm-linker-v9"

ARTICLE_RE = re.compile(r"\b\d{6}\b")
OUTPUTS = {}

COUNTRY_BASE = {
    "SE": "https://www.jula.se",
    "NO": "https://www.jula.no",
    "FI": "https://www.jula.fi",
    "PL": "https://www.jula.pl",
}

SEARCH_URLS = {
    "SE": "https://www.jula.se/search/?query={article}",
    "NO": "https://www.jula.no/search/?query={article}",
    "FI": "https://www.jula.fi/search/?query={article}",
    "PL": "https://www.jula.pl/search/?query={article}",
}

HTML = """
<!doctype html>
<html><body>
<h2>DM Linker v9</h2>
<form method=post enctype=multipart/form-data action="/link">
PDF: <input type=file name=pdf><br>
Land:
<select name=country>
<option>SE</option><option>NO</option><option>FI</option><option>PL</option>
</select>
<br><button type=submit>Länka PDF</button>
</form>
</body></html>
"""


def extract_article(text):
    m = ARTICLE_RE.search(text or "")
    return m.group(0) if m else None


def fast_lookup(country, article):
    base = COUNTRY_BASE[country]
    url = SEARCH_URLS[country].format(article=article)

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        links = re.findall(r'href="([^"]*?/catalog/[^"]+)"', r.text, re.I)

        for link in links[:5]:  # top 5 candidates
            full = urljoin(base, link).split("?")[0]
            if "search" in full: continue

            try:
                pr = requests.get(full, timeout=6)
                if article in pr.text:
                    return full
            except:
                continue
    except:
        return None

    return None


def resolve_bulk(country, articles):
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fast_lookup, country, a): a for a in articles}

        for f in as_completed(futures):
            art = futures[f]
            try:
                results[art] = f.result()
            except:
                results[art] = None

    return results


def link_pdf(path, country):
    doc = fitz.open(path)

    articles = set()
    blocks_info = []

    for page_index, page in enumerate(doc):
        for block in page.get_text("blocks"):
            art = extract_article(block[4])
            if art:
                articles.add(art)
                blocks_info.append((page_index, block, art))

    lookup = resolve_bulk(country, list(articles))

    inserted = 0
    missing = []

    for page_index, block, art in blocks_info:
        page = doc[page_index]
        rect = fitz.Rect(block[:4])

        url = lookup.get(art)

        if not url:
            missing.append(art)
            continue

        page.insert_link({
            "kind": fitz.LINK_URI,
            "from": rect,
            "uri": url
        })
        inserted += 1

    out = Path(tempfile.mkdtemp()) / f"linked_{path.name}"
    doc.save(out)
    return out, inserted, len(articles), missing


@app.route("/")
def index():
    return HTML


@app.route("/link", methods=["POST"])
def link():
    file = request.files.get("pdf")
    country = request.form.get("country", "SE")

    if not file:
        flash("No file")
        return redirect("/")

    tmp = Path(tempfile.mkdtemp()) / file.filename
    file.save(tmp)

    out, inserted, total, missing = link_pdf(tmp, country)

    fid = str(uuid.uuid4())
    OUTPUTS[fid] = out

    return f"Inserted: {inserted}/{total}<br>Missing: {len(missing)}<br><a href='/download/{fid}'>Download</a>"


@app.route("/download/<fid>")
def download(fid):
    return send_file(OUTPUTS[fid], as_attachment=True)


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
