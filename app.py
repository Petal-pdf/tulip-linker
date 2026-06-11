
import json
import re
import tempfile
import uuid
import threading
import traceback
from pathlib import Path
from urllib.parse import urljoin

import fitz
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
app.secret_key = "dm-linker-v10-4-api-capture"

# Support 6 and 7 digit article numbers, e.g. 017285 and 1061547.
ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
JOBS_ROOT = Path(tempfile.gettempdir()) / "dm_linker_jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

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

HTML_INDEX = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker V10.4</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 16px; color: #222; background:#fafafa; }
    .card { background:white; border: 1px solid #ddd; border-radius: 14px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.05); }
    h1 { margin-top: 0; }
    .muted { color:#666; font-size:14px; }
    label { display:block; margin:14px 0 6px; font-weight:600; }
    input[type=file], select { width:100%; padding:10px; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    button, .button { display:inline-block; margin-top:20px; padding:12px 18px; border:0; border-radius:8px; background:#0078d4; color:white; font-weight:600; cursor:pointer; text-decoration:none; }
    .flash { background:#fff4ce; border:1px solid #e1c542; padding:12px; border-radius:8px; margin-bottom:16px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>DM Linker V10.4</h1>
    <p class="muted">API-capture + browser fallback. Appen försöker fånga Julas produktdata från nätverksanrop och länkar bara säkra produktsidor.</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}{% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <form method="post" enctype="multipart/form-data" action="/link">
      <label>PDF</label>
      <input type="file" name="pdf" accept=".pdf" required>
      <div class="row"><div>
        <label>Land</label>
        <select name="country"><option value="SE">SE</option><option value="NO">NO</option><option value="FI">FI</option><option value="PL">PL</option></select>
      </div></div>
      <button type="submit">Starta länkning</button>
    </form>
  </div>
</body>
</html>
"""

HTML_STATUS = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  {% if job.status not in ['done','error','expired'] %}<meta http-equiv="refresh" content="4">{% endif %}
  <title>DM Linker – jobstatus</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 16px; color: #222; background:#fafafa; }
    .card { background:white; border: 1px solid #ddd; border-radius: 14px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.05); }
    .ok { background:#e8f5e9; border:1px solid #81c784; padding:12px; border-radius:8px; margin:16px 0; }
    .warn { background:#fff4ce; border:1px solid #e1c542; padding:12px; border-radius:8px; margin:16px 0; }
    .error { background:#ffebee; border:1px solid #ef9a9a; padding:12px; border-radius:8px; margin:16px 0; white-space: pre-wrap; }
    .button { display:inline-block; margin-top:12px; padding:12px 18px; border:0; border-radius:8px; background:#0078d4; color:white; font-weight:600; cursor:pointer; text-decoration:none; }
    .secondary { background:#666; margin-left:8px; }
    code { background:#f3f3f3; padding:2px 6px; border-radius:4px; }
    ul { columns: 3; line-height:1.55; }
  </style>
</head>
<body><div class="card">
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
    {% if job.missing %}
      <div class="warn"><strong>Vissa produkter fick ingen länk.</strong><br>Orsak: appen hittade ingen säker produktsida via API-capture/browser-lookup. Ingen fallback/search-länk har skapats.</div>
      <h3>Saknade artikelnummer</h3><ul>{% for article in job.missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>
    {% endif %}
    <a class="button" href="/download/{{ job_id }}">Ladda ner PDF</a><a class="button secondary" href="/">Länka en ny PDF</a>
  {% elif job.status == 'error' %}
    <div class="error">{{ job.error }}</div><a class="button secondary" href="/">Tillbaka</a>
  {% elif job.status == 'expired' %}
    <div class="warn">Jobbet finns inte längre. Kör PDF:en igen.</div><a class="button secondary" href="/">Starta om</a>
  {% else %}
    <div class="warn">Jobbet körs. Sidan uppdateras automatiskt var fjärde sekund. Lämna fliken öppen.</div>
  {% endif %}
</div></body></html>
"""

def job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def job_json_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def save_job(job_id: str, job: dict):
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    job_json_path(job_id).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def load_job(job_id: str) -> dict:
    p = job_json_path(job_id)
    if not p.exists():
        return {"status": "expired", "message": "Jobbet hittades inte.", "country": "", "pages_read": 0, "pages_zero": [], "total_articles": 0, "inserted": 0, "missing": [], "output": None, "error": ""}
    return json.loads(p.read_text(encoding="utf-8"))


def update_job(job_id: str, **kwargs):
    job = load_job(job_id)
    job.update(kwargs)
    save_job(job_id, job)


def find_articles(text: str):
    # Preserve order and remove duplicates within same text block.
    found = []
    for m in ARTICLE_RE.finditer(text or ""):
        article = m.group(0)
        if article not in found:
            found.append(article)
    return found


def is_valid_product_url(url: str, country: str, article: str) -> bool:
    base = COUNTRY_BASE[country]
    if not url:
        return False
    clean = url.split("?")[0].split("#")[0]
    return clean.startswith(base) and "/catalog/" in clean and article in clean and "search" not in clean.lower()


def normalize_candidate_url(value, country: str):
    if not isinstance(value, str):
        return None
    if "/catalog/" not in value:
        return None
    base = COUNTRY_BASE[country]
    full = urljoin(base, value)
    return full.split("?")[0].split("#")[0]


def recursive_find_product_urls(data, country: str, article: str):
    """Walk arbitrary JSON and find catalog URLs that contain the article number."""
    hits = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str):
                candidate = normalize_candidate_url(v, country)
                if candidate and is_valid_product_url(candidate, country, article):
                    hits.append(candidate)
            else:
                hits.extend(recursive_find_product_urls(v, country, article))
    elif isinstance(data, list):
        for item in data:
            hits.extend(recursive_find_product_urls(item, country, article))
    elif isinstance(data, str):
        candidate = normalize_candidate_url(data, country)
        if candidate and is_valid_product_url(candidate, country, article):
            hits.append(candidate)
    return hits


def extract_urls_from_text(text: str, country: str, article: str):
    base = COUNTRY_BASE[country]
    hits = []
    # Absolute and relative catalog URLs.
    patterns = [
        r'https?://www\.jula\.(?:se|no|fi|pl)/catalog/[^\s"\'<>]*?' + re.escape(article) + r'[^\s"\'<>]*',
        r'/catalog/[^\s"\'<>]*?' + re.escape(article) + r'[^\s"\'<>]*',
    ]
    for pattern in patterns:
        for m in re.findall(pattern, text or "", flags=re.I):
            candidate = urljoin(base, m).split("?")[0].split("#")[0]
            if is_valid_product_url(candidate, country, article):
                hits.append(candidate)
    return hits


def api_capture_lookup(country: str, article: str, page) -> str | None:
    base = COUNTRY_BASE[country]
    search_url = SEARCH_URLS[country].format(article=article)
    captured_urls = []

    def on_response(resp):
        try:
            url_l = resp.url.lower()
            content_type = (resp.headers.get("content-type") or "").lower()
            # Capture likely JSON/API/search responses.
            if "json" in content_type or "search" in url_l or "product" in url_l or "graphql" in url_l or "api" in url_l:
                try:
                    data = resp.json()
                    captured_urls.extend(recursive_find_product_urls(data, country, article))
                except Exception:
                    try:
                        txt = resp.text()
                        captured_urls.extend(extract_urls_from_text(txt, country, article))
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", on_response)
    try:
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            page.goto(search_url, wait_until="load", timeout=15000)
        page.wait_for_timeout(2200)
    except Exception:
        pass
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # First: captured API/JSON product URL.
    for u in captured_urls:
        if is_valid_product_url(u, country, article):
            return u

    # Second: rendered DOM links.
    try:
        hrefs = page.eval_on_selector_all("a[href*='/catalog/']", "els => els.map(a => a.href)")
    except Exception:
        hrefs = []
    for href in hrefs:
        clean = href.split("?")[0].split("#")[0]
        if is_valid_product_url(clean, country, article):
            return clean

    # Third: raw rendered HTML.
    try:
        html = page.content()
        for u in extract_urls_from_text(html, country, article):
            return u
    except Exception:
        pass

    # Fourth: open a few catalog candidates and verify body/final URL.
    for href in hrefs[:5]:
        clean = href.split("?")[0].split("#")[0]
        if not clean.startswith(base) or "/catalog/" not in clean or "search" in clean.lower():
            continue
        product_page = None
        try:
            product_page = page.context.new_page()
            product_page.goto(clean, wait_until="domcontentloaded", timeout=10000)
            product_page.wait_for_timeout(700)
            body_text = product_page.text_content("body", timeout=4000) or ""
            final_url = product_page.url.split("?")[0].split("#")[0]
            if article in body_text and is_valid_product_url(final_url, country, article):
                return final_url
        except Exception:
            pass
        finally:
            if product_page:
                try: product_page.close()
                except Exception: pass
    return None


def lookup_articles(country: str, articles: list[str], job_id: str) -> dict:
    results = {a: None for a in articles}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            locale="sv-SE",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        for idx, article in enumerate(articles, start=1):
            update_job(job_id, message=f"Slår upp artikel {idx}/{len(articles)}: {article}")
            results[article] = api_capture_lookup(country, article, page)
        context.close()
        browser.close()
    return results


def run_job(job_id: str, input_path: str, country: str):
    try:
        update_job(job_id, status="running", message="Läser PDF och hittar artikelnummer...")
        path = Path(input_path)
        doc = fitz.open(path)
        blocks_info = []
        articles = []
        pages_zero = []

        for page_index, page in enumerate(doc):
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
            page_articles = []
            for block in page.get_text("blocks"):
                block_articles = find_articles(block[4])
                if block_articles:
                    # First article in block becomes link anchor, consistent with DM practice.
                    anchor_article = block_articles[0]
                    blocks_info.append((page_index, block, anchor_article))
                    for a in block_articles:
                        if a not in articles:
                            articles.append(a)
                        if a not in page_articles:
                            page_articles.append(a)
            if not page_articles:
                pages_zero.append(page_index + 1)

        update_job(job_id, total_articles=len(articles), pages_read=doc.page_count, pages_zero=pages_zero)
        if not articles:
            update_job(job_id, status="done", message="Inga artikelnummer hittades.")
            return

        update_job(job_id, message="Startar API-capture/browser-lookup...")
        lookup = lookup_articles(country, articles, job_id)

        inserted = 0
        missing = set()
        update_job(job_id, message="Skriver länkar i PDF...")
        for page_index, block, article in blocks_info:
            url = lookup.get(article)
            if not url:
                missing.add(article)
                continue
            rect = fitz.Rect(block[:4])
            doc[page_index].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url, "border": [0, 0, 0]})
            inserted += 1

        out_file = job_dir(job_id) / f"linked_{path.name}"
        doc.save(out_file)
        doc.close()

        update_job(job_id, output=str(out_file), inserted=inserted, missing=sorted(missing), status="done", message="Klart.")
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

    save_job(job_id, {
        "status": "queued",
        "message": "Jobbet är köat...",
        "country": country,
        "pages_read": 0,
        "pages_zero": [],
        "total_articles": 0,
        "inserted": 0,
        "missing": [],
        "output": None,
        "error": "",
    })
    thread = threading.Thread(target=run_job, args=(job_id, str(in_path), country), daemon=True)
    thread.start()
    return redirect(url_for('status', job_id=job_id))


@app.get('/status/<job_id>')
def status(job_id):
    job = load_job(job_id)
    return render_template_string(HTML_STATUS, job=job, job_id=job_id)


@app.get('/download/<job_id>')
def download(job_id):
    job = load_job(job_id)
    output = job.get("output")
    if job.get("status") != "done" or not output:
        abort(404)
    path = Path(output)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name, mimetype='application/pdf')


@app.get('/health')
def health():
    return {'ok': True, 'version': 'v10.4-api-capture'}


if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
