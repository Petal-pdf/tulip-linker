import re
import tempfile
from urllib.parse import quote_plus

import fitz
from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")


def find_articles(text):
    return list(dict.fromkeys(ARTICLE_RE.findall(text or "")))


def lookup(article):
    return f"https://www.jula.se/search/?query={quote_plus(article)}"


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
            abs(x0 - ax0) < 350
            and y0 > ay0 - 350
            and y0 < ay0 + 220
        ):
            rects.append(fitz.Rect(x0, y0, x1, y1))

    if not rects:
        return None

    rect = rects[0]

    for r in rects[1:]:
        rect |= r

    rect = fitz.Rect(
        rect.x0 - 100,
        rect.y0 - 220,
        rect.x1 + 100,
        rect.y1 + 80,
    )

    rect = fitz.Rect(
        max(0, rect.x0),
        max(0, rect.y0),
        min(page.rect.width, rect.x1),
        min(page.rect.height, rect.y1),
    )

    return rect


def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)

    linked = set()

    for page in doc:
        blocks = page.get_text("blocks")

        for block in blocks:
            text = block[4]

            articles = find_articles(text)

            for article in articles:
                rect = find_product_rect(page, article)

                if rect is None:
                    continue

                key = (
                    page.number,
                    article,
                    round(rect.x0),
                    round(rect.y0),
                    round(rect.x1),
                    round(rect.y1),
                )

                if key in linked:
                    continue

                linked.add(key)

                page.insert_link(
                    {
                        "kind": fitz.LINK_URI,
                        "from": rect,
                        "uri": lookup(article),
                    }
                )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


HTML_PAGE = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DM Linker 💗</title>

<style>
body{
    font-family:Segoe UI,Arial,sans-serif;
    background:#ffe4f0;
    padding:40px;
}

.card{
    max-width:700px;
    margin:auto;
    background:white;
    padding:30px;
    border-radius:18px;
    box-shadow:0 6px 20px rgba(0,0,0,.1);
}

h1{
    margin-top:0;
}

input{
    margin-bottom:15px;
}

button{
    background:#ff4fa3;
    border:none;
    padding:14px 20px;
    color:white;
    font-weight:bold;
    border-radius:10px;
    cursor:pointer;
}

button:hover{
    background:#ff2c8a;
}
</style>
</head>

<body>

<div class="card">

<h1>💗 DM Linker</h1>

<form methodt
    type="file"
    name="pdf"
    accept=".pdf"
    required
>

<br><br>

<button type="submit">
    Starta länkning
</button>

</form>

</div>

</body>
</html>
"""


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/link", methods=["POST"])
def link():

    if "pdf" not in request.files:
        return "Ingen PDF uppladdad", 400

    pdf = request.files["pdf"]

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    ) as src:

        pdf.save(src.name)

        output_path = src.name.replace(
            ".pdf",
            "_linked.pdf"
        )

    process_pdf(src.name, output_path)

    return send_file(
        output_path,
        as_attachment=True,
        download_name="linked.pdf",
        mimetype="application/pdf",
    )


@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False,
    )
