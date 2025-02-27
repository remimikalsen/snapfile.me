#!/usr/bin/env bash
set -e

if [ "$1" = "test-e2e" ]; then
    # Spin up the app in the background
    cd app
    python app.py &
    APP_PID=$!

    echo "Waiting for app to start..."
    # Let the app bind to port 8080
    sleep 5

    # Set the base URL for the tests run properly in Docker
    export BASE_URL="http://localhost:8080"

    # Now run only the end-to-end tests
    cd ..
    pytest -m e2e

elif [ "$1" = "test-unit" ]; then
    # Just run the unit tests (that do NOT require a running server)
    pytest -m "not e2e"

elif [ "$1" = "lint" ]; then
    flake8 .    

else
    # Default: run the app as normal
    cd app
    exec python app.py
fi