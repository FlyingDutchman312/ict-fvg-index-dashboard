FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Copy every runtime module explicitly. Keep original fvg_engine.py and other files in GitHub.
COPY worker.py fvg_engine.py candle_validation.py observation_store.py candle_chart_store.py shadow_engine.py signal_intelligence.py paper_simulator.py paper_diagnostics.py ./
CMD ["python", "-u", "worker.py"]
