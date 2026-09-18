"""Stable process bootstrap for the installed wynxo command."""

from __future__ import annotations


def main():
    """Install the product composition once, then delegate to the CLI core."""
    from . import runtime_compat  # noqa: F401
    from . import cli

    # Tests and embedders can replace cli.main. In that case bootstrap remains
    # a bare dispatcher and does not mutate classes in the host process.
    if getattr(cli.main, "__module__", "") == cli.__name__:
        from .product import install
        install()
    return cli.main()


if __name__ == "__main__":
    result = main()
    if isinstance(result, int):
        raise SystemExit(result)
