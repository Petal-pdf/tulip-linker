
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "--timeout", "300", "-b", "0.0.0.0:8000", "app:app"]
