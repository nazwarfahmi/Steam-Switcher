FROM python:3.12-alpine
WORKDIR /app
COPY cloud_server.py /app/cloud_server.py
ENV PORT=8765
ENV DATA_DIR=/data/saves
EXPOSE 8765
CMD ["python", "/app/cloud_server.py"]

