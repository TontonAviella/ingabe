FROM rust:1-bookworm AS builder

ENV CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_HTTP_MULTIPLEXING=false \
    CARGO_HTTP_TIMEOUT=600 \
    CARGO_NET_RETRY=10

RUN set -eux; \
    for attempt in 1 2 3; do \
        apt-get clean; \
        rm -rf /var/lib/apt/lists/*; \
        apt-get update \
            -o Acquire::Retries=3 \
            -o Acquire::http::No-Cache=true \
            -o Acquire::https::No-Cache=true \
            -o Acquire::http::Pipeline-Depth=0 \
            -o Acquire::https::Pipeline-Depth=0 \
            && apt-get install -y --no-install-recommends \
            -o Acquire::Retries=3 \
            ca-certificates \
            pkg-config \
            && break; \
        if [ "$attempt" = "3" ]; then exit 1; fi; \
        sleep 5; \
    done; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Cargo.toml Cargo.lock* ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim

RUN set -eux; \
    for attempt in 1 2 3; do \
        apt-get clean; \
        rm -rf /var/lib/apt/lists/*; \
        apt-get update \
            -o Acquire::Retries=3 \
            -o Acquire::http::No-Cache=true \
            -o Acquire::https::No-Cache=true \
            -o Acquire::http::Pipeline-Depth=0 \
            -o Acquire::https::Pipeline-Depth=0 \
            && apt-get install -y --no-install-recommends \
            -o Acquire::Retries=3 \
            ca-certificates \
            && break; \
        if [ "$attempt" = "3" ]; then exit 1; fi; \
        sleep 5; \
    done; \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/target/release/mundi-geokernel /usr/local/bin/mundi-geokernel

ENV RUST_LOG=info
EXPOSE 8878
CMD ["mundi-geokernel"]
