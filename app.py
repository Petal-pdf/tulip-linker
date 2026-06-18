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
# Hitta artiklar
# -------------------------
def find_articles(text):
    return list(set(ARTICLE_RE.findall(text or "")))


def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"


# -------------------------
# PDF → bild
# -------------------------
def page_to_image(page):
    pix = page.get_pixmap()
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    img = img.reshape(pix.height, pix.width, pix.n)
    return img


# -------------------------
# AI: hitta boxar
# -------------------------
def detect_boxes(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    edges = cv2.Canny(gray, 50, 150)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)

        # filtrera små saker
        if w > 200 and h > 200:
            boxes.append((x, y, w, h))

    return boxes


# -------------------------
# Match artikel → box
# -------------------------
def match_articles(page, boxes):
    words = page.get_text("words")
    results = {}

    for w in words:
        text = w[4]

        if re.fullmatch(r"\d{6,7}", text):
            ax, ay = w[0], w[1]

            best = None
            best_dist = 999999

            for (x, y, bw, bh) in boxes:
                cx = x + bw / 2
                cy = y + bh / 2

                dist = abs(ax - cx) + abs(ay - cy)

                if dist < best_dist:
                    best_dist = dist
                    best = (x, y, bw, bh)

            if best:
                results[text] = best

    return results


# -------------------------
# PROCESS
# -------------------------
def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)

    for page in doc:
        img = page_to_image(page)
        boxes = detect_boxes(img)

        article_boxes = match_articles(page, boxes)

        for article, (x, y, w, h) in article_boxes.items():

            rect = fitz.Rect(x, y, x + w, y + h)

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
    return """
    <h2>DM Linker AI</h2>
    <form method="post" action="/link" enctype="multipart/form-data">
        <input type="file" name="pdf" required>
        <button type="submit">Start</button>
    </form>
    """


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
