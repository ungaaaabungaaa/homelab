FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY homelab.py /app/
COPY web /app/web
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8086
ENTRYPOINT ["python", "/app/homelab.py"]
CMD ["serve", "--data", "/data", "--host", "0.0.0.0"]
