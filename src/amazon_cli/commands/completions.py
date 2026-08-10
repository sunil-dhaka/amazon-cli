"""amz completions command -- print a shell completion script."""

import click
from click.shell_completion import get_completion_class

from amazon_cli.errors import InputError

#: Where each shell wants the incantation, and what the incantation is.
#: Sourcing the generated script directly is faster than click's `eval` form
#: because it skips a subprocess on every new shell, so that is what we hint at.
_INSTALL_HINT = {
    "bash": 'Add to ~/.bashrc:  eval "$(_AMZ_COMPLETE=bash_source amz)"',
    "zsh": 'Add to ~/.zshrc:  eval "$(_AMZ_COMPLETE=zsh_source amz)"',
    "fish": "Save as ~/.config/fish/completions/amz.fish:  amz completions fish > ~/.config/fish/completions/amz.fish",
}


@click.command("completions")
@click.argument("shell", type=click.Choice(sorted(_INSTALL_HINT)))
def completions(shell):
    """Print the shell completion script for SHELL.

    The output is a script: redirect it to a file, or `eval` it.
    """
    # Imported here: cli.py imports this module, so a module-level import back
    # into cli.py would be circular.
    from amazon_cli.cli import cli

    completion_cls = get_completion_class(shell)
    if completion_cls is None:  # pragma: no cover -- Choice already guards this
        raise InputError(f"No completion support for shell {shell!r}.")

    script = completion_cls(cli, {}, "amz", "_AMZ_COMPLETE").source()
    # A leading comment keeps the output directly sourceable in all three shells.
    click.echo(f"# {_INSTALL_HINT[shell]}")
    click.echo(script.strip())
