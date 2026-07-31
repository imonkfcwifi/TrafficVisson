"""TrafficSuperVision's authenticated, loopback-only desktop bridge."""


def create_app(*args, **kwargs):
    """Import the heavy application graph only after runtime bootstrap."""

    from .main import create_app as application_factory

    return application_factory(*args, **kwargs)

__all__ = ["create_app"]
