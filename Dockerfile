FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The vault lives here; mount a volume to keep it across restarts.
VOLUME /app/data
ENV SANCTUM_DATA_DIR=/app/data

CMD ["python", "bot.py"]
