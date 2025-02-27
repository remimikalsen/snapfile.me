import sys
import os
import pytest

# Get the project root (one level up from tests/)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture(scope="session")
def base_url():
    # Default to 127.0.0.1:8080 for plain server runs (windows, linux)
    # but this can be overridden when for exmaple used in Docker
    return os.getenv("BASE_URL", "http://127.0.0.1:8080")
