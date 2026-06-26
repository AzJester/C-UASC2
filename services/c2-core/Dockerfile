# Reference image. A fielded build uses an Iron Bank-hardened base and an SBOM
# signed in the DevSecOps pipeline (docs/06).
FROM python:3.11-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
# Run as non-root (least privilege).
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
