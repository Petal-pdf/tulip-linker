
import re
import tempfile
import uuid
import threading
import traceback
from pathlib import Path
from urllib.parse import urljoin

import fitz  # PyMuPDF
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
app.secret_key = "dm-linker-v10-2-job-status"

ARTICLE_RE = re.compile(r"\b\d{6}\b")
OUTPUTS = {}
JOBS = {}

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
  <title>DM Linker V10.2</title>
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
    <h1>DM Linker V10.2</h1>
    <p class="muted">Jobbsida: appen startar länkningen i bakgrunden och visar status tills PDF:en är klar. Ingen search/fallback skapas i PDF:en.</p>
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
  <meta http-equiv="refresh" content="4">
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
  <p>Artiklar hittade: <strong>{{ job.total_articles }}</strong></p>
  <p>Länkar infogade: <strong>{{ job.inserted }}</strong></p>
  <p>Saknade artiklar: <strong>{{ job.missing|length }}</strong></p>

  {% if job.status == 'done' %}
    <div class="ok">PDF:en är klar.</div>
    {% if job.missing %}
      <div class="warn"><strong>Vissa produkter fick ingen länk.</strong><br>Orsak: appen hittade ingen säker produktsida via browser-lookup. Ingen fallback/search-länk har skapats.</div>
      <h3>Saknade artikelnummer</h3><ul>{% for article in job.missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>
    {% endif %}
    <a class="button" href="/download/{{ job_id }}">Ladda ner PDF</a><a class="button secondary" href="/">Länka en ny PDF</a>
  {% elif job.status == 'error' %}
    <div class="error">{{ job.error }}</div>
    <a class="button secondary" href="/">Tillbaka</a>
  {% else %}
    <div class="warn">Jobbet körs. Sidan uppdateras automatiskt var fjärde sekund. Lämna fliken öppen.</div>
  {% endif %}
</div></body></html>
"""

def first_article(text: str):
    m = ARTICLE_RE.search(text or "")
    return m.group(0) if m else None


def is_valid_product_url(url: str, country: str, article: str) -> bool:
    base = COUNTRY_BASE[country]
    if not url:
        return False
    clean = url.split("?")[0].split("#")[0]
    return clean.startswith(base) and "/catalog/" in clean and article in clean and "search" not in clean.lower()


def browser_lookup_urls(country: str, articles: list[str], job: dict) -> dict:
    base = COUNTRY_BASE[country]
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
            job["message"] = f"Slår upp artikel {idx}/{len(articles)}: {article}"
            search_url = SEARCH_URLS[country].format(article=article)
            try:
                # domcontentloaded är lättare än networkidle och minskar 502-risk.
                page.goto(search_url, wait_until="domcontentloaded", timeout=12000)
                page.wait_for_timeout(1500)
            except PlaywrightTimeoutError:
                try:
                    page.goto(search_url, wait_until="load", timeout=12000)
                    page.wait_for_timeout(1000)
                except Exception:
                    continue
            except Exception:
                continue

            for label in ["Godkänn", "Acceptera", "Accept", "OK", "Tillåt alla", "Hyväksy", "Akceptuję"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=700)
                    break
                except Exception:
                    pass

            try:
                hrefs = page.eval_on_selector_all("a[href*='/catalog/']", "els => els.map(a => a.href)")
            except Exception:
                hrefs = []

            # Strict: produkt-URL måste innehålla artikelnummer.
            for href in hrefs:
                clean = href.split("?")[0].split("#")[0]
                if is_valid_product_url(clean, country, article):
                    results[article] = clean
                    break

            if results[article]:
                continue

            # Lättare fallback: öppna bara första 3 kandidater, inte 8.
            for href in hrefs[:3]:
                clean = href.split("?")[0].split("#")[0]
                if not clean.startswith(base) or "/catalog/" not in clean or "search" in clean.lower():
                    continue
                product_page = None
                try:
                    product_page = context.new_page()
                    product_page.goto(clean, wait_until="domcontentloaded", timeout=10000)
                    product_page.wait_for_timeout(700)
                    body_text = product_page.text_content("body", timeout=4000) or ""
                    final_url = product_page.url.split("?")[0].split("#")[0]
                    if article in body_text and is_valid_product_url(final_url, country, article):
                        results[article] = final_url
                        break
                except Exception:
                    pass
                finally:
                    if product_page:
                        try: product_page.close()
                        except Exception: pass
        context.close()
        browser.close()
    return results


def run_job(job_id: str, input_path: str, country: str):
    job = JOBS[job_id]
    try:
        job["status"] = "running"
        job["message"] = "Läser PDF och hittar artikelnummer..."
        path = Path(input_path)
        doc = fitz.open(path)
        blocks_info = []
        articles = []

        for page_index, page in enumerate(doc):
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
            for block in page.get_text("blocks"):
                article = first_article(block[4])
                if article:
                    blocks_info.append((page_index, block, article))
                    if article not in articles:
                        articles.append(article)

        job["total_articles"] = len(articles)
        if not articles:
            job["status"] = "done"
            job["message"] = "Inga artikelnummer hittades."
            return

        job["message"] = "Startar browser-lookup..."
        lookup = browser_lookup_urls(country, articles, job)

        inserted = 0
        missing = set()
        job["message"] = "Skriver länkar i PDF..."
        for page_index, block, article in blocks_info:
            url = lookup.get(article)
            if not url:
                missing.add(article)
                continue
            rect = fitz.Rect(block[:4])
            doc[page_index].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url, "border": [0, 0, 0]})
            inserted += 1

        out_dir = Path(tempfile.mkdtemp())
        out_file = out_dir / f"linked_{path.name}"
        doc.save(out_file)
        doc.close()

        job["output"] = str(out_file)
        job["inserted"] = inserted
        job["missing"] = sorted(missing)
        job["status"] = "done"
        job["message"] = "Klart."
    except Exception:
        job["status"] = "error"
        job["error"] = traceback.format_exc()
        job["message"] = "Jobbet misslyckades. Se felmeddelande nedan."


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

    temp_dir = Path(tempfile.mkdtemp())
    in_path = temp_dir / pdf.filename
    pdf.save(in_path)

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "message": "Jobbet är köat...",
        "country": country,
        "total_articles": 0,
        "inserted": 0,
        "missing": [],
        "output": None,
        "error": "",
    }
    thread = threading.Thread(target=run_job, args=(job_id, str(in_path), country), daemon=True)
    thread.start()
    return redirect(url_for('status', job_id=job_id))


@app.get('/status/<job_id>')
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return render_template_string(HTML_STATUS, job=job, job_id=job_id)


@app.get('/download/<job_id>')
def download(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("output"):
        abort(404)
    path = Path(job["output"])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name, mimetype='application/pdf')


@app.get('/health')
def health():
    return {'ok': True, 'version': 'v10.2-job-status'}


if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
