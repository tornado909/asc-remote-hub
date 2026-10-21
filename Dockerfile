FROM python:3.12-slim
WORKDIR /srv/app
COPY server/ ./server/
COPY client/ ./client/
COPY install/ ./install/
COPY LICENSE ./LICENSE
# The Hub runs as an unprivileged PUID:PGID user. Ensure it can read
# source copied from root-owned staging trees without widening write access.
RUN chmod -R a+rX /srv/app/server /srv/app/client /srv/app/install /srv/app/LICENSE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python3", "server/hub.py", "serve"]
