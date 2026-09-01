import os
import re
import fitz
import tempfile
import requests

from bs4 import BeautifulSoup
from flask import Flask, request, send_file, render_template

app = Flask(__name__)

ARTICLE_PATTERN = r"\b\d{6,7}\b"


# -----------------------------
# LAND
# -----------------------------
def detect_domain(filename):

    filename = filename.lower()

    if "_pl" in filename:
        return "https://www.jula.pl"

    if "_no" in filename:
        return "https://www.jula.no"

    if "_fi" in filename:
        return "https://www.jula.fi"

    return "https://www.jula.se"


# -----------------------------
# PRODUKTSIDA
# -----------------------------
def find_product_url(domain, article):

    try:

        search_url = (
            f"{domain}/search/?query={article}"
        )

        response = requests.get(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            timeout=20
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        for link in soup.find_all(
            "a",
            href=True
        ):

            href = link["href"]

            if article in href:

                if href.startswith("/"):

                    return domain + href

                return href

        return search_url

    except Exception:
        return search_url


# -----------------------------
# HÄMTA ARTIKELNUMMER
# -----------------------------
def get_articles(text):

    matches = re.findall(
        ARTICLE_PATTERN,
        text
    )

    unique = []

    seen = set()

    for match in matches:

        if match not in seen:

            seen.add(match)
            unique.append(match)

    return unique


# -----------------------------
# FIRST ONLY
# -----------------------------
def get_first_article_per_block(page):

    output = []

    blocks = page.get_text("blocks")

    for block in blocks:

        text = block[4]

        matches = re.findall(
            ARTICLE_PATTERN,
            text
        )

        if matches:

            output.append(
                (
                    matches[0],
                    block
                )
            )

    return output


# -----------------------------
# LÄNKA SIDA
# -----------------------------
def link_page(page, domain):

    blocks = get_first_article_per_block(page)

    for article, block in blocks:

        x0 = block[0]
        y0 = block[1]
        x1 = block[2]
        y1 = block[3]

        rect = fitz.Rect(
            x0,
            y0,
            x1,
            y1
        )

        url = find_product_url(
            domain,
            article
        )

        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": rect,
                "uri": url
            }
        )


# -----------------------------
# PROCESSA PDF
# -----------------------------
def process_pdf(
    input_pdf,
    output_pdf,
    filename
):

    domain = detect_domain(
        filename
    )

    doc = fitz.open(input_pdf)

    for page in doc:

        link_page(
            page,
            domain
        )

    doc.save(
        output_pdf,
        garbage=4,
        deflate=True
    )

    doc.close()


# -----------------------------
# STARTSIDA
# -----------------------------
@app.route("/")
def index():
    return render_template(
        "index.html"
    )


# -----------------------------
# LÄNKA PDF
# -----------------------------
@app.route(
    "/link",
    methods=["POST"]
)
def link_pdf():

    if "pdf" not in request.files:

        return "Ingen PDF", 400

    pdf = request.files["pdf"]

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    ) as temp:

        pdf.save(temp.name)

        output_pdf = (
            temp.name.replace(
                ".pdf",
                "_linked.pdf"
            )
        )

    process_pdf(
        temp.name,
        output_pdf,
        pdf.filename
    )

    return send_file(
        output_pdf,
        as_attachment=True,
        download_name=(
            "linked.pdf"
        ),
        mimetype="application/pdf"
    )


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                8000
            )
        )
    )
