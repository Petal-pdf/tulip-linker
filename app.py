import re
from pathlib import Path
from urllib.parse import quote_plus

import fitz
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
# ✅ SMART PRODUKT-RUTA (FUNKAR PÅ DIN LAYOUT)
# -------------------------
def find_product_rect(page, article):
    words = page.get_text("words")

    anchors = [w for w in words if w[4] == article]
    if not anchors:
        return None

    ax0, ay0, ax1, ay1 = anchors[0][:4]

    rects = []

    # 🔥 samla text nära artikeln (produkt område)
    for w in words:
        x0, y0, x1, y1 = w[:4]

        if (
            abs(x0 - ax0) < 250 and   # samma kolumn
            y0 > ay0 - 320 and        # upp (bild)
            y0 < ay0 + 150            # ner (text)
        ):
            rects.append(fitz.Rect(x0, y0, x1, y1))

    if not rects:
        return None

    rect = rects[0]
    for r in rects[1:]:
        rect |= r

    # 🔥 expandera till hela produktkortet
    rect = fitz.Rect(
        rect.x0 - 140,
        rect.y0 - 260,
        rect.x1 + 140,
        rect.y1 + 100,
    )

    # håll inom sidan
    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect


# -------------------------
# PROCESS
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
# ROUTES
# -------------------------
@app.route("/")
def index():
    return """
    <h2>DM Linker</h2>
    <form action="/link" method="post" enctype="multipart/form-data">
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
