FROM python:3.12-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY farewatch ./farewatch
COPY config.example.toml ./config.example.toml
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)" || exit 1
ENTRYPOINT ["python", "-m", "farewatch"]
CMD ["--help"]
