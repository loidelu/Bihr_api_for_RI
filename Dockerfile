FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY bihr_connector /app/bihr_connector
RUN pip install --no-cache-dir .
CMD ["python", "-m", "bihr_connector.cli", "run-scheduler"]
