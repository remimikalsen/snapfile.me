#!/usr/bin/env bash
set -e

if [ "$1" = "test-e2e" ]; then
    # Spin up the app in the background
    python app/app.py &
    APP_PID=$!

    echo "Waiting for app to start..."
    # Let the app bind to port 8080
    sleep 5

    # Now run only the end-to-end tests
    pytest -m e2e

elif [ "$1" = "test-unit" ]; then
    # Just run the unit tests (that do NOT require a running server)
    pytest -m "not e2e"
else
    # Default: run the app as normal
    exec python app/app.py
fi