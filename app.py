import os
import re
import tempfile
import fitz

from flask import Flask, request, send_file

app = Flask(__name__)

ARTICLE_REGEX = r"\b\d{6}\b"


def detect_domain(filename):

    name = filename.upper()

    if name.startswith("SWE"):
        return "https://www.jula.se"

    if name.startswith("NOR"):
        return "https://www.jula.no"

    if name.startswith("POL"):
        return "https://www.jula.pl"

    if name.startswith("FIN"):
        return "https://www.jula.fi"

    return "https://www.jula.se"


def build_url(domain, article):

    return f"{domain}/a-{article}/"


def process_pdf(
    input_file,
    output_file,
    filename
):

    domain = detect_domain(
        filename
    )

    doc = fitz.open(input_file)

    for page in doc:

        page_text = page.get_text()

        articles = set(
            re.findall(
                ARTICLE_REGEX,
                page_text
            )
        )

        for article in articles:

            url = build_url(
                domain,
                article
            )

            locations = page.search_for(
                article
            )

            for rect in locations:

                try:

                    page.insert_link(
                        {
                            "kind": fitz.LINK_URI,
                            "from": rect,
                            "uri": url
                        }
                    )

                except Exception:
                    pass

    doc.save(
        output_file,
        garbage=4,
        deflate=True
    )

    doc.close()


@app.route("/")
def index():

    return """
<!DOCTYPE html>
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
    border-radius:20px;
    padding:40px;
}

button{
    background:#ff4fa3;
    color:white;
    border:none;
    padding:15px 25px;
    border-radius:10px;
    cursor:pointer;
}

h1{
    margin-top:0;
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
    ) as source:

        pdf.save(source.name)

        output = source.name.replace(
            ".pdf",
            "_linked.pdf"
        )

    process_pdf(
        source.name,
        output,
        pdf.filename
    )

    return send_file(
        output,
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
