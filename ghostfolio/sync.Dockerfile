FROM docker.io/library/python:3.12-alpine
WORKDIR /opt/sync
COPY ghostfolio_sync.py /opt/sync/ghostfolio_sync.py
ENTRYPOINT ["python", "/opt/sync/ghostfolio_sync.py"]
CMD ["run"]
