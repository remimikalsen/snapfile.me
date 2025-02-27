import pytest
from pathlib import Path
from playwright.sync_api import Page


@pytest.mark.e2e
def test_upload_download_quota(page: Page, tmp_path: Path, base_url: str):
    """
    This test will:
    1. Go to the main page and read the current quota.
    2. Upload a file.
    3. Check that the quota is reduced by one.
    4. Follow the landing page link to download the file.
    5. Check the file is downloaded (via the Playwright download API).
    6. Check that the final quota is still consistent (and the file can't be downloaded again).
    """

    #
    # 1. Check initial quota
    #
    initial_quota = get_quota_info(page, base_url)
    print(f"Initial quota from /check-limit = {initial_quota}")

    #
    # 2. Go to home page and upload
    #
    page.goto(base_url, timeout=10_000)

    # Set the file to be uploaded; a small dummy file in tmp_path
    test_file = tmp_path / "dummy_test_file.txt"
    test_file.write_text("Hello SnapFile.me!")

    # Use the file input to upload
    page.set_input_files("#file-input", str(test_file))

    # Wait for success or error
    # The app sets success text into #success-message upon success
    success_selector = "#success-message"
    error_selector = "#error-message"

    # Wait for either success or error to appear
    page.wait_for_selector(f"{success_selector}, {error_selector}")

    # Check if it's a success
    if page.is_visible(error_selector):
        error_text = page.inner_text(error_selector)
        pytest.fail(f"Upload failed with error: {error_text}")

    # If success, parse the link from success content
    success_text = page.inner_text(success_selector)
    print("Success text content:\n", success_text)

    # Your success message prints something like:
    # Select that <a> that points to the "landing" URL:
    landing_link_el = page.locator(".success-link").first
    landing_url = landing_link_el.get_attribute("href")
    assert landing_url, "Could not find the landing page link after upload."

    print(f"Landing URL: {landing_url}")

    #
    # 3. Check that the quota was reduced
    #
    new_quota = get_quota_info(page, base_url)
    print(f"Quota after upload = {new_quota}")

    assert new_quota == initial_quota - 1, (
        f"Expected the quota to be (initial - 1) but got {new_quota}. "
        f"Initial was {initial_quota}."
    )

    #
    # 4. Download the file from the landing page
    #
    page.goto(landing_url, timeout=10_000)
    # The direct download link is #download-button => On click, it triggers the actual download
    with page.expect_download() as download_info:
        page.click("#download-button")
    download = download_info.value
    # Save it to a known location:
    download_path = str(tmp_path / "downloaded_file.txt")
    download.save_as(download_path)
    print(f"Downloaded file is saved to {download_path}")

    # Validate file contents if you want:
    downloaded_text = Path(download_path).read_text()
    assert (
        downloaded_text == "Hello SnapFile.me!"
    ), "Downloaded file content is not matching the uploaded file content."

    # 5. Confirm the file is no longer available (since it is single-download)

    # Wait a moment for server to have a change to clean up
    page.wait_for_timeout(6000)

    # Attempt to load the same download link directly:
    response = page.goto(landing_url, timeout=10_000)
    # Now expect that the content indicates the file is not available
    assert response.status == 404, f"Expected 404 status but got {response.status}"

    #
    # 6. Final quota check
    #
    final_quota = get_quota_info(page, base_url)
    print(f"Final quota from /check-limit after download = {final_quota}")

    # We expect final_quota to be the same as new_quota at this point
    assert (
        final_quota == new_quota
    ), f"Expected final quota to remain {new_quota}, got {final_quota}."


def get_quota_info(page: Page, base_url: str) -> int:
    """
    Helper function to fetch JSON from /check-limit
    and return the 'quota_left' integer.
    """
    with page.expect_response(f"{base_url}/check-limit") as response_info:
        page.goto(f"{base_url}/check-limit", wait_until="networkidle")
    response = response_info.value
    assert response.ok, f"Failed to fetch /check-limit: status {response.status}"
    data = response.json()
    # data should look like:
    # {
    #   "limit_reached": false,
    #   "quota_left": 4,
    #   "quota_renewal_hours": ...,
    #   "quota_renewal_minutes": ...
    # }
    return data.get("quota_left", 0)
