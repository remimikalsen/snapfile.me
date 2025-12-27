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
    cd ../tests
    pytest -m e2e

elif [ "$1" = "test-unit" ]; then
    # Just run the unit tests (that do NOT require a running server)
    # and capture output for CI processing in tests.yaml

    # Change to the tests directory
    cd tests

    # Run unit tests with coverage and capture all output.
    PYTEST_OUTPUT=$(pytest --cov-report=xml:coverage.xml -m "not e2e" 2>&1)
    PYTEST_EXIT_CODE=$?

    # Generate the coverage badge SVG and capture its output.
    BADGE_OUTPUT=$(python generate_coverage_badge.py print)

    # Read the contents of the coverage.xml file if it exists.
    if [ -f coverage.xml ]; then
        COVERAGE_XML=$(cat coverage.xml)
    else
        COVERAGE_XML=""
    fi

    # Print outputs with clear delimiters for GitHub Actions to parse.
    echo "-----BEGIN PYTEST OUTPUT-----"
    echo "$PYTEST_OUTPUT"
    echo "-----END PYTEST OUTPUT-----"

    echo "-----BEGIN BADGE SVG-----"
    echo "$BADGE_OUTPUT"
    echo "-----END BADGE SVG-----"

    echo "-----BEGIN COVERAGE XML-----"
    echo "$COVERAGE_XML"
    echo "-----END COVERAGE XML-----"

    # Exit with the same code as pytest.
    exit $PYTEST_EXIT_CODE

elif [ "$1" = "lint" ]; then
    cd tests
    flake8 ../app

else
    # Default: run the app as normal
    cd app
    exec python app.py
fi
