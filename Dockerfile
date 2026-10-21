FROM python:3.12-slim

# Fonts for the title covers (covers.py)
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-liberation fonts-lato \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY paperboy ./paperboy

ENV PAPERBOY_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 17300
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,json,os; c=json.load(open(os.environ['PAPERBOY_DATA']+'/config.json')); urllib.request.urlopen(f'http://127.0.0.1:{c.get(\"port\",17300)}/health', timeout=4)"
CMD ["python", "-m", "paperboy"]
