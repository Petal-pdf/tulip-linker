
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urljoin

import fitz
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
app.secret_key = "dm-linker-v10-1"

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

HTML_INDEX = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker V10.1</title>
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
    <h1>DM Linker V10.1</h1>
    <p class="muted">Browser-lookup med Playwright. Länkar bara riktiga produktsidor. Ingen search/fallback skapas.</p>
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
      <button type="submit">Länka PDF</button>
    </form>
  </div>
</body>
</html>
"""

HTML_RESULT = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker – resultat</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 16px; color: #222; background:#fafafa; }
    .card { background:white; border: 1px solid #ddd; border-radius: 14px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.05); }
    .ok { background:#e8f5e9; border:1px solid #81c784; padding:12px; border-radius:8px; margin:16px 0; }
    .warn { background:#fff4ce; border:1px solid #e1c542; padding:12px; border-radius:8px; margin:16px 0; }
    .button { display:inline-block; margin-top:12px; padding:12px 18px; border:0; border-radius:8px; background:#0078d4; color:white; font-weight:600; cursor:pointer; text-decoration:none; }
    .secondary { background:#666; margin-left:8px; }
    code { background:#f3f3f3; padding:2px 6px; border-radius:4px; }
    ul { columns: 3; line-height:1.55; }
  </style>
</head>
<body><div class="card">
  <h1>Resultat</h1>
  <div class="ok">PDF skapad. Länkar infogade: <strong>{{ inserted }}</strong>.<br>Unika artiklar hittade: <strong>{{ total_articles }}</strong>.<br>Artiklar utan säker produktsida: <strong>{{ missing|length }}</strong>.</div>
  {% if missing %}<div class="warn"><strong>Vissa produkter fick ingen länk.</strong><br>Orsak: appen hittade ingen säker produktsida via browser-lookup. Ingen fallback/search-länk har skapats.</div><h3>Saknade artikelnummer</h3><ul>{% for article in missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>{% endif %}
  <a class="button" href="/download/{{ file_id }}">Ladda ner PDF</a><a class="button secondary" href="/">Länka en ny PDF</a>
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


def browser_lookup_urls(country: str, articles: list[str]) -> dict:
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
        for article in articles:
            search_url = SEARCH_URLS[country].format(article=article)
            try:
                page.goto(search_url, wait_until="networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                try:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    continue
            except Exception:
                continue

            # Cookie banners - ignore if not present.
            for label in ["Godkänn", "Acceptera", "Accept", "OK", "Tillåt alla", "Hyväksy", "Akceptuję"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=800)
                    break
                except Exception:
                    pass

            try:
                page.wait_for_timeout(1400)
                hrefs = page.eval_on_selector_all("a[href*='/catalog/']", "els => els.map(a => a.href)")
            except Exception:
                hrefs = []

            for href in hrefs:
                clean = href.split("?")[0].split("#")[0]
                if is_valid_product_url(clean, country, article):
                    results[article] = clean
                    break
            if results[article]:
                continue

            # If card URL doesn't include article, open top candidates and verify body/final URL.
            for href in hrefs[:8]:
                clean = href.split("?")[0].split("#")[0]
                if not clean.startswith(base) or "/catalog/" not in clean or "search" in clean.lower():
                    continue
                product_page = None
                try:
                    product_page = context.new_page()
                    product_page.goto(clean, wait_until="networkidle", timeout=15000)
                    body_text = product_page.text_content("body", timeout=5000) or ""
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


def link_pdf(input_path: Path, country: str):
    doc = fitz.open(input_path)
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
    lookup = browser_lookup_urls(country, articles)
    inserted = 0
    missing = set()
    for page_index, block, article in blocks_info:
        url = lookup.get(article)
        if not url:
            missing.add(article)
            continue
        rect = fitz.Rect(block[:4])
        doc[page_index].insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url, "border": [0, 0, 0]})
        inserted += 1
    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / f"linked_{input_path.name}"
    doc.save(out_file)
    doc.close()
    return out_file, inserted, articles, sorted(missing)

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
    out_path, inserted, articles, missing = link_pdf(in_path, country)
    if not articles:
        flash('Inga artikelnummer hittades i PDF:ens textlager.')
        return redirect(url_for('index'))
    file_id = str(uuid.uuid4())
    OUTPUTS[file_id] = out_path
    return render_template_string(HTML_RESULT, inserted=inserted, total_articles=len(articles), missing=missing, file_id=file_id)

@app.get('/download/<file_id>')
def download(file_id):
    path = OUTPUTS.get(file_id)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=Path(path).name, mimetype='application/pdf')

@app.get('/health')
def health():
    return {'ok': True, 'version': 'v10.1-playwright-fixed'}

if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
