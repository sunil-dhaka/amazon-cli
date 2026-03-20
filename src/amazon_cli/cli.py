"""CLI entry point -- click group and global options."""

import click

from amazon_cli import __version__
from amazon_cli.commands.compare import compare
from amazon_cli.commands.product import product
from amazon_cli.commands.reviews import reviews
from amazon_cli.commands.search import search


@click.group()
@click.version_option(__version__, prog_name="amz")
def cli():
    """amz -- Amazon.in in your terminal."""


cli.add_command(search)
cli.add_command(product)
cli.add_command(compare)
cli.add_command(reviews)
