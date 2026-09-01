import os
import re
import fitz
import tempfile

from flask import (
    Flask,
    request,
    send_file
)

app = Flask(__name__)

ARTICLE_RE = r"\b\d{6}\b"


def detect_domain(filename):

    filename = filename.lower()

    if "_pl" in filename:
        return "https://www.jula.pl"

    if "_no" in filename:
        return "https://www.jula.no"

    if "_fi" in filename:
        return "https://www.jula.fi"

    return "https://www.jula.se"


def article_url(domain, article):

    return f"{domain}/a-{article}/"


def process_pdf(
    input_pdf,
    output_pdf,
    filename
):

    domain = detect_domain(filename)

    doc = fitz.open(input_pdf)

    for page in doc:

        blocks = page.get_text("blocks")

        for block in blocks:

            text = block[4]

            matches = re.findall(
                ARTICLE_RE,
                text
            )

            if not matches:
                continue

            # FIRST ONLY REGELN
            article = matches[0]

            url = article_url(
                domain,
                article
            )

            rect = fitz.Rect(
                block[0],
                block[1],
                block[2],
                block[3]
            )

            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": rect,
                    "uri": url
                }
            )

    doc.save(
        output_pdf,
        garbage=4,
        deflate=True
    )

    doc.close()


@app.route("/")
def index():

    return """
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<title>DM Linker</title>

<style>

body{
    font-family:Arial,sans-serif;
    background:#f5dce8;
    padding:50px;
}

.card{

    max-width:700px;

    margin:auto;

    background:white;

    padding:40px;

    border-radius:20px;
}

button{

    background:#ff4fa3;

    color:white;

    border:none;

    padding:15px 25px;

    border-radius:10px;

    cursor:pointer;

    font-size:16px;
}

</style>

</head>

<body>

<div class="card">

<h1>DM Linker</h1>

<form
action="/link"
method="POST"
enctype="multipart/form-data">

<inputbutton type="submit">
Starta länkning
</button>

</form>

</div>

</body>

</html>
"""


@app.route("/health")
def health():
    return "OK"


@app.route(
    "/link",
    methods=["POST"]
)
def link_pdf():

    if "pdf" not in request.files:
        return "Ingen PDF uppladdad", 400

    pdf = request.files["pdf"]

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    ) as src:

        pdf.save(src.name)

        output_pdf = src.name.replace(
            ".pdf",
            "_linked.pdf"
        )

    process_pdf(
        src.name,
        output_pdf,
        pdf.filename
    )

    return send_file(
        output_pdf,
        as_attachment=True,
        download_name="linked.pdf",
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
``
