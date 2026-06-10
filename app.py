
import csv
import re
import tempfile
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, request, send_file, flash, redirect, url_for, render_template_string, abort

app = Flask(__name__)
app.secret_key = "dm-linker-web-v5-1-secret"

ARTICLE_RE = re.compile(r"\b\d{6}\b")
OUTPUTS = {}
MASTER_MAPPING_FILE = Path(__file__).parent / "data" / "master_mapping.csv"

HTML_INDEX = """
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker</title>
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
    <h1>DM Linker</h1>
    <p class="muted">Ladda upp PDF, välj land och få tillbaka en länkad PDF.</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
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
      </div>
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
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DM Linker – resultat</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 16px; color: #222; background:#fafafa; }
    .card { background:white; border: 1px solid #ddd; border-radius: 14px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.05); }
    h1 { margin-top: 0; }
    .ok { background:#e8f5e9; border:1px solid #81c784; padding:12px; border-radius:8px; margin:16px 0; }
    .warn { background:#fff4ce; border:1px solid #e1c542; padding:12px; border-radius:8px; margin:16px 0; }
    .button { display:inline-block; margin-top:12px; padding:12px 18px; border:0; border-radius:8px; background:#0078d4; color:white; font-weight:600; cursor:pointer; text-decoration:none; }
    .secondary { background:#666; margin-left:8px; }
    code { background:#f3f3f3; padding:2px 6px; border-radius:4px; }
    ul { columns: 3; line-height:1.55; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Resultat</h1>
    <div class="ok">
      PDF skapad. Länkar infogade: <strong>{{ inserted }}</strong>.<br>
      Hittade artiklar: <strong>{{ total_articles }}</strong>.<br>
      Artiklar utan URL i master-mapping: <strong>{{ missing|length }}</strong>.
    </div>
    {% if missing %}
      <div class="warn">
        <strong>Vissa produkter fick ingen länk.</strong><br>
        Orsak: artikelnumret saknas i <code>data/master_mapping.csv</code> för valt land. Ingen fallback/search-länk har skapats.
      </div>
      <h3>Saknade artikelnummer</h3>
      <ul>{% for article in missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>
    {% endif %}
    <a class="button" href="/download/{{ file_id }}">Ladda ner PDF</a>
    <a class="button secondary" href="/">Länka en ny PDF</a>
  </div>
</body>
</html>
"""

def load_master_mapping():
    mapping = {}
    if not MASTER_MAPPING_FILE.exists():
        return mapping
    raw = MASTER_MAPPING_FILE.read_text(encoding="utf-8-sig", errors="ignore")
    reader = csv.DictReader(raw.splitlines())
    for row in reader:
        country = (row.get("country") or row.get("Land") or "").strip().upper()
        article = (row.get("article") or row.get("Artikelnummer") or row.get("artikel") or "").strip()
        url = (row.get("url") or row.get("URL") or row.get("Produktlänk") or row.get("Produktlank") or "").strip()
        if country and article and url:
            mapping[(country, article)] = url
    return mapping


def first_article(text: str):
    m = ARTICLE_RE.search(text or "")
    return m.group(0) if m else None


def link_pdf(input_path: Path, country: str):
    master = load_master_mapping()
    doc = fitz.open(input_path)
    inserted = 0
    found_articles = []
    missing = set()

    for page in doc:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)

        for block in page.get_text("blocks"):
            rect = fitz.Rect(block[:4])
            text = block[4]
            article = first_article(text)
            if not article:
                continue

            found_articles.append(article)
            url = master.get((country, article))

            # Ingen fallback. Om riktig URL saknas skapas ingen länk alls.
            if not url:
                missing.add(article)
                continue

            page.insert_link({"kind": fitz.LINK_URI, "from": rect, "uri": url, "border": [0, 0, 0]})
            inserted += 1

    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / f"linked_{input_path.name}"
    doc.save(out_file)
    doc.close()
    return out_file, inserted, found_articles, sorted(missing)


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

    out_path, inserted, found_articles, missing = link_pdf(in_path, country)
    if not found_articles:
        flash('Inga artikelnummer hittades i PDF:ens textlager.')
        return redirect(url_for('index'))

    file_id = str(uuid.uuid4())
    OUTPUTS[file_id] = out_path
    return render_template_string(HTML_RESULT, inserted=inserted, total_articles=len(found_articles), missing=missing, file_id=file_id)


@app.get('/download/<file_id>')
def download(file_id):
    path = OUTPUTS.get(file_id)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=Path(path).name, mimetype='application/pdf')


@app.get('/health')
def health():
    return {'ok': True}


if __name__ == '__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False)
