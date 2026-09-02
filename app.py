import os
import re
import csv
import json
import time
import uuid
import shutil
import tempfile
import difflib
import logging
import threading

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

from flask import Flask, request, send_file, jsonify, after_this_request

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dm-linker")

app = Flask(__name__)

# =========================
# KONFIG
# =========================

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
NON_ARTNR_RE = re.compile(r"^0\d{2}\.\d{2}\.\d{4}$")  # filtrerar bort datum typ 24.09.2026

JULA_SEARCH_URL = "https://www.jula.se/search/?q={query}"

MATCH_THRESHOLD = 0.55   # difflib ratio 0-1 för att räkna som verifierad
EDGE_SNAP_TOL = 20.0     # pt - hur nära sidkant en ruta ska ligga för att snäppas dit
LOOKUP_CACHE: dict[str, tuple[str | None, str | None]] = {}  # artnr -> (url, name)

# =========================
# JOBB-STATUS (för progress-polling)
# =========================
# Body-analys är snabb, men uppslagningen mot jula.se tar en stund per
# unikt artikelnummer. Render (och de flesta proxys) stänger anslutningen
# om ett enda HTTP-anrop hänger för länge -> 502. Lösningen: starta
# bearbetningen i en bakgrundstråd direkt, svara omedelbart med ett
# job_id, och låt frontend polla /link/progress/<job_id> tills den är klar.

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


def _get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


# =========================
# STEG 1: HITTA ARTIKELNUMMER + RUTOR I PDF
# =========================

def extract_candidates(doc: fitz.Document):
    """
    Går igenom varje sida i PDF:en, hittar ord som ser ut som artikelnummer,
    och beräknar en klickyta för produktrutan baserat på:
      1. Fyllda bakgrundsrutor (vektor-rects) som omsluter ordet
      2. Bildramar som omsluter ordet
      3. Fallback: textkluster ovanför artikelnumret

    Returnerar en lista av dicts:
      {page, artnr, name_guess, rect (fitz.Rect)}
    """
    candidates = []

    for page_index, page in enumerate(doc):
        page_rect = page.rect
        words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word)

        # vektor-bakgrundsrutor (synligt fyllda rektanglar) på sidan.
        # Vi ignorerar rutor med fill_opacity 0 (osynliga hjälprutor som
        # bara används för textpositionering) - annars fastnar klickytan
        # ibland i en osynlig ruta istället för den riktiga produktbilden.
        drawings = page.get_drawings()
        fill_rects = [
            fitz.Rect(d["rect"])
            for d in drawings
            if d.get("fill") is not None and d.get("fill_opacity", 1) > 0
        ]

        # bildramar på sidan
        image_rects = [fitz.Rect(info["bbox"]) for info in page.get_image_info()]

        for w in words:
            x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
            if not ARTICLE_RE.fullmatch(text) or NON_ARTNR_RE.match(text):
                continue

            word_point = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)

            # Samla ALLA rutor (bakgrundsfärg + bilder) som omsluter
            # artikelnumret, och välj den STÖRSTA. Produktrutan är nästan
            # alltid större än små dekorativa underrutor (t.ex. en vit
            # textbakgrund inuti en produktbild), så störst-yta-heuristiken
            # träffar rätt betydligt oftare än "första träff".
            containing = [r for r in fill_rects if r.contains(word_point)]
            containing += [
                r for r in image_rects
                if fitz.Rect(r.x0 - 20, r.y0 - 20, r.x1 + 20, r.y1 + 60).contains(word_point)
            ]

            if containing:
                box = max(containing, key=lambda r: r.get_area())
            else:
                # fallback - löst kluster ovanför artikelnumret
                box = fitz.Rect(x0 - 20, y0 - 100, x0 + 250, y1 + 15)

            # snäpp ut mot sidkant om rutan redan ligger nära (regel: ingen tom marginal)
            bx0, by0, bx1, by1 = box.x0, box.y0, box.x1, box.y1
            if bx0 <= EDGE_SNAP_TOL:
                bx0 = 0
            if bx1 >= page_rect.width - EDGE_SNAP_TOL:
                bx1 = page_rect.width
            if by0 <= EDGE_SNAP_TOL:
                by0 = 0
            if by1 >= page_rect.height - EDGE_SNAP_TOL:
                by1 = page_rect.height
            box = fitz.Rect(bx0, by0, bx1, by1)

            # gissa produktnamn: textrader ovanför artikelnumret i samma kolumn
            name_words = [
                ww[4] for ww in words
                if abs(ww[0] - x0) < 250 and (y0 - 120) < ww[1] < y0
            ]
            name_guess = " ".join(name_words[-15:]).strip()

            candidates.append({
                "page": page_index,
                "artnr": text,
                "name_guess": name_guess,
                "rect": box,
            })

    log.info(f"Hittade {len(candidates)} artikelnummer-kandidater.")
    return candidates


