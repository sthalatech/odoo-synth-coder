# odoo image: base odoo + optional custom addons from a git URL.
ARG ODOO_IMAGE=odoo:19
FROM ${ODOO_IMAGE}

ARG CUSTOM_ADDONS_GIT_URL=

USER root
RUN mkdir -p /mnt/extra-addons-custom
# Clone custom addons if a URL was provided at build time (core-only when empty).
RUN if [ -n "${CUSTOM_ADDONS_GIT_URL}" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git \
      && git clone --depth 1 "${CUSTOM_ADDONS_GIT_URL}" /mnt/extra-addons-custom \
      && rm -rf /var/lib/apt/lists/* ; \
    fi

COPY entrypoint.sh /odoo-entrypoint.sh
RUN chmod +x /odoo-entrypoint.sh

ENTRYPOINT ["/odoo-entrypoint.sh"]
