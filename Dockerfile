FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY bihr_connector /app/bihr_connector
COPY alembic.ini /app/
COPY alembic /app/alembic
RUN pip install --no-cache-dir .
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