# =========================
# STEG 2: SLÅ UPP + VERIFIERA PÅ JULA.SE
# =========================

def lookup_jula(artnr: str, page, retries: int = 1):
    """
    Slår upp artikelnumret via jula.se:s sök med en riktig (headless)
    webbläsare, eftersom jula.se renderar sökresultat med JavaScript -
    en vanlig requests.get() ser bara ett tomt HTML-skal.

    Hittar produktsidans URL (aldrig söksidan själv - regel 5) och läser
    produktnamnet från produktsidan för verifiering.

    VIKTIGT (prestanda): vi väntar INTE på "networkidle". Många sajter
    (jula.se inkluderat) har ständig bakgrundstrafik (analytics m.m.) som
    gör att "networkidle" aldrig inträffar - Playwright hänger då kvar
    till hela timeouten löper ut, VARJE sidladdning. Med 100+ produkter
    blir det evighetslångt. Istället: vänta bara på att DOM:en är laddad
    ("domcontentloaded", snabbt), och ge sedan React/Next.js en kort,
    begränsad chans att hydrera klart via ett kort networkidle-försök
    som vi avbryter tidigt om det inte händer.
    """
    if artnr in LOOKUP_CACHE:
        return LOOKUP_CACHE[artnr]

    result = (None, None)

    def _settle():
        # Ge sidan max ~2s extra för att bli klar - annars kör vidare ändå.
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass

    for attempt in range(retries + 1):
        try:
            page.goto(JULA_SEARCH_URL.format(query=artnr), wait_until="domcontentloaded", timeout=10000)
            _settle()
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except Exception as e:
            log.warning(f"[{artnr}] söksidan misslyckades ({attempt + 1}/{retries + 1}): {e}")
            continue

        candidate_links = [
            h for h in hrefs
            if f"-{artnr}/" in h or h.rstrip("/").endswith(artnr)
        ]

        if not candidate_links:
            # Genuint inget resultat - inget värt att gå i loop om igen.
            break

        url = candidate_links[0]
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=10000)
            _settle()
            content = page.content()
            if artnr in content:
                h1 = page.locator("h1").first
                name_web = h1.inner_text().strip() if h1.count() else None
                result = (url, name_web)
        except Exception as e:
            log.warning(f"[{artnr}] kunde inte läsa produktsidan: {e}")

        break  # antingen lyckades det, eller så var det ett riktigt fel - försök inte igen

    LOOKUP_CACHE[artnr] = result
    return result


def match_score(name_pdf: str, name_web: str) -> float:
    if not name_pdf or not name_web:
        return 0.0
    return difflib.SequenceMatcher(None, name_pdf.lower(), name_web.lower()).ratio()


def resolve_candidates(candidates: list[dict], page, progress_cb=None):
    """
    Slår upp och verifierar varje UNIKT artikelnummer en gång (cache:as
    inom denna körning), mappar sedan tillbaka resultatet till varje ruta
    som refererar det numret. progress_cb(done, total) anropas efter varje
    unikt uppslag, så anroparen kan visa procent.
    """
    unique_artnrs = list(dict.fromkeys(c["artnr"] for c in candidates))
    total = len(unique_artnrs)
    per_artnr: dict[str, tuple[str | None, str | None]] = {}

    for i, artnr in enumerate(unique_artnrs, start=1):
        per_artnr[artnr] = lookup_jula(artnr, page)
        if progress_cb:
            progress_cb(i, total)

    resolved = []
    for c in candidates:
        url, name_web = per_artnr[c["artnr"]]
        score = match_score(c["name_guess"], name_web) if name_web else 0.0
        verified = bool(url and name_web and score >= MATCH_THRESHOLD)

        note = ""
        if not url:
            note = "Ingen exakt produktsida kunde hittas."
        elif not verified:
            note = f"Namn matchar inte tillräckligt bra ({score:.0%}) - granska manuellt."

        resolved.append({
            **c,
            "url": url,
            "name_web": name_web,
            "match_score": round(score, 2),
            "verified": verified,
            "note": note,
        })
    return resolved


