import os
import re
import tempfile
import fitz

from flask import Flask
from flask import request
from flask import send_file

app = Flask(__name__)

# =========================
# ARTIKELNUMMER -> URL
# =========================

URLS = {
    "023534":
    "https://www.jula.se/catalog/bygg-och-farg/varme-och-ventilation/luftforbattring/elektriska-avfuktare/luftavfuktare-023534/",

    "028959":
    "https://www.jula.se/catalog/verktyg-och-maskiner/tillbehor-till-elverktyg/bits-och-bitssatser/borr-och-bitssatser/borr-och-bitssats-028959/",

    "032291":
    "https://www.jula.se/catalog/el-och-belysning/belysning/inomhusbelysning/led-armaturer/led-armatur-032291/",

    "024289":
    "https://www.jula.se/catalog/bygg-och-farg/forvaring/hyllor/forvaringshyllor/forvaringshylla-024289/"
}

ARTICLE_RE = r"\b\d{6}\b"

# =========================
# PDF PROCESSING
# =========================

def process_pdf(input_pdf, output_pdf):

    doc = fitz.open(input_pdf)

    for page in doc:

        words = page.get_text("words")

        for word in words:

            text = word[4]

            if not re.fullmatch(ARTICLE_RE, text):
                continue

            if text not in URLS:
                continue

            url = URLS[text]

            rect = fitz.Rect(
                word[0],
                word[1],
                word[2],
                word[3]
            )

            page.insert_link({
                "kind": fitz.LINK_URI,
                "from": rect,
                "uri": url
            })

            print(f"Linked {text}")

    doc.save(
        output_pdf,
        garbage=4,
        deflate=True
    )

    doc.close()

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

body{
    font-family:Arial,sans-serif;
    background:#f5dce8;
    padding:40px;
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

# =========================
# ROUTES
# =========================

@app.route("/")
def index():
    return HTML_PAGE


@app.route("/health")
def health():
    return "OK"


@app.route(
    "/link",
    methods=["POST"]
)
def link():

    if "pdf" not in request.files:
        return "Ingen PDF uppladdad", 400

    uploaded_pdf = request.files["pdf"]

    with tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    ) as src:

        uploaded_pdf.save(src.name)

        output_pdf = src.name.replace(
            ".pdf",
            "_linked.pdf"
        )

    process_pdf(
        src.name,
        output_pdf
    )

    return send_file(
        output_pdf,
        as_attachment=True,
        download_name="linked.pdf",
        mimetype="application/pdf"
    )

# =========================
# START
# =========================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                8000
            )
        ),
        debug=True
    )
