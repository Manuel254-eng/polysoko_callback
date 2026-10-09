FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=8080
EXPOSE 8080
# Threads let the background forwards run alongside new callbacks.
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --access-logfile -
