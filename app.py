import re
import tempfile
from urllib.parse import quote_plus

import fitz
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_RE = re.compile(r"\b\d{6,7}\b")

URL_CACHE = {}


def find_articles(text):
    return list(dict.fromkeys(ARTICLE_RE.findall(text or "")))


def lookup(article):

    if article in URL_CACHE:
        return URL_CACHE[article]

    search_url = (
        f"https://www.jula.se/search/?query={quote_plus(article)}"
    )

    try:
        response = requests.get(
            search_url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0"
                )
            }
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        links = soup.find_all("a", href=True)

        for link in links:

            href = link["href"]

            if "/catalog/" not in href:
                continue

            if href.startswith("/"):
                href = "https://www.jula.se" + href

            URL_CACHE[article] = href
            return href

    except Exception as e:
        print(
            f"Fel vid uppslagning "
            f"{article}: {e}"
        )

    return None


def find_product_rect(page, article):

    words = page.get_text("words")

    anchors = [
        w for w in words
        if w[4] == article
    ]

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
            rects.append(
                fitz.Rect(
                    x0,
                    y0,
                    x1,
                    y1
                )
            )

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

    for page_index, page in enumerate(doc):

        print(
            f"Bearbetar sida "
            f"{page_index + 1}/{len(doc)}"
        )

        text = page.get_text()

        articles = find_articles(text)

        for article in articles:

            product_url = lookup(article)

            if not product_url:
                print(
                    f"Ingen produkt hittad: "
                    f"{article}"
                )
                continue

            rect = find_product_rect(
                page,
                article
            )

            if rect is None:
                continue

            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": rect,
                    "uri": product_url,
                }
            )

            print(
                f"{article} -> "
                f"{product_url}"
            )

    doc.save(
        output_path,
        garbage=4,
        deflate=True
    )

    doc.close()


HTML_PAGE = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>DM Linker</title>

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
}

button{
    background:#ff4fa3;
    border:none;
    padding:14px 20px;
    color:white;
    border-radius:10px;
    cursor:pointer;
}
</style>

</head>
<body>

<div class="card">

<h1>DM Linker</h1>

/link

<input
    type="file"
    name="pdf"
    accept=".pdf"
    required>

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


@app.route("/health")
def health():
    return "OK", 200


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

    process_pdf(
        src.name,
        output_path
    )

    return send_file(
        output_path,
        as_attachment=True,
        download_name="linked.pdf",
        mimetype="application/pdf",
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=True
    )
