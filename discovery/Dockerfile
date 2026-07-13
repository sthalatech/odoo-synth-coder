# discovery image: postgres client + git + python3 + ssh, for provenance
# discovery against a live Odoo source DB and its addons repo.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-client git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work
COPY discover.py /work/discover.py
COPY entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
