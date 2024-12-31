ARG BUILD_FROM
FROM $BUILD_FROM

# Copy root filesystem
COPY rootfs /

# Install vhclient
ARG BUILD_ARCH
RUN /usr/bin/getvhclient $BUILD_ARCH
