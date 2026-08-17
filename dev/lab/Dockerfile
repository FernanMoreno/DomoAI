FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY mqtt/fake_zigbee2mqtt.py /app/mqtt/fake_zigbee2mqtt.py
COPY modbus/simulator.json /app/modbus/simulator.json

CMD ["python", "/app/mqtt/fake_zigbee2mqtt.py"]
