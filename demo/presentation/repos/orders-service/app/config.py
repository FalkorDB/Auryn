"""Orders-service configuration."""

import os


def region():
    return os.getenv("REGION", "us")
