import re
from pathlib import Path
from urllib.parse import quote_plus
import re
from pathlib import Path
from urllib.parse import quote_plus

import fitz  # PyMuPDF
import cv2
import numpy as np
from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")

# -------------------------
# HJÄLP: hitta artiklar
# -------------------------
def find_articles(text):
    return list(set(ARTICLE_RE.findall(text or "")))

def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"

# -------------------------
# KONVERTERA PDF-SIDA -> BILD
# -------------------------
def page_to_image(page):
    pix = page.get_pixmap()
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    img = img.reshape(pix.height, pix.width, pix.n)
    return img

# -------------------------
# 🤖 AI: hitta produktboxar via OpenCV
# -------------------------
def detect_product_boxes(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # edges
    edges = cv2.Canny(gray, 50, 150)

    # hitta contours
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)

        # filtrera små grejer (text, badges)
        if w > 150 and h > 150:
            boxes.append((x, y, w, h))

    return boxes

# -------------------------
# matcha artikel -> närmaste box
# -------------------------
def match_article_to_box(page, boxes):
    words = page.get_text("words")
    matches = {}

    for w in words:
        txt = w[4]

        if re.fullmatch(r"\d{6,7}", txt):
            ax, ay = w[0], w[1]

            best = None
            best_dist = 999999

            for (x, y, bw, bh) in boxes:
                bx = x + bw / 2
                by = y + bh / 2

                dist = abs(ax - bx) + abs(ay - by)

                if dist < best_dist:
                    best_dist = dist
                    best = (x, y, bw, bh)

            if best:
                matches[txt] = best

    return matches

# -------------------------
# PROCESS
# -------------------------
def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)

    for page in doc:
        img = page_to_image(page)
        boxes = detect_product_boxes(img)

        article_boxes = match_article_to_box(page, boxes)

        for article, (x, y, w, h) in article_boxes.items():

            rect = fitz.Rect(
                x,
                y,
                x + w,
                y + h
            )

            page.insert_link({
                "kind": fitz.LINK_URI,
                "from": rect,
                "uri": lookup(article)
            })

    doc.save(output_path)
    doc.close()

# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def index():
    return '''
    <form action="/link" method="post" enctype="multipart/form-data">
        <input type="file" name="pdf">
        <button>Start</button>
    </form>
    '''

@app.route("/link", methods=["POST"])
def link():
    pdf = request.files["pdf"]

    input_path = Path("input.pdf")
    output_path = Path("output.pdf")

    pdf.save(input_path)

    process_pdf(input_path, output_path)

    return send_file(output_path, as_attachment=True)

# -------------------------
if __name__ == "__main__":
    app.run()
import fitz
from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")

# -------------------------
# FIND ARTICLES
# -------------------------
def find_articles(text):
    return list(set(ARTICLE_RE.findall(text or "")))

def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"

# -------------------------
# PRODUCT RECT (STORA RUTOR)
# -------------------------
def find_product_rect(page, article):
    words = page.get_text("words")

    anchors = [w for w in words if w[4] == article]
    if not anchors:
        return None

    ax0, ay0, ax1, ay1 = anchors[0][:4]

    cluster = []
    for w in words:
        x0, y0, x1, y1 = w[:4]

        if (
            abs(y0 - ay0) < 120 and
            abs(x0 - ax0) < 400
        ):
            cluster.append(fitz.Rect(x0, y0, x1, y1))

    if not cluster:
        return None

    rect = cluster[0]
    for r in cluster[1:]:
        rect |= r

    # 🔥 gör rutan stor
    rect = fitz.Rect(
        rect.x0 - 180,
        rect.y0 - 220,
        rect.x1 + 180,
        rect.y1 + 180,
    )

    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect

# -------------------------
# PDF PROCESS
# -------------------------
def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)

    for page in doc:
        for block in page.get_text("blocks"):
            articles = find_articles(block[4])

            for article in articles:
                rect = find_product_rect(page, article)
                if not rect:
                    continue

                page.insert_link({
                    "kind": fitz.LINK_URI,
                    "from": rect,
                    "uri": lookup(article)
                })

    doc.save(output_path)
    doc.close()

# -------------------------
# ROUTES (NO THREADS)
# -------------------------
@app.route("/")
def index():
    return '''
    <form action="/link" method="post" enctype="multipart/form-data">
        <input type="file" name="pdf">
        <button>Start</button>
    </form>
    '''

@app.route("/link", methods=["POST"])
def link():
    pdf = request.files["pdf"]

    input_path = Path("input.pdf")
    output_path = Path("output.pdf")

    pdf.save(input_path)

    # ✅ KÖR DIREKT (INGA THREADS)
    process_pdf(input_path, output_path)

    return send_file(output_path, as_attachment=True)

# -------------------------
if __name__ == "__main__":
    app.run()
