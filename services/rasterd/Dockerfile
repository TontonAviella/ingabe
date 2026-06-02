FROM rust:1-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    clang \
    libgdal-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Cargo.toml Cargo.lock* ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libgdal32 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/target/release/mundi-rasterd /usr/local/bin/mundi-rasterd

ENV RUST_LOG=info
EXPOSE 8877
CMD ["mundi-rasterd"]
