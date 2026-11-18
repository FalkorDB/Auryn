# The worker shares the SAME base image as the API (python:3.12-slim) -- in the graph that base
# collapses to ONE Image node both services point at (the shared-base signal).
FROM python:3.12-slim
WORKDIR /app
COPY . /app
CMD ["python", "-m", "worker.main"]
