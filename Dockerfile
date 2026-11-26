FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir --default-timeout=120 --retries 5 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        flask==3.1.3 \
        gevent==26.9.0 \
        python-dotenv==1.2.4 \
        edge-tts==7.3.1 \
        emoji==2.16.0 \
        aiohttp==3.12.15 \
        aiohappyeyeballs==2.7.1 \
        aiosignal==1.4.0 \
        attrs==26.1.0 \
        frozenlist==1.8.0 \
        multidict==6.1.0 \
        yarl==1.18.3 \
        propcache==0.5.4 \
        idna==3.20 \
        imageio-ffmpeg==0.8.1 && \
    python -c "import imageio_ffmpeg, os; src=imageio_ffmpeg.get_ffmpeg_exe(); os.symlink(src, '/usr/local/bin/ffmpeg')"

COPY app/ /app

RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5050/health', timeout=3)" || exit 1

CMD ["python", "/app/server.py"]
