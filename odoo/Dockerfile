# odoo image: runs Odoo FROM SOURCE (odoo-bin) at a pinned git ref.
# The official odoo image is reused only as a dependency base (system libs +
# Python packages + wkhtmltopdf). Core Odoo is then replaced by a source clone
# at ODOO_GIT_REF so the running code matches a specific commit/branch (e.g. from
# the provenance step). Custom addons are optionally cloned too.
ARG ODOO_IMAGE=odoo:19
FROM ${ODOO_IMAGE}

ARG ODOO_GIT_URL=https://github.com/odoo/odoo
ARG ODOO_GIT_REF=19.0
ARG CUSTOM_ADDONS_GIT_URL=

USER root

# git for the source checkout(s).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Clone Odoo source at the pinned ref into /opt/odoo-src. Try a shallow
# branch/tag clone first; fall back to a full clone + checkout for bare commits.
RUN set -eux; \
    if git clone --depth 1 --branch "${ODOO_GIT_REF}" "${ODOO_GIT_URL}" /opt/odoo-src 2>/dev/null; then \
      :; \
    else \
      git clone "${ODOO_GIT_URL}" /opt/odoo-src \
      && git -C /opt/odoo-src checkout "${ODOO_GIT_REF}"; \
    fi; \
    git -C /opt/odoo-src rev-parse HEAD > /opt/odoo-src.commit

# Install any extra Python requirements the pinned source declares (deps already
# largely present from the base image; this covers version drift). Best-effort.
RUN pip install --no-cache-dir --break-system-packages -r /opt/odoo-src/requirements.txt || true

# Optional custom addons.
RUN mkdir -p /mnt/extra-addons-custom \
 && if [ -n "${CUSTOM_ADDONS_GIT_URL}" ]; then \
      git clone --depth 1 "${CUSTOM_ADDONS_GIT_URL}" /mnt/extra-addons-custom; \
    fi

COPY entrypoint.sh /odoo-entrypoint.sh
RUN chmod +x /odoo-entrypoint.sh

ENTRYPOINT ["/odoo-entrypoint.sh"]
