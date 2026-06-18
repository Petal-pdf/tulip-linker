
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

# 6 + 7 digit articles, e.g. 017285 and 1061547
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

SEARCH_URLS = {
    "SE": "https://www.jula.se/search/?query={article}",
    "NO": "https://www.jula.no/search/?query={article}",
    "FI": "https://www.jula.fi/search/?query={article}",
    "PL": "https://www.jula.pl/search/?query={article}",
}

# Extra lightweight attempts. These are only used to FIND a product URL.
# They are never written to the PDF as fallback links.
ALT_SEARCH_URLS = {
    "SE": [
        "https://www.jula.se/search/?query={article}",
        "https://www.jula.se/search/?q={article}",
        "https://www.jula.se/search/{article}/",
    ],
    "NO": [
        "https://www.jula.no/search/?query={article}",
        "https://www.jula.no/search/?q={article}",
        "https://www.jula.no/search/{article}/",
    ],
    "FI": [
        "https://www.jula.fi/search/?query={article}",
        "https://www.jula.fi/search/?q={article}",
        "https://www.jula.fi/search/{article}/",
    ],
    "PL": [
        "https://www.jula.pl/search/?query={article}",
        "https://www.jula.pl/search/?q={article}",
        "https://www.jula.pl/search/{article}/",
    ],
}

