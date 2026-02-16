FROM camptocamp/qgis-server:3.44-gdal3.10-desktop
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-psycopg2 python3-pyqt5 python3-owslib \
        python3-lxml python3-yaml python3-jinja2 \
        python3-fastapi \
    && rm -rf /var/lib/apt/lists/*

ENV QT_QPA_PLATFORM=offscreen
ENV XDG_RUNTIME_DIR=/tmp/xdg-runtime
RUN mkdir -p /tmp && chmod 777 /tmp
RUN mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

WORKDIR /app

COPY server.py .

CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8817"]

