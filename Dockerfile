FROM python:3.12-slim
WORKDIR /srv/app
COPY server/ ./server/
COPY client/ ./client/
COPY install/ ./install/
COPY LICENSE ./LICENSE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python3", "server/hub.py", "serve"]