# =========================
# STEG 3: LÄNKA PDF:EN
# =========================

def apply_links(doc: fitz.Document, resolved: list[dict]):
    linked = 0
    for r in resolved:
        if not r["verified"]:
            continue
        page = doc[r["page"]]
        page.insert_link({
            "kind": fitz.LINK_URI,
            "from": r["rect"],
            "uri": r["url"],
        })
        linked += 1
        log.info(f"Länkade {r['artnr']} -> {r['url']}")
    return linked


# =========================
# STEG 4: CSV + QA-RAPPORT
# =========================

def write_csv(resolved: list[dict], out_path: str):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Artikelnummer", "Produktnamn", "URL", "Sida"])
        for r in resolved:
            if r["verified"]:
                w.writerow([r["artnr"], r["name_web"] or r["name_guess"], r["url"], r["page"] + 1])


def write_qa_report(resolved: list[dict], out_path: str):
    report = {
        "antal_produkter_hittade": len(resolved),
        "antal_lankar_skapade": sum(1 for r in resolved if r["verified"]),
        "osakra_matchningar": [
            {
                "artikelnummer": r["artnr"],
                "sida": r["page"] + 1,
                "namn_i_pdf": r["name_guess"],
                "namn_pa_webben": r["name_web"],
                "url_kandidat": r["url"],
                "anledning": r["note"],
            }
            for r in resolved if not r["verified"]
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


# =========================
# ORKESTRERING
# =========================

def process_pdf(input_pdf: str, out_dir: str, page, progress_cb=None):
    os.makedirs(out_dir, exist_ok=True)

    doc = fitz.open(input_pdf)
    candidates = extract_candidates(doc)
    resolved = resolve_candidates(candidates, page, progress_cb=progress_cb)
    linked_count = apply_links(doc, resolved)

    out_pdf = os.path.join(out_dir, "linked.pdf")
    out_csv = os.path.join(out_dir, "links.csv")
    out_qa = os.path.join(out_dir, "qa_report.json")

    doc.save(out_pdf, garbage=4, deflate=True)
    doc.close()

    write_csv(resolved, out_csv)
    qa_report = write_qa_report(resolved, out_qa)

    log.info(f"Klart: {linked_count} länkar skapade av {len(resolved)} kandidater.")
    return out_pdf, out_csv, out_qa, qa_report


def run_job(job_id: str, input_pdf: str, out_dir: str):
    """
    Körs i en bakgrundstråd. Öppnar en egen Playwright/Chromium-instans
    för det här jobbet (Playwrights sync-API delas inte mellan trådar),
    kör hela uppslagningen genom den, och stänger ner allt igen efteråt.
    Uppdaterar JOBS med löpande status/procent.
    """
    try:
        _set_job(job_id, status="extracting", progress=0, progress_text="Läser PDF...")

        def on_progress(done, total):
            pct = int(done / total * 100) if total else 100
            _set_job(job_id, status="looking_up", progress=pct,
                      progress_text=f"Slår upp produkt {done}/{total} på jula.se...")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            browser_page = browser.new_page()
            try:
                out_pdf, out_csv, out_qa, qa_report = process_pdf(
                    input_pdf, out_dir, browser_page, progress_cb=on_progress
                )
            finally:
                browser.close()

        _set_job(
            job_id,
            status="done",
            progress=100,
            progress_text="Klart!",
            pdf_path=out_pdf,
            qa_report=qa_report,
        )
    except Exception as e:
        log.exception(f"Jobb {job_id} misslyckades")
        _set_job(job_id, status="error", error=str(e))


# =========================
# HTML
# =========================

HTML_PAGE = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>DM Linker</title>
<style>
body{font-family:Arial,sans-serif;background:#f5dce8;padding:40px;}
.card{max-width:700px;margin:auto;background:white;padding:40px;border-radius:20px;}
button{background:#ff4fa3;color:white;border:none;padding:15px 25px;border-radius:10px;cursor:pointer;font-size:1em;}
button:disabled{background:#f5a9cf;cursor:not-allowed;}
#status{margin-top:16px;font-size:0.95em;}
#status.ok{color:#1a9c4a;}
#status.error{color:#d92d2d;}
#status.working{color:#555;}
.progress-wrap{
    margin-top:16px;
    background:#f2f2f2;
    border-radius:10px;
    overflow:hidden;
    height:22px;
    display:none;
}
.progress-bar{
    height:100%;
    width:0%;
    background:#ff4fa3;
    transition:width 0.3s ease;
    display:flex;
    align-items:center;
    justify-content:center;
    color:white;
    font-size:0.8em;
    font-weight:bold;
}
</style>
</head>
<body>
<div class="card">
<h1>DM Linker</h1>
<form id="linkForm">
<input type="file" id="pdfInput" name="pdf" accept=".pdf" required>
<br><br>
<button type="submit" id="submitBtn">Starta länkning</button>
</form>

<div class="progress-wrap" id="progressWrap">
  <div class="progress-bar" id="progressBar">0%</div>
</div>

<p id="status" style="color:#888;font-size:0.9em;">
Resultatet laddas ner som en färdiglänkad PDF.
</p>
</div>

<script>
const form = document.getElementById("linkForm");
const btn = document.getElementById("submitBtn");
const status = document.getElementById("status");
const fileInput = document.getElementById("pdfInput");
const progressWrap = document.getElementById("progressWrap");
const progressBar = document.getElementById("progressBar");

function setProgress(pct, text) {
    progressWrap.style.display = "block";
    progressBar.style.width = pct + "%";
    progressBar.textContent = pct + "%";
    if (text) {
        status.className = "working";
        status.textContent = text;
    }
}

async function pollProgress(jobId) {
    while (true) {
        const resp = await fetch(`/link/progress/${jobId}`);
        if (!resp.ok) throw new Error("Kunde inte hämta status (" + resp.status + ")");
        const data = await resp.json();

        if (data.status === "error") {
            throw new Error(data.error || "Okänt fel under bearbetning");
        }

        setProgress(data.progress || 0, data.progress_text || "Bearbetar...");

        if (data.status === "done") {
            return;
        }

        await new Promise(r => setTimeout(r, 1000));
    }
}

form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) return;

    btn.disabled = true;
    btn.textContent = "Länkar PDF...";
    setProgress(0, "Laddar upp och analyserar PDF...");

    const formData = new FormData();
    formData.append("pdf", fileInput.files[0]);

    try {
        const startResp = await fetch("/link/start", { method: "POST", body: formData });
        if (!startResp.ok) {
            const text = await startResp.text();
            throw new Error(text || ("Serverfel (" + startResp.status + ")"));
        }
        const { job_id } = await startResp.json();

        await pollProgress(job_id);

        // ladda ner PDF:en
        window.location = `/link/download/${job_id}`;

        status.className = "ok";
        status.textContent = "Klart! Den länkade PDF:en har laddats ner.";
    } catch (err) {
        status.className = "error";
        status.textContent = "Något gick fel: " + err.message;
    } finally {
        btn.disabled = false;
        btn.textContent = "Starta länkning";
    }
});
</script>
</body>
</html>
"""


# =========================
# ROUTES
# =========================

@app.route("/")
def index():
    return HTML_PAGE


@app.route("/health")
def health():
    return "OK"


@app.route("/link/start", methods=["POST"])
def link_start():
    if "pdf" not in request.files:
        return "Ingen PDF uppladdad", 400

    uploaded_pdf = request.files["pdf"]

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(tempfile.gettempdir(), "dm-linker", job_id)
    os.makedirs(job_dir, exist_ok=True)

    src_path = os.path.join(job_dir, "input.pdf")
    uploaded_pdf.save(src_path)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "progress_text": "I kö...",
            "job_dir": job_dir,
        }

    out_dir = os.path.join(job_dir, "out")
    thread = threading.Thread(target=run_job, args=(job_id, src_path, out_dir), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/link/progress/<job_id>")
def link_progress(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Okänt job_id"}), 404

    return jsonify({
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "progress_text": job.get("progress_text", ""),
        "error": job.get("error"),
    })


@app.route("/link/download/<job_id>")
def link_download(job_id):
    job = _get_job(job_id)
    if not job:
        return "Okänt job_id", 404
    if job.get("status") != "done" or not job.get("pdf_path"):
        return "Jobbet är inte klart än", 409

    pdf_path = job["pdf_path"]

    @after_this_request
    def _cleanup(response):
        job_dir = job.get("job_dir")
        if job_dir and os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
        return response

    return send_file(
        pdf_path,
        as_attachment=True,
        download_name="linked.pdf",
        mimetype="application/pdf",
    )


# =========================
# START
# =========================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        debug=True,
    )

