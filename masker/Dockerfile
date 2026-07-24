# masker image: postgres 16 client/server libs + greenmask + odoo-synth rules.
FROM postgres:16

ARG GREENMASK_VERSION=v0.2.22
# Pinned SHA256 of the greenmask-linux-amd64.tar.gz release asset for
# ${GREENMASK_VERSION}. Supply-chain integrity: the build fails if the
# downloaded tarball does not match (tampering, a swapped/re-published release
# asset, or a MITM despite HTTPS). Update this together with GREENMASK_VERSION.
#   v0.2.22 -> a8a8d79cc2b5245ae9a6f6efdf307e5b2df6ef3db625685d21ba97f9f99f4022
ARG GREENMASK_TARBALL_SHA256=a8a8d79cc2b5245ae9a6f6efdf307e5b2df6ef3db625685d21ba97f9f99f4022

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates gettext-base python3 python3-passlib openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Download greenmask and verify it in two layers:
#   1. tarball SHA256 vs the pinned GREENMASK_TARBALL_SHA256 build arg
#      (fails the build on a tampered/swapped download).
#   2. the extracted binary vs the greenmask.sha256 checksum shipped *inside*
#      the tarball (catches post-extraction tampering; `sha256sum -c` reads
#      "292b... *greenmask" from greenmask.sha256 and checks ./greenmask).
# The old `find ... ! -name '*.sha256'` glob was fragile; the tarball layout is
# fixed (LICENSE, greenmask, greenmask.sha256) so install the binary directly.
RUN set -eu; \
    url="https://github.com/GreenmaskIO/greenmask/releases/download/${GREENMASK_VERSION}/greenmask-linux-amd64.tar.gz"; \
    curl -fsSL -o /tmp/gm.tgz "$url"; \
    echo "${GREENMASK_TARBALL_SHA256}  /tmp/gm.tgz" | sha256sum -c -; \
    tar xzf /tmp/gm.tgz -C /tmp; \
    cd /tmp && sha256sum -c greenmask.sha256; \
    install -m0755 /tmp/greenmask /usr/local/bin/greenmask; \
    rm -rf /tmp/gm.tgz /tmp/greenmask /tmp/greenmask.sha256 /tmp/LICENSE; \
    greenmask --version

WORKDIR /work
COPY rules/ /work/rules/
COPY profiles/ /work/profiles/
COPY greenmask.tmpl.yml /work/greenmask.tmpl.yml
COPY entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
