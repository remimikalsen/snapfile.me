# Testing Snapfile

- For developers, run `pip install -r requirements-tests.txt` file. You get flake8, black and testing tools.
  - Run `flake8 ../app` in the tests directory to run linting
  - Run `black ../app` in the tests directory to fix code automatically

- To run tests, install dev-requirements and:
  - `pytest -m "e2e"` for Playwright tests; but make sure your app is running on localhost:8080 first.
  - `pytest -m "no e2e"` for Unit tests. The app doesn't need to run

- You will get automatic coverage reports when running pytest

