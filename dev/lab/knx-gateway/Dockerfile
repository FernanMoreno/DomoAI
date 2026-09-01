FROM debian:bookworm-slim

ARG KNXD_COMMIT=7080c10a7d8eb6f925522030ab95a690d0127f2d

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        git \
        iproute2 \
        libev-dev \
        libfmt-dev \
        libsystemd-dev \
        libtool \
        libusb-1.0-0-dev \
        pkg-config \
    && git clone --filter=blob:none https://github.com/knxd/knxd.git /tmp/knxd \
    && git -C /tmp/knxd checkout --detach "${KNXD_COMMIT}" \
    && cd /tmp/knxd \
    && sh bootstrap.sh \
    && ./configure --prefix=/usr \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf /tmp/knxd \
    && rm -rf /var/lib/apt/lists/*

COPY knxd.conf /etc/knxd/knxd.conf
COPY healthcheck.sh /usr/local/bin/knx-gateway-healthcheck
RUN chmod 0755 /usr/local/bin/knx-gateway-healthcheck

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
    CMD ["/usr/local/bin/knx-gateway-healthcheck"]

ENTRYPOINT ["knxd", "/etc/knxd/knxd.conf", "main"]