HTML_INDEX = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DM Linker V11</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;max-width:920px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}
.card{background:white;border:1px solid #ddd;border-radius:14px;padding:24px;box-shadow:0 2px 10px rgba(0,0,0,.05)}
h1{margin-top:0}.muted{color:#666;font-size:14px}label{display:block;margin:14px 0 6px;font-weight:600}
input[type=file],select{width:100%;padding:10px}.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
button,.button{display:inline-block;margin-top:20px;padding:12px 18px;border:0;border-radius:8px;background:#0078d4;color:white;font-weight:600;cursor:pointer;text-decoration:none}
.flash{background:#fff4ce;border:1px solid #e1c542;padding:12px;border-radius:8px;margin-bottom:16px}
</style></head>
<body><div class="card">
<h1>DM Linker V11</h1>
<p class="muted">Lightweight lookup utan Playwright/Chromium. Låg minnesförbrukning för Render Free. Länkar bara säkra produktsidor — aldrig search/fallback.</p>
{% with messages = get_flashed_messages() %}{% if messages %}{% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}{% endif %}{% endwith %}
<form method="post" enctype="multipart/form-data" action="/link">
<label>PDF</label><input type="file" name="pdf" accept=".pdf" required>
<div class="row"><div><label>Land</label><select name="country"><option value="SE">SE</option><option value="NO">NO</option><option value="FI">FI</option><option value="PL">PL</option></select></div></div>
<button type="submit">Starta länkning</button>
</form></div></body></html>
"""

HTML_STATUS = """
<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{% if job.status not in ['done','error','expired'] %}<meta http-equiv="refresh" content="3">{% endif %}
<title>DM Linker – jobstatus</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;max-width:920px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}
.card{background:white;border:1px solid #ddd;border-radius:14px;padding:24px;box-shadow:0 2px 10px rgba(0,0,0,.05)}
.ok{background:#e8f5e9;border:1px solid #81c784;padding:12px;border-radius:8px;margin:16px 0}.warn{background:#fff4ce;border:1px solid #e1c542;padding:12px;border-radius:8px;margin:16px 0}.error{background:#ffebee;border:1px solid #ef9a9a;padding:12px;border-radius:8px;margin:16px 0;white-space:pre-wrap}
.button{display:inline-block;margin-top:12px;padding:12px 18px;border:0;border-radius:8px;background:#0078d4;color:white;font-weight:600;cursor:pointer;text-decoration:none}.secondary{background:#666;margin-left:8px}
code{background:#f3f3f3;padding:2px 6px;border-radius:4px}ul{columns:3;line-height:1.55}
</style></head><body><div class="card">
<h1>Jobstatus</h1>
<p>Status: <strong>{{ job.status }}</strong></p>
<p>{{ job.message }}</p>
<p>Land: <strong>{{ job.country }}</strong></p>
<p>Sidor lästa: <strong>{{ job.pages_read }}</strong></p>
<p>Artiklar hittade: <strong>{{ job.total_articles }}</strong></p>
<p>Länkar infogade: <strong>{{ job.inserted }}</strong></p>
<p>Saknade artiklar: <strong>{{ job.missing|length }}</strong></p>
{% if job.pages_zero %}<div class="warn">Sidor utan text/artikelnummer: {{ job.pages_zero }}</div>{% endif %}
{% if job.status == 'done' %}
<div class="ok">PDF:en är klar.</div>
{% if job.missing %}<div class="warn"><strong>Vissa produkter fick ingen länk.</strong><br>Orsak: appen hittade ingen säker produktsida via lightweight lookup. Ingen fallback/search-länk har skapats.</div><h3>Saknade artikelnummer</h3><ul>{% for article in job.missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>{% endif %}
<a class="button" href="/download/{{ job_id }}">Ladda ner PDF</a><a class="button secondary" href="/">Länka en ny PDF</a>
{% elif job.status == 'error' %}<div class="error">{{ job.error }}</div><a class="button secondary" href="/">Tillbaka</a>
{% elif job.status == 'expired' %}<div class="warn">Jobbet finns inte längre. Kör PDF:en igen.</div><a class="button secondary" href="/">Starta om</a>
{% else %}<div class="warn">Jobbet körs. Sidan uppdateras automatiskt var tredje sekund.</div>{% endif %}
</div></body></html>
"""

def job_dir(job_id):
    return JOBS_ROOT / job_id

def job_json_path(job_id):
    return job_dir(job_id) / "job.json"

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def load_json(path, fallback):
    if not path.exists():
        return fallback

    try:
        text = path.read_text(encoding="utf-8").strip()

        if not text:
            return fallback  # filen är tom → använd fallback

        return json.loads(text)

    except json.JSONDecodeError:
        return fallback

    except Exception:
        return fallback

def save_job(job_id, job):
    save_json(job_json_path(job_id), job)

def load_job(job_id):
    return load_json(job_json_path(job_id), {"status":"expired","message":"Jobbet hittades inte.","country":"","pages_read":0,"pages_zero":[],"total_articles":0,"inserted":0,"missing":[],"output":None,"error":""})

def update_job(job_id, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)

def load_cache():
    return load_json(CACHE_FILE, {})

def save_cache(cache):
    save_json(CACHE_FILE, cache)

def find_articles(text):
    found=[]
    for m in ARTICLE_RE.finditer(text or ""):
        a=m.group(0)
        if a not in found:
            found.append(a)
    return found

def clean_url(url):
    return html_lib.unescape(url).split("?")[0].split("#")[0].rstrip("/") + "/"

def is_valid_product_url(url, country, article):
    if not url:
        return False
    base = COUNTRY_BASE[country]
    c = clean_url(url)
    return c.startswith(base) and "/catalog/" in c and article in c and "search" not in c.lower()

def candidate_urls_from_text(text, country, article):
    base = COUNTRY_BASE[country]
    hits = []
    if not text:
        return hits
    text = html_lib.unescape(text)
    patterns = [
        r'https?://www\.jula\.(?:se|no|fi|pl)/catalog/[^\s"\'<>\\]*?' + re.escape(article) + r'[^\s"\'<>\\]*',
        r'/catalog/[^\s"\'<>\\]*?' + re.escape(article) + r'[^\s"\'<>\\]*',
        r'"url"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
        r'"href"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, text, flags=re.I):
            try:
                raw = bytes(raw, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            u = urljoin(base, raw)
            if is_valid_product_url(u, country, article):
                cu = clean_url(u)
                if cu not in hits:
                    hits.append(cu)
    return hits

def walk_json_for_urls(data, country, article):
    hits=[]
    if isinstance(data, dict):
        for v in data.values():
            hits.extend(walk_json_for_urls(v, country, article))
    elif isinstance(data, list):
        for item in data:
            hits.extend(walk_json_for_urls(item, country, article))
    elif isinstance(data, str):
        hits.extend(candidate_urls_from_text(data, country, article))
    return hits

def fetch(session, url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    }
    try:
        return session.get(url, headers=headers, timeout=10, allow_redirects=True)
    except Exception:
        return None

def lookup_article(country, article, session, cache):
    key = f"{country}_{article}"
    cached = cache.get(key)
    if cached and is_valid_product_url(cached, country, article):
        return cached

    # 1) Search URLs: parse final redirect, HTML, embedded JSON, catalog links.
    for template in ALT_SEARCH_URLS[country]:
        url = template.format(article=quote_plus(article))
        r = fetch(session, url)
        if not r:
            continue
        final = clean_url(r.url)
        if is_valid_product_url(final, country, article):
            cache[key] = final
            return final

        ct = (r.headers.get("content-type") or "").lower()
        if "json" in ct:
            try:
                for u in walk_json_for_urls(r.json(), country, article):
                    cache[key] = u
                    return u
            except Exception:
                pass
        text = r.text or ""
        # HTML / __NEXT_DATA__ / state JSON / hrefs / canonical etc.
        for u in candidate_urls_from_text(text, country, article):
            cache[key] = u
            return u

    # 2) Very light sitemap/product-page discovery fallback: sometimes search HTML embeds escaped links.
    # No search URL is ever written to PDF. If this fails, no link.
    return None

def lookup_all(country, articles, job_id):
    cache = load_cache()
    session = requests.Session()
    results = {}
    for idx, article in enumerate(articles, start=1):
        update_job(job_id, message=f"Slår upp artikel {idx}/{len(articles)}: {article}")
        results[article] = lookup_article(country, article, session, cache)
        if idx % 10 == 0:
            save_cache(cache)
    save_cache(cache)
    return results

def run_job(job_id, input_path, country):
    try:
        update_job(job_id, status="running", message="Läser PDF och hittar artikelnummer...")
        path = Path(input_path)
        doc = fitz.open(path)
        blocks_info=[]; articles=[]; pages_zero=[]
        for pi, page in enumerate(doc):
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
            page_articles=[]
            for block in page.get_text("blocks"):
                bas = find_articles(block[4])
                if bas:
                    blocks_info.append((pi, block, bas[0]))  # first article per product block is anchor
                    for a in bas:
                        if a not in articles:
                            articles.append(a)
                        if a not in page_articles:
                            page_articles.append(a)
            if not page_articles:
                pages_zero.append(pi+1)
        update_job(job_id, total_articles=len(articles), pages_read=doc.page_count, pages_zero=pages_zero)
        if not articles:
            update_job(job_id, status="done", message="Inga artikelnummer hittades.")
            return
        lookup = lookup_all(country, articles, job_id)
        inserted=0; missing=set()
        update_job(job_id, message="Skriver länkar i PDF...")
        for pi, block, article in blocks_info:
            url = lookup.get(article)
            if not url:
                missing.add(article)
                continue
            doc[pi].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(block[:4]), "uri": url, "border": [0,0,0]})
            inserted += 1
        out = job_dir(job_id) / f"linked_{path.name}"
        doc.save(out)
        doc.close()
        update_job(job_id, output=str(out), inserted=inserted, missing=sorted(missing), status="done", message="Klart.")
    except Exception:
        update_job(job_id, status="error", error=traceback.format_exc(), message="Jobbet misslyckades. Se felmeddelande nedan.")

@app.get('/')
def index():
    return render_template_string(HTML_INDEX)

@app.post('/link')
def link_route():
    pdf = request.files.get('pdf')
    country = request.form.get('country', 'SE').upper()
    if not pdf or pdf.filename == '':
        flash('Ladda upp en PDF först.')
        return redirect(url_for('index'))
    if not pdf.filename.lower().endswith('.pdf'):
        flash('Filen måste vara en PDF.')
        return redirect(url_for('index'))
    job_id = str(uuid.uuid4())
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    in_path = d / pdf.filename
    pdf.save(in_path)
    save_job(job_id, {"status":"queued","message":"Jobbet är köat...","country":country,"pages_read":0,"pages_zero":[],"total_articles":0,"inserted":0,"missing":[],"output":None,"error":""})
    threading.Thread(target=run_job, args=(job_id, str(in_path), country), daemon=True).start()
    return redirect(url_for('status', job_id=job_id))

@app.get('/status/<job_id>')
def status(job_id):
    return render_template_string(HTML_STATUS, job=load_job(job_id), job_id=job_id)

@app.get('/download/<job_id>')
def download(job_id):
    job=load_job(job_id); output=job.get('output')
    if job.get('status') != 'done' or not output:
        abort(404)
    path = Path(output)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name, mimetype='application/pdf')

@app.get('/health')
def health():
    return {'ok': True, 'version': 'v11-lightweight-no-playwright'}

if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
