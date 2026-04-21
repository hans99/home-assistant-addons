FROM ghcr.io/home-assistant/base:latest

# Copy root filesystem
COPY rootfs /

# Install vhclient
ARG BUILD_ARCH
RUN /usr/bin/getvhclient $BUILD_ARCH && echo "Cache busted at $(date)"
