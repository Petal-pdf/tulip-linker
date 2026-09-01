import os
import tempfile

import fitz
from flask import Flask, request, send_file

app = Flask(__name__)


def process_pdf(input_path, output_path):
    doc = fitz.open(input_path)

    # Test: lägg bara till en sparad PDF
    doc.save(output_path)

    doc.close()


HTML_PAGE = """
<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>DM Linker</title>

<style>
body{
    font-family:Segoe UI,Arial,sans-serif;
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
    font-size:16px;
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
    return "OK"


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
        mimetype="application/pdf"
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )
