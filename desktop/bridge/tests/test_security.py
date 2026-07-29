import pytest

from app.security import validate_public_source_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/video.mp4",
        "http://10.10.1.2/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/video.mp4",
        "https://user:password@example.com/video.mp4",
        "https://example.com:8443/video.mp4",
    ],
)
def test_url_validator_rejects_local_or_credentialed_targets(url):
    with pytest.raises(ValueError):
        validate_public_source_url("direct_url", url)


@pytest.mark.parametrize(
    ("source_type", "url"),
    [
        ("google_drive", "https://drive.google.com/file/d/example/view"),
        ("dropbox", "https://www.dropbox.com/s/example/video.mp4"),
        ("onedrive", "https://1drv.ms/v/example"),
        ("youtube", "https://youtu.be/example"),
        ("direct_url", "https://media.example.com/video.mp4"),
    ],
)
def test_url_validator_accepts_expected_public_provider_hosts(source_type, url):
    assert validate_public_source_url(source_type, url)

