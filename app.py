import os
import tempfile
import fitz

from flask import (
    Flask,
    request,
    send_file
)

app = Flask(__name__)


def process_pdf(input_pdf, output_pdf):

    doc = fitz.open(input_pdf)

    # TEST:
    # öppnar och sparar bara om PDF

    doc.save(output_pdf)

    doc.close()


@app.route("/")
def index():

    return """
<!DOCTYPE html>

<html>

<head>

<title>DM Linker</title>

<style>

body{
    font-family:Arial;
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
}

</style>

</head>

<body>

<div class="card">

<h1>DM Linker</h1>

<form
    action="/link"
    methodame="pdf"
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


@app.route(
    "/health"
)
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
        output_pdf
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
