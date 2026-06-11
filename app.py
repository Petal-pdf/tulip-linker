
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
app.secret_key = "dm-linker-v10-5-low-memory"

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
<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DM Linker V10.5</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:920px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}.card{background:white;border:1px solid #ddd;border-radius:14px;padding:24px;box-shadow:0 2px 10px rgba(0,0,0,.05)}h1{margin-top:0}.muted{color:#666;font-size:14px}label{display:block;margin:14px 0 6px;font-weight:600}input[type=file],select{width:100%;padding:10px}.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}button,.button{display:inline-block;margin-top:20px;padding:12px 18px;border:0;border-radius:8px;background:#0078d4;color:white;font-weight:600;cursor:pointer;text-decoration:none}.flash{background:#fff4ce;border:1px solid #e1c542;padding:12px;border-radius:8px;margin-bottom:16px}</style>
</head><body><div class="card"><h1>DM Linker V10.5</h1><p class="muted">Low-memory browser lookup för Render Free. Appen blockerar bilder/fonts/CSS för att inte få out of memory.</p>
{% with messages = get_flashed_messages() %}{% if messages %}{% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}{% endif %}{% endwith %}
<form method="post" enctype="multipart/form-data" action="/link"><label>PDF</label><input type="file" name="pdf" accept=".pdf" required><div class="row"><div><label>Land</label><select name="country"><option value="SE">SE</option><option value="NO">NO</option><option value="FI">FI</option><option value="PL">PL</option></select></div></div><button type="submit">Starta länkning</button></form></div></body></html>
"""

HTML_STATUS = """
<!doctype html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{% if job.status not in ['done','error','expired'] %}<meta http-equiv="refresh" content="4">{% endif %}<title>DM Linker – jobstatus</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:920px;margin:40px auto;padding:0 16px;color:#222;background:#fafafa}.card{background:white;border:1px solid #ddd;border-radius:14px;padding:24px;box-shadow:0 2px 10px rgba(0,0,0,.05)}.ok{background:#e8f5e9;border:1px solid #81c784;padding:12px;border-radius:8px;margin:16px 0}.warn{background:#fff4ce;border:1px solid #e1c542;padding:12px;border-radius:8px;margin:16px 0}.error{background:#ffebee;border:1px solid #ef9a9a;padding:12px;border-radius:8px;margin:16px 0;white-space:pre-wrap}.button{display:inline-block;margin-top:12px;padding:12px 18px;border:0;border-radius:8px;background:#0078d4;color:white;font-weight:600;cursor:pointer;text-decoration:none}.secondary{background:#666;margin-left:8px}code{background:#f3f3f3;padding:2px 6px;border-radius:4px}ul{columns:3;line-height:1.55}</style>
</head><body><div class="card"><h1>Jobstatus</h1><p>Status: <strong>{{ job.status }}</strong></p><p>{{ job.message }}</p><p>Land: <strong>{{ job.country }}</strong></p><p>Sidor lästa: <strong>{{ job.pages_read }}</strong></p><p>Artiklar hittade: <strong>{{ job.total_articles }}</strong></p><p>Länkar infogade: <strong>{{ job.inserted }}</strong></p><p>Saknade artiklar: <strong>{{ job.missing|length }}</strong></p>{% if job.pages_zero %}<div class="warn">Sidor utan text/artikelnummer: {{ job.pages_zero }}</div>{% endif %}
{% if job.status == 'done' %}<div class="ok">PDF:en är klar.</div>{% if job.missing %}<div class="warn"><strong>Vissa produkter fick ingen länk.</strong><br>Orsak: appen hittade ingen säker produktsida. Ingen fallback/search-länk har skapats.</div><h3>Saknade artikelnummer</h3><ul>{% for article in job.missing %}<li><code>{{ article }}</code></li>{% endfor %}</ul>{% endif %}<a class="button" href="/download/{{ job_id }}">Ladda ner PDF</a><a class="button secondary" href="/">Länka en ny PDF</a>{% elif job.status == 'error' %}<div class="error">{{ job.error }}</div><a class="button secondary" href="/">Tillbaka</a>{% elif job.status == 'expired' %}<div class="warn">Jobbet finns inte längre. Kör PDF:en igen.</div><a class="button secondary" href="/">Starta om</a>{% else %}<div class="warn">Jobbet körs. Sidan uppdateras automatiskt var fjärde sekund.</div>{% endif %}</div></body></html>
"""

def job_dir(job_id): return JOBS_ROOT / job_id
def job_json_path(job_id): return job_dir(job_id) / "job.json"
def save_job(job_id, job):
    d = job_dir(job_id); d.mkdir(parents=True, exist_ok=True)
    job_json_path(job_id).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
def load_job(job_id):
    p = job_json_path(job_id)
    if not p.exists():
        return {"status":"expired","message":"Jobbet hittades inte.","country":"","pages_read":0,"pages_zero":[],"total_articles":0,"inserted":0,"missing":[],"output":None,"error":""}
    return json.loads(p.read_text(encoding="utf-8"))
def update_job(job_id, **kwargs):
    job = load_job(job_id); job.update(kwargs); save_job(job_id, job)

def find_articles(text):
    found=[]
    for m in ARTICLE_RE.finditer(text or ""):
        a=m.group(0)
        if a not in found: found.append(a)
    return found

def is_valid_product_url(url, country, article):
    base = COUNTRY_BASE[country]
    if not url: return False
    clean = url.split("?")[0].split("#")[0]
    return clean.startswith(base) and "/catalog/" in clean and article in clean and "search" not in clean.lower()

def extract_urls(text, country, article):
    base=COUNTRY_BASE[country]; hits=[]
    for pattern in [r'https?://www\.jula\.(?:se|no|fi|pl)/catalog/[^\s"\'<>]*?' + re.escape(article) + r'[^\s"\'<>]*', r'/catalog/[^\s"\'<>]*?' + re.escape(article) + r'[^\s"\'<>]*']:
        for m in re.findall(pattern, text or "", flags=re.I):
            u=urljoin(base,m).split("?")[0].split("#")[0]
            if is_valid_product_url(u,country,article): hits.append(u)
    return hits

def walk_json(data, country, article):
    hits=[]
    if isinstance(data, dict):
        for v in data.values(): hits.extend(walk_json(v,country,article))
    elif isinstance(data, list):
        for x in data: hits.extend(walk_json(x,country,article))
    elif isinstance(data, str):
        hits.extend(extract_urls(data,country,article))
    return hits

def lookup_one(country, article, page, job_id, idx, total):
    update_job(job_id, message=f"Slår upp artikel {idx}/{total}: {article}")
    captured=[]
    search_url=SEARCH_URLS[country].format(article=article)
    def on_response(resp):
        try:
            ct=(resp.headers.get("content-type") or "").lower(); u=resp.url.lower()
            if "json" in ct or "search" in u or "product" in u or "api" in u or "graphql" in u:
                try: captured.extend(walk_json(resp.json(), country, article))
                except Exception:
                    try: captured.extend(extract_urls(resp.text(), country, article))
                    except Exception: pass
        except Exception: pass
    page.on("response", on_response)
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(1400)
    except Exception:
        pass
    try: page.remove_listener("response", on_response)
    except Exception: pass
    for u in captured:
        if is_valid_product_url(u,country,article): return u
    try:
        hrefs=page.eval_on_selector_all("a[href*='/catalog/']", "els => els.map(a => a.href)")
    except Exception:
        hrefs=[]
    for href in hrefs:
        clean=href.split("?")[0].split("#")[0]
        if is_valid_product_url(clean,country,article): return clean
    try:
        html=page.content()
        for u in extract_urls(html,country,article): return u
    except Exception: pass
    return None

def lookup_articles(country, articles, job_id):
    results={a:None for a in articles}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--disable-default-apps", "--disable-sync",
            "--disable-translate", "--mute-audio", "--no-first-run", "--disable-features=site-per-process"
        ])
        context=browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", locale="sv-SE", viewport={"width":1280,"height":800})
        # Block heavy resources to avoid Render Free out-of-memory.
        def route_filter(route):
            rtype = route.request.resource_type
            if rtype in ["image", "media", "font", "stylesheet"]:
                return route.abort()
            return route.continue_()
        context.route("**/*", route_filter)
        page=context.new_page()
        for idx,a in enumerate(articles, start=1):
            results[a]=lookup_one(country,a,page,job_id,idx,len(articles))
        context.close(); browser.close()
    return results

def run_job(job_id, input_path, country):
    try:
        update_job(job_id,status="running",message="Läser PDF och hittar artikelnummer...")
        path=Path(input_path); doc=fitz.open(path)
        blocks_info=[]; articles=[]; pages_zero=[]
        for pi,page in enumerate(doc):
            for annot in list(page.annots() or []): page.delete_annot(annot)
            page_articles=[]
            for block in page.get_text("blocks"):
                bas=find_articles(block[4])
                if bas:
                    blocks_info.append((pi,block,bas[0]))
                    for a in bas:
                        if a not in articles: articles.append(a)
                        if a not in page_articles: page_articles.append(a)
            if not page_articles: pages_zero.append(pi+1)
        update_job(job_id,total_articles=len(articles),pages_read=doc.page_count,pages_zero=pages_zero)
        if not articles:
            update_job(job_id,status="done",message="Inga artikelnummer hittades."); return
        lookup=lookup_articles(country,articles,job_id)
        inserted=0; missing=set(); update_job(job_id,message="Skriver länkar i PDF...")
        for pi,block,article in blocks_info:
            url=lookup.get(article)
            if not url: missing.add(article); continue
            doc[pi].insert_link({"kind":fitz.LINK_URI,"from":fitz.Rect(block[:4]),"uri":url,"border":[0,0,0]})
            inserted+=1
        out=job_dir(job_id)/f"linked_{path.name}"; doc.save(out); doc.close()
        update_job(job_id,output=str(out),inserted=inserted,missing=sorted(missing),status="done",message="Klart.")
    except Exception:
        update_job(job_id,status="error",error=traceback.format_exc(),message="Jobbet misslyckades. Se felmeddelande nedan.")

@app.get('/')
def index(): return render_template_string(HTML_INDEX)
@app.post('/link')
def link_route():
    pdf=request.files.get('pdf'); country=request.form.get('country','SE').upper()
    if not pdf or pdf.filename=='': flash('Ladda upp en PDF först.'); return redirect(url_for('index'))
    if not pdf.filename.lower().endswith('.pdf'): flash('Filen måste vara en PDF.'); return redirect(url_for('index'))
    job_id=str(uuid.uuid4()); d=job_dir(job_id); d.mkdir(parents=True,exist_ok=True)
    in_path=d/pdf.filename; pdf.save(in_path)
    save_job(job_id,{"status":"queued","message":"Jobbet är köat...","country":country,"pages_read":0,"pages_zero":[],"total_articles":0,"inserted":0,"missing":[],"output":None,"error":""})
    threading.Thread(target=run_job,args=(job_id,str(in_path),country),daemon=True).start()
    return redirect(url_for('status',job_id=job_id))
@app.get('/status/<job_id>')
def status(job_id): return render_template_string(HTML_STATUS,job=load_job(job_id),job_id=job_id)
@app.get('/download/<job_id>')
def download(job_id):
    job=load_job(job_id); output=job.get('output')
    if job.get('status')!='done' or not output: abort(404)
    path=Path(output)
    if not path.exists(): abort(404)
    return send_file(path,as_attachment=True,download_name=path.name,mimetype='application/pdf')
@app.get('/health')
def health(): return {'ok':True,'version':'v10.5-low-memory'}
if __name__=='__main__':
    import os
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',8000)), debug=False)
