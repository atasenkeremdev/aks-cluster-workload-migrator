# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS runtime
ARG TARGETARCH
ARG AZURE_CLI_VERSION=2.91.0
ARG KUBECTL_VERSION=v1.34.1
ARG KUBELOGIN_VERSION=v0.2.22
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/azure-cli/bin:$PATH HOME=/home/migrator \
    AZURE_CORE_COLLECT_TELEMETRY=0 AZURE_CORE_LOGIN_EXPERIENCE_V2=off AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no
RUN --mount=type=secret,id=build_ca,mode=0444 \
    if [ -s /run/secrets/build_ca ]; then \
        printf 'Acquire::https::CaInfo "/run/secrets/build_ca";\n' > /etc/apt/apt.conf.d/99-build-ca; \
        export PIP_CERT=/run/secrets/build_ca; \
    fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip tini \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/azure-cli \
    && /opt/azure-cli/bin/pip install --no-cache-dir "azure-cli==${AZURE_CLI_VERSION}" \
    && rm -f /etc/apt/apt.conf.d/99-build-ca
RUN --mount=type=secret,id=build_ca,mode=0444 set -eu; \
    if [ -s /run/secrets/build_ca ]; then export CURL_CA_BUNDLE=/run/secrets/build_ca; fi; \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo 'Unsupported image architecture' >&2; exit 1;; esac; \
    curl --fail --show-error --location --retry 3 -o /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    curl --fail --show-error --location --retry 3 -o /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256"; \
    printf '%s  /tmp/kubectl\n' "$(cat /tmp/kubectl.sha256)" | sha256sum --check; \
    install -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    archive="kubelogin-linux-${TARGETARCH}.zip"; \
    curl --fail --show-error --location --retry 3 -o "/tmp/${archive}" "https://github.com/Azure/kubelogin/releases/download/${KUBELOGIN_VERSION}/${archive}"; \
    curl --fail --show-error --location --retry 3 -o "/tmp/${archive}.sha256" "https://github.com/Azure/kubelogin/releases/download/${KUBELOGIN_VERSION}/${archive}.sha256"; \
    cd /tmp; sha256sum --check "${archive}.sha256"; \
    unzip -q "$archive" -d kubelogin-extracted; \
    install -m 0755 "kubelogin-extracted/bin/linux_${TARGETARCH}/kubelogin" /usr/local/bin/kubelogin; \
    rm -rf /tmp/kubectl /tmp/kubectl.sha256 "/tmp/${archive}" "/tmp/${archive}.sha256" /tmp/kubelogin-extracted
RUN groupadd --gid 10001 migrator && useradd --uid 10001 --gid 10001 --create-home migrator \
    && mkdir -p /var/lib/migrator /opt/migrator && chown 10001:10001 /var/lib/migrator /opt/migrator
WORKDIR /opt/migrator
ENV AZURE_CONFIG_DIR=/tmp/azure/profile MIGRATOR_STATE_DIR=/var/lib/migrator \
    KUBECONFIG=/tmp/kubeconfig/config MIGRATOR_KUBECONFIG_SECRET=/etc/migrator/kubeconfig/config \
    MIGRATOR_AZURE_AUTH=device-code MIGRATOR_AUTH_MODE=device-code \
    MIGRATOR_PUBLIC_ORIGIN=http://localhost:8081

COPY --chown=10001:10001 migrator/ ./migrator/
COPY --chown=10001:10001 web/ ./web/
COPY --chown=10001:10001 excluded-namespaces.txt ./excluded-namespaces.txt
COPY --chown=10001:10001 container/ ./container/
USER 10001:10001
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).close()"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "container.entrypoint"]
CMD ["--host", "0.0.0.0", "--port", "8081"]

FROM runtime AS test
COPY --chown=10001:10001 tests/ ./tests/
RUN MIGRATOR_AUTH_MODE=local python -m unittest discover -s tests -v

FROM runtime AS release
