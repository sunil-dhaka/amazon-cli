"""Amazon CLI - Terminal interface for Amazon.in."""

__version__ = "0.1.0"


def main():
    from amazon_cli.cli import cli

    cli()
