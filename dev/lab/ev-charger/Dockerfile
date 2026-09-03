FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
WORKDIR /app

COPY dev/lab/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/domoai/lab/ev_charging_simulator.py /app/domoai/lab/ev_charging_simulator.py
COPY src/domoai/runtime/clock.py /app/domoai/runtime/clock.py
COPY dev/lab/ev-charger/server.py /app/ev-charger/server.py
COPY dev/lab/ev-charger/profile.json /app/profile.json

CMD ["python", "/app/ev-charger/server.py"]
