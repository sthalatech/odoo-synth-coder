# masker image: postgres 16 client/server libs + greenmask + odoo-synth rules.
FROM postgres:16

ARG GREENMASK_VERSION=v0.2.22

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates gettext-base python3 python3-passlib openssh-client \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/gm.tgz \
      "https://github.com/GreenmaskIO/greenmask/releases/download/${GREENMASK_VERSION}/greenmask-linux-amd64.tar.gz" \
    && tar xzf /tmp/gm.tgz -C /tmp \
    && install -m0755 "$(find /tmp -maxdepth 2 -type f -name 'greenmask*' ! -name '*.sha256' ! -name '*.tgz' | head -1)" /usr/local/bin/greenmask \
    && rm -rf /tmp/gm.tgz \
    && greenmask --version

WORKDIR /work
COPY rules/ /work/rules/
COPY profiles/ /work/profiles/
COPY greenmask.tmpl.yml /work/greenmask.tmpl.yml
COPY entrypoint.sh /work/entrypoint.sh
RUN chmod +x /work/entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
