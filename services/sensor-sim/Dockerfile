# Reference image for the edge materiel simulator.
FROM python:3.11-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sim.py .

RUN useradd --create-home --uid 10002 simuser
USER simuser

CMD ["python", "sim.py"]
