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
# Space-separated list of extra pip packages discovered for this profile
# (manifest external_dependencies + requirements.txt + heuristic import scan,
# incl. undeclared deps like googletrans). Baked in on top of the static set.
ARG PYTHON_DEPS=

USER root

# git for the source checkout(s).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Clone Odoo source at the pinned ref into /opt/odoo-src. A shallow branch/tag
# clone works for named refs; for a bare commit SHA we shallow-fetch just that
# commit (GitHub allows fetch-by-SHA) so we never pull the multi-GB full history.
RUN set -eux; \
    if git clone --depth 1 --branch "${ODOO_GIT_REF}" "${ODOO_GIT_URL}" /opt/odoo-src 2>/dev/null; then \
      :; \
    else \
      mkdir -p /opt/odoo-src \
      && git -C /opt/odoo-src init -q \
      && git -C /opt/odoo-src remote add origin "${ODOO_GIT_URL}" \
      && git -C /opt/odoo-src fetch -q --depth 1 origin "${ODOO_GIT_REF}" \
      && git -C /opt/odoo-src checkout -q FETCH_HEAD; \
    fi; \
    (git -C /opt/odoo-src rev-parse HEAD 2>/dev/null || echo "${ODOO_GIT_REF}") > /opt/odoo-src.commit; \
    rm -rf /opt/odoo-src/.git; \
    # The base odoo image bundles a full (rolling, newer) Odoo under
    # dist-packages. `odoo.addons` is a namespace package, so that copy's addons
    # dir leaks into odoo.addons.__path__ at runtime and can shadow our pinned
    # source (e.g. a newer base_vat importing tools.verify_hash_signed that our
    # pinned odoo.tools lacks). Remove it so ONLY /opt/odoo-src provides core +
    # its bundled addons. System/python deps from the base image are unaffected.
    rm -rf /usr/lib/python3/dist-packages/odoo /usr/lib/python3/dist-packages/odoo-*.egg-info

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
        if git clone --depth 1 --branch "${CUSTOM_ADDONS_GIT_REF}" "$URL" /mnt/extra-addons-custom 2>/dev/null; then \
          :; \
        else \
          git init -q /mnt/extra-addons-custom \
          && git -C /mnt/extra-addons-custom remote add origin "$URL" \
          && git -C /mnt/extra-addons-custom fetch -q --depth 1 origin "${CUSTOM_ADDONS_GIT_REF}" \
          && git -C /mnt/extra-addons-custom checkout -q FETCH_HEAD; \
        fi; \
      else \
        git clone --depth 1 "$URL" /mnt/extra-addons-custom; \
      fi; \
      git -C /mnt/extra-addons-custom rev-parse HEAD > /mnt/extra-addons-custom.commit; \
      git -C /mnt/extra-addons-custom remote set-url origin "${CUSTOM_ADDONS_GIT_URL}"; \
    fi

# Python + system deps required by the baked addons (enterprise + custom +
# third_party). Derived from each module's __manifest__.py external_dependencies
# plus a static import scan of the custom repos.
#
# CRITICAL: the base image ships ancient system builds of cryptography (3.4.8) /
# pyOpenSSL (21.0.0) that Odoo core (base -> ir_mail_server) imports. Letting pip
# upgrade cryptography (pulled transitively by e.g. google-auth) breaks pyOpenSSL
# with `module 'lib' has no attribute 'GEN_EMAIL'`. We freeze the fragile core
# libs to their base versions via a constraints file so the extras resolve
# around them. numpy/pandas are pinned to an ABI-compatible pair (pandas 2.1.x is
# built against numpy 1.x; mixing with numpy 2.x yields "numpy.dtype size
# changed" import crashes). Installed per-package so one bad wheel doesn't block
# the rest.
RUN set -eux; \
    printf '%s\n' \
      'cryptography==3.4.8' 'pyOpenSSL==21.0.0' 'lxml==4.8.0' 'Pillow==9.0.1' \
      'python-dateutil==2.8.1' 'Werkzeug==2.0.2' 'psycopg2==2.9.2' \
      'reportlab==3.6.8' 'gevent==21.8.0' 'greenlet==1.1.2' 'zeep==4.1.0' \
      'requests==2.25.1' 'urllib3==1.26.5' 'numpy==1.26.4' 'pandas==2.1.4' \
      > /tmp/constraints.txt; \
    # System libraries for weasyprint (pango/cairo/gdk-pixbuf), placed here so
    # earlier expensive clone/COPY layers stay cached. Best-effort.
    apt-get update \
      && apt-get install -y --no-install-recommends \
         libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
         libgdk-pixbuf-2.0-0 libcairo2 shared-mime-info \
      && rm -rf /var/lib/apt/lists/* || true; \
    # Scientific stack: install numpy+pandas together at ABI-compatible pins.
    pip install --no-cache-dir "numpy==1.26.4" "pandas==2.1.4" polars; \
    # Relax exact-version external_dependencies pins in custom manifests that
    # have no prebuilt wheel for this Python and no compiler in the image (e.g.
    # pycryptodome==3.9.8). Odoo enforces the pin as an exact string match; the
    # underlying crypto API is stable, so dropping the pin lets the module load
    # against the newer pycryptodome we install below.
    find /mnt/extra-addons-custom -name __manifest__.py -exec \
      sed -i -E 's/pycryptodome[[:space:]]*==[[:space:]]*3\.9\.8/pycryptodome/g' {} + 2>/dev/null || true; \
    for base in /mnt/extra-addons-custom; do \
      find "$base" -maxdepth 3 -name requirements.txt -exec \
        pip install --no-cache-dir -c /tmp/constraints.txt -r {} \; 2>/dev/null || true; \
    done; \
    for pkg in \
      boto3 confluent-kafka kafka-python geoip2 google-auth dbfread ebaysdk \
      pyusb pyserial pycryptodome asn1crypto PyJWT \
      phonenumbers openpyxl qrcode python-ldap xmlsec \
      Levenshtein fuzzywuzzy python-stdnum xmltodict pdf417gen signxml \
      pyhanko mock imgkit PyMuPDF weasyprint; do \
      pip install --no-cache-dir -c /tmp/constraints.txt "$pkg" \
        || echo "WARN: could not install $pkg"; \
    done

# Profile-discovered extra deps (PYTHON_DEPS build-arg). Installed per-package
# against the same constraints so one bad wheel never blocks the rest. This is
# what makes each per-profile image match its source's *actual* imports,
# including undeclared ones the static list above cannot know about.
ARG PYTHON_DEPS
RUN set -eux; \
    if [ -n "${PYTHON_DEPS}" ]; then \
      for pkg in ${PYTHON_DEPS}; do \
        pip install --no-cache-dir --break-system-packages -c /tmp/constraints.txt "$pkg" \
          || echo "WARN: could not install discovered dep $pkg"; \
      done; \
    fi

COPY entrypoint.sh /odoo-entrypoint.sh
RUN chmod +x /odoo-entrypoint.sh

ENTRYPOINT ["/odoo-entrypoint.sh"]
