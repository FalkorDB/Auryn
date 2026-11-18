# Multi-stage build for the API service. A `builder` stage installs deps; the slim `final` stage
# copies them in and ships the app code -- so the final image carries no build toolchain.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim AS final
WORKDIR /app
COPY --from=builder /install /usr/local
COPY . /app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "app.main"]
