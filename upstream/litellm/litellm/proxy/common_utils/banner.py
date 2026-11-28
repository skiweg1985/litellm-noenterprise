from typing import Final

# LiteLLM ASCII banner with the noenterprise subtitle
LITELLM_BANNER: Final = """   ██╗     ██╗████████╗███████╗██╗     ██╗     ███╗   ███╗
   ██║     ██║╚══██╔══╝██╔════╝██║     ██║     ████╗ ████║
   ██║     ██║   ██║   █████╗  ██║     ██║     ██╔████╔██║
   ██║     ██║   ██║   ██╔══╝  ██║     ██║     ██║╚██╔╝██║
   ███████╗██║   ██║   ███████╗███████╗███████╗██║ ╚═╝ ██║
   ╚══════╝╚═╝   ╚═╝   ╚══════╝╚══════╝╚══════╝╚═╝     ╚═╝
   ────────────── noenterprise · sudo-ai.de ──────────────"""


def show_banner():
    """Display the LiteLLM CLI banner."""
    try:
        import click

        click.echo(f"\n{LITELLM_BANNER}\n")
    except ImportError:
        print("\n")  # noqa: T201
