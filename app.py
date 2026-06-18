import re
from pathlib import Path
from urllib.parse import quote_plus

import fitz
from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")


# -------------------------
# hitta artiklar
# -------------------------
def find_articles(text):
    return list(set(ARTICLE_RE.findall(text or "")))


def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"


# -------------------------
# smart produkt-ruta
# -------------------------
def find_product_rect(page, article):
    words = page.get_text("words")

    anchors = [w for w in words if w[4] == article]
    if not anchors:
        return None

    ax0, ay0, ax1, ay1 = anchors[0][:4]

    rects = []

    for w in words:
        x0, y0, x1, y1 = w[:4]

        if (
            abs(x0 - ax0) < 250 and
            y0 > ay0 - 320 and
            y0 < ay0 + 150
        ):
            rects.append(fitz.Rect(x0, y0, x1, y1))

    if not rects:
        return None

    rect = rects[0]
    for r in rects[1:]:
        rect |= r

    rect = fitz.Rect(
        rect.x0 - 140,
        rect.y0 - 260,
        rect.x1 + 140,
        rect.y1 + 100,
    )

    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect


# -------------------------
# process
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
# UI (rosa 💗)
# -------------------------
HTML_PAGE = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DM Linker 💗</title>

<style>
body {
    font-family: Segoe UI, Arial;
    background: #ffe4f0;
    padding: 40px;
}

.card {
    max-width: 700px;
    margin: auto;
    background: white;
    padding: 30px;
    border-radius: 18px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.1);
}

h1 {
    margin-top: 0;
}

button {
    background: #ff4fa3;
    border: none;
    padding: 14px 20px;
    color: white;
    font-weight: bold;
    border-radius: 10px;
    cursor: pointer;
}

button:hover {
    background: #ff2c8a;
}

input {
    margin-bottom: 15px;
}
</style>
</head>

<body>
<div class="card">
<h1>💗 DM Linker</h1>

<form method="post" action="/link" enctype="multipart/form-data">
    <input type="file" name="pdf" required>
    <br>
    <button type="submit">Starta länkning</button>
</form>

</div>
</body>
</html>
"""


# -------------------------
# routes
# -------------------------
@app.route("/")
def index():
    return HTML_PAGE


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
