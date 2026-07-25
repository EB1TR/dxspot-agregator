FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system dxspot \
    && adduser --system --ingroup dxspot --home /app dxspot \
    && mkdir -p /app/data \
    && chown -R dxspot:dxspot /app

COPY --chown=dxspot:dxspot dxspot/ /app/dxspot/
COPY --chown=dxspot:dxspot dxspot_agregator.py /app/
COPY --chown=dxspot:dxspot config.example.json /app/config.json
COPY --chown=dxspot:dxspot README.md LICENSE /app/

USER dxspot

EXPOSE 7300 8080

ENTRYPOINT ["python3", "-m", "dxspot"]
CMD ["--config", "/app/config.json"]
