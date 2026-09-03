FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
WORKDIR /app

COPY dev/lab/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/domoai/lab/water_consumption_simulator.py /app/domoai/lab/water_consumption_simulator.py
COPY src/domoai/runtime/clock.py /app/domoai/runtime/clock.py
COPY dev/lab/water-meter/server.py /app/water-meter/server.py
COPY dev/lab/water-meter/profile.json /app/profile.json

CMD ["python", "/app/water-meter/server.py"]
