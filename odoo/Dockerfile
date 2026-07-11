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
ARG CUSTOM_ADDONS_GIT_REF=

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

# Enterprise addons: baked from the build context at odoo/enterprise/ (the
# 02_build_push.sh script auto-unzips odoo/enterprise.zip into it beforehand).
# The directory always exists (.gitkeep) so this COPY never fails; it's empty
# when no enterprise code is provided.
COPY enterprise/ /opt/enterprise/

# Local custom modules: baked from the build context at odoo/custom-addons/.
COPY custom-addons/ /opt/custom/

# Install any Python requirements declared by enterprise/custom modules.
RUN set -eux; \
    for base in /opt/enterprise /opt/custom; do \
      find "$base" -maxdepth 2 -name requirements.txt -exec \
        pip install --no-cache-dir --break-system-packages -r {} \; 2>/dev/null || true; \
    done

# Optional custom addons from git (clones CUSTOM_ADDONS_GIT_REF if set).
# A GitHub token is provided via a BuildKit secret (id=gh_token) for private
# repos. The secret is mounted only for this RUN and never persists in a layer.
RUN --mount=type=secret,id=gh_token,required=false \
    mkdir -p /mnt/extra-addons-custom \
 && if [ -n "${CUSTOM_ADDONS_GIT_URL}" ]; then \
      URL="${CUSTOM_ADDONS_GIT_URL}"; \
      if [ -f /run/secrets/gh_token ]; then \
        TOK="$(cat /run/secrets/gh_token)"; \
        URL="$(printf '%s' "$URL" | sed -E "s#https://#https://x-access-token:${TOK}@#")"; \
      fi; \
      if [ -n "${CUSTOM_ADDONS_GIT_REF}" ]; then \
        git clone --depth 1 --branch "${CUSTOM_ADDONS_GIT_REF}" "$URL" /mnt/extra-addons-custom; \
      else \
        git clone --depth 1 "$URL" /mnt/extra-addons-custom; \
      fi; \
      git -C /mnt/extra-addons-custom rev-parse HEAD > /mnt/extra-addons-custom.commit; \
      git -C /mnt/extra-addons-custom remote set-url origin "${CUSTOM_ADDONS_GIT_URL}"; \
    fi

COPY entrypoint.sh /odoo-entrypoint.sh
RUN chmod +x /odoo-entrypoint.sh

ENTRYPOINT ["/odoo-entrypoint.sh"]
