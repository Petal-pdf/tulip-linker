
import io
import csv
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string

app = Flask(__name__)
app.secret_key = "dm-linker-web-v3-secret"

ARTICLE_RE = re.compile(r"\b\d{6}\b")

COUNTRY_FALLBACKS = {
    "SE": lambda article: f"https://www.jula.se/search/?query={article}",
    "NO": lambda article: f"https://www.jula.no/search/?query={article}",
    "FI": lambda article: f"https://www.jula.fi/search/?query={article}",
    "PL": lambda article: f"https://www.jula.pl/search/?query={article}",
}
COUNTRY_DOMAINS = {
    "SE": "https://www.jula.se",
    "NO": "https://www.jula.no",
    "FI": "https://www.jula.fi",
    "PL": "https://www.jula.pl",
}

HTML = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker Web v3</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 16px; color: #222; background:#fafafa; }
    .card { background:white; border: 1px solid #ddd; border-radius: 14px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.05); }
    h1 { margin-top: 0; }
    .muted { color:#666; font-size:14px; }
    label { display:block; margin:14px 0 6px; font-weight:600; }
    input[type=file], select { width:100%; padding:10px; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    button { margin-top:20px; padding:12px 18px; border:0; border-radius:8px; background:#0078d4; color:white; font-weight:600; cursor:pointer; }
    .flash { background:#fff4ce; border:1px solid #e1c542; padding:12px; border-radius:8px; margin-bottom:16px; }
    code { background:#f3f3f3; padding:2px 6px; border-radius:4px; }
    ul { line-height: 1.5; }
    .ok { background:#e8f5e9; border:1px solid #81c784; padding:12px; border-radius:8px; margin-bottom:16px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>DM Linker Web v3</h1>
    <p class="muted">Ladda upp PDF, välj land och få tillbaka en länkad PDF. För exakta produktsidor: använd CSV med <code>article,url</code>.</p>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, msg in messages %}
          <div class="{{ 'ok' if category == 'ok' else 'flash' }}">{{ msg }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <form method="post" enctype="multipart/form-data" action="/link">
      <label>PDF</label>
      <input type="file" name="pdf" accept=".pdf" required>

      <div class="row">
        <div>
          <label>Land</label>
          <select name="country">
            <option value="SE">SE</option>
            <option value="NO">NO</option>
            <option value="FI">FI</option>
            <option value="PL">PL</option>
          </select>
        </div>
        <div>
          <label>CSV med artikel → URL (valfritt)</label>
          <input type="file" name="mapping_csv" accept=".csv">
        </div>
      </div>

      <label><input type="checkbox" name="normalize_domain"> Normalisera domän i CSV till valt land</label>
      <button type="submit">Länka PDF</button>
    </form>

    <h3>Viktigt</h3>
    <ul>
      <li>Om du hostar appen statiskt (bara HTML/CSS) kommer uppladdning inte fungera. Den här appen kräver Python-backend.</li>
      <li>Utan CSV används fallback-länkar baserat på artikelnummer. Med CSV används exakt produktsida per artikelnummer.</li>
      <li>Detta följer din standard: artikelnummer = ankare och metoden ska kunna skifta mellan SE/NO/PL/FI.</li>
    </ul>
  </div>
</body>
</html>
"""


def load_mapping_from_csv(file_storage):
    if not file_storage or file_storage.filename == "":
        return {}
    raw = file_storage.read().decode("utf-8-sig", errors="ignore")
    file_storage.stream.seek(0)
    reader = csv.DictReader(io.StringIO(raw))
    mapping = {}
    for row in reader:
        article = (row.get("article") or row.get("Artikelnummer") or row.get("artikel") or "").strip()
        url = (row.get("url") or row.get("URL") or row.get("Produktlänk") or row.get("Produktlank") or "").strip()
        if article and url:
            mapping[article] = url
    return mapping


def normalize_country_mapping(mapping, country):
    out = {}
    base_domain = COUNTRY_DOMAINS[country]
    for article, url in mapping.items():
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            out[article] = base_domain + parsed.path
        else:
            out[article] = url
    return out


def first_article(text: str):
    m = ARTICLE_RE.search(text or "")
    return m.group(0) if m else None


def link_pdf(input_path: Path, country: str, mapping: dict, remove_existing: bool = True):
    doc = fitz.open(input_path)
    inserted = 0
    for page in doc:
        if remove_existing:
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
        for block in page.get_text("blocks"):
            rect = fitz.Rect(block[:4])
            text = block[4]
            article = first_article(text)
            if not article:
                continue
            url = mapping.get(article) or COUNTRY_FALLBACKS[country](article)
            page.insert_link({
                "kind": fitz.LINK_URI,
                "from": rect,
                "uri": url,
                "border": [0, 0, 0],
            })
            inserted += 1
    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / f"linked_{input_path.name}"
    doc.save(out_file)
    doc.close()
    return out_file, inserted


@app.get('/')
def index():
    return render_template_string(HTML)


@app.post('/link')
def link_route():
    pdf = request.files.get('pdf')
    csv_map = request.files.get('mapping_csv')
    country = request.form.get('country', 'SE')
    normalize_domain = bool(request.form.get('normalize_domain'))

    if not pdf or pdf.filename == '':
        flash('Ladda upp en PDF först.', 'error')
        return redirect(url_for('index'))
    if not pdf.filename.lower().endswith('.pdf'):
        flash('Filen måste vara en PDF.', 'error')
        return redirect(url_for('index'))

    temp_dir = Path(tempfile.mkdtemp())
    in_path = temp_dir / pdf.filename
    pdf.save(in_path)

    mapping = load_mapping_from_csv(csv_map)
    if normalize_domain and mapping:
        mapping = normalize_country_mapping(mapping, country)

    out_path, inserted = link_pdf(in_path, country, mapping)
    if inserted == 0:
        flash('Inga artikelnummer hittades i PDF:ens textlager.', 'error')
        return redirect(url_for('index'))

    return send_file(out_path, as_attachment=True, download_name=out_path.name, mimetype='application/pdf')


@app.get('/health')
def health():
    return {'ok': True}


if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
