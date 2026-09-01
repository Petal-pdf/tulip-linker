import os
import re
import csv
import json
import time
import tempfile
import difflib
import logging

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

from flask import Flask, request, send_file

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dm-linker")

app = Flask(__name__)

# =========================
# KONFIG
# =========================

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")
NON_ARTNR_RE = re.compile(r"^0\d{2}\.\d{2}\.\d{4}$")  # filtrerar bort datum typ 24.09.2026

JULA_SEARCH_URL = "https://www.jula.se/search/?q={query}"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DM-Linker/1.0)"}

MATCH_THRESHOLD = 0.55   # difflib ratio 0-1 för att räkna som verifierad
EDGE_SNAP_TOL = 20.0     # pt - hur nära sidkant en ruta ska ligga för att snäppas dit
LOOKUP_CACHE: dict[str, tuple[str | None, str | None]] = {}  # artnr -> (url, name)


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

def lookup_jula(artnr: str, session: requests.Session, retries: int = 2):
    """
    Slår upp artikelnumret via jula.se:s sök, hittar produktsidans URL
    (aldrig söksidan självt - regel 5) och läser produktnamnet från
    produktsidan för verifiering.
    """
    if artnr in LOOKUP_CACHE:
        return LOOKUP_CACHE[artnr]

    result = (None, None)

    for attempt in range(retries + 1):
        try:
            resp = session.get(JULA_SEARCH_URL.format(query=artnr), headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"[{artnr}] sökfel ({attempt + 1}/{retries + 1}): {e}")
            time.sleep(1.5)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        candidate_links = [
            a["href"] for a in soup.find_all("a", href=True)
            if f"-{artnr}/" in a["href"] or a["href"].rstrip("/").endswith(artnr)
        ]

        if candidate_links:
            url = candidate_links[0]
            if not url.startswith("http"):
                url = "https://www.jula.se" + url
            try:
                p_resp = session.get(url, headers=HEADERS, timeout=15)
                p_resp.raise_for_status()
                if artnr in p_resp.text:
                    p_soup = BeautifulSoup(p_resp.text, "html.parser")
                    title_tag = p_soup.find("h1") or p_soup.find("title")
                    name_web = title_tag.get_text(strip=True) if title_tag else None
                    result = (url, name_web)
                    break
            except requests.RequestException:
                pass

        time.sleep(1.0)

    LOOKUP_CACHE[artnr] = result
    return result


def match_score(name_pdf: str, name_web: str) -> float:
    if not name_pdf or not name_web:
        return 0.0
    return difflib.SequenceMatcher(None, name_pdf.lower(), name_web.lower()).ratio()


def resolve_candidates(candidates: list[dict]):
    """
    Slår upp och verifierar varje unikt artikelnummer en gång, mappar
    sedan tillbaka resultatet till varje ruta som refererar det numret.
    """
    resolved = []
    with requests.Session() as session:
        for c in candidates:
            url, name_web = lookup_jula(c["artnr"], session)
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

def process_pdf(input_pdf: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)

    doc = fitz.open(input_pdf)
    candidates = extract_candidates(doc)
    resolved = resolve_candidates(candidates)
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
.spinner{
    display:inline-block;
    width:16px;height:16px;
    border:3px solid #ffffff66;
    border-top-color:#ffffff;
    border-radius:50%;
    animation:spin 0.8s linear infinite;
    vertical-align:middle;
    margin-right:8px;
}
@keyframes spin{to{transform:rotate(360deg);}}
#status{margin-top:16px;font-size:0.95em;}
#status.ok{color:#1a9c4a;}
#status.error{color:#d92d2d;}
#status.working{color:#555;}
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
<p id="status" style="color:#888;font-size:0.9em;">
Resultatet laddas ner som en färdiglänkad PDF.
</p>
</div>

<script>
const form = document.getElementById("linkForm");
const btn = document.getElementById("submitBtn");
const status = document.getElementById("status");
const fileInput = document.getElementById("pdfInput");

form.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (!fileInput.files.length) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Länkar PDF...';
    status.className = "working";
    status.textContent = "Bearbetar - detta kan ta en stund beroende på hur många produkter DM:en innehåller...";

    const formData = new FormData();
    formData.append("pdf", fileInput.files[0]);

    try {
        const resp = await fetch("/link", { method: "POST", body: formData });

        if (!resp.ok) {
            const text = await resp.text();
            throw new Error(text || ("Serverfel (" + resp.status + ")"));
        }

        const blob = await resp.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "linked.pdf";
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);

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


@app.route("/link", methods=["POST"])
def link():
    if "pdf" not in request.files:
        return "Ingen PDF uppladdad", 400

    uploaded_pdf = request.files["pdf"]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            src_path = os.path.join(tmp, "input.pdf")
            uploaded_pdf.save(src_path)

            out_dir = os.path.join(tmp, "out")
            out_pdf, out_csv, out_qa, qa_report = process_pdf(src_path, out_dir)

            # CSV och QA-rapport skrivs fortfarande ut i loggen så du kan
            # se vad som hände, men skickas inte till användaren - de vill
            # bara ha PDF:en tillbaka.
            log.info(f"QA: {qa_report['antal_lankar_skapade']}/{qa_report['antal_produkter_hittade']} länkade.")

            with open(out_pdf, "rb") as f:
                data = f.read()
    except Exception as e:
        log.exception("Fel vid länkning av PDF")
        return f"Kunde inte länka PDF:en: {e}", 500

    from io import BytesIO
    return send_file(
        BytesIO(data),
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
