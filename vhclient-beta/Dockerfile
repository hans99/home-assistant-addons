# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
#
# Supported architectures match config.yaml / current Supervisor: aarch64, amd64.
# Pin the base image (recommended). Do not rely on an externally provided BUILD_FROM
# default from Supervisor (removed in Supervisor 2026.04.0).
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

# Python controller for VirtualHere Client API (named pipes)
RUN apk add --no-cache \
    python3 \
    py3-paho-mqtt

# Copy root filesystem (includes getvhclient helper)
COPY rootfs /

# Install VirtualHere console client for the build architecture.
# Supervisor provides BUILD_ARCH / BUILD_VERSION; Docker BuildKit provides TARGETARCH.
# Override CACHEBUST at build time to force a fresh download of the client binary.
ARG BUILD_ARCH
ARG BUILD_VERSION
ARG TARGETARCH
ARG CACHEBUST=1
RUN set -euo pipefail \
  && echo "CACHEBUST=${CACHEBUST}" \
  && arch="${BUILD_ARCH:-}" \
  && if [ -z "${arch}" ]; then \
       if [ -z "${TARGETARCH:-}" ]; then \
         echo "Neither BUILD_ARCH nor TARGETARCH is set; use Supervisor or Docker BuildKit." >&2; \
         exit 1; \
       fi; \
       case "${TARGETARCH}" in \
         amd64) arch="amd64" ;; \
         arm64) arch="aarch64" ;; \
         *) echo "Unsupported TARGETARCH: ${TARGETARCH} (supported: amd64, arm64)" >&2; exit 1 ;; \
       esac; \
     fi \
  && case "${arch}" in \
       amd64|aarch64) ;; \
       *) echo "Unsupported BUILD_ARCH: ${arch} (supported: aarch64, amd64)" >&2; exit 1 ;; \
     esac \
  && echo "Building VirtualHere client for ARCH=${arch} VERSION=${BUILD_VERSION:-unknown}" \
  && /usr/bin/getvhclient "${arch}"

LABEL \
  org.opencontainers.image.title="Home Assistant App: VirtualHere Client (Beta)" \
  org.opencontainers.image.description="Beta VirtualHere USB Client for Home Assistant" \
  org.opencontainers.image.source="https://git.carlscrona.se/haka/home-assistant-addons" \
  org.opencontainers.image.licenses="Apache-2.0"
