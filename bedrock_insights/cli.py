from __future__ import annotations

import click

from . import __version__
from .client import make_bedrock_client, make_clients
from .pricing import set_debug
from .setup_cmd import auto_setup, run_setup
from .web import run_web


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="bedrock-insights")
@click.option("--web",  is_flag=True,
              help="Serve the live web dashboard (this is the default action).")
@click.option("--port", type=int, default=8765, metavar="PORT",
              help="Port for the web dashboard (default: 8765).")
@click.option("--host", default="127.0.0.1", metavar="HOST",
              help="Bind address for the web dashboard (default: 127.0.0.1). "
                   "Use 0.0.0.0 to expose on your network — see the security warning.")
@click.option("--region",  default=None, envvar="AWS_DEFAULT_REGION",
              help="AWS region(s) to monitor. Comma-separate for multi-region "
                   "(e.g. us-east-1,us-west-2). Omit to monitor the major Bedrock regions.")
@click.option("--profile", default=None, envvar="AWS_PROFILE",
              help="AWS named profile.")
@click.option("--token", default=None, envvar="BEDROCK_INSIGHTS_TOKEN", metavar="TOKEN",
              help="Require this token to access the dashboard (cookie, ?token=, or Bearer header). "
                   "Recommended when binding to a non-localhost --host.")
@click.option("--no-db", "no_db", is_flag=True,
              help="Disable on-disk persistence (run in-memory only; history is lost on restart).")
@click.option("--no-content", "no_content", is_flag=True, envvar="BEDROCK_INSIGHTS_NO_CONTENT",
              help="Disable viewing prompt/response bodies in the Recent tab "
                   "(recommended for shared/network-exposed deployments).")
@click.option("--debug", is_flag=True,
              help="Print diagnostic output (e.g. why pricing lookups failed) to stderr.")
@click.option("--setup", is_flag=True, is_eager=True,
              help="Run one-time setup to enable Bedrock model invocation logging.")
@click.option("--auto-setup", "auto_setup_flag", is_flag=True,
              help="Before launching, automatically enable Bedrock model invocation "
                   "logging (if not already on) in every region about to be monitored. "
                   "Idempotent, but requires IAM write permissions (iam:CreateRole, "
                   "logs:CreateLogGroup, bedrock:PutModelInvocationLoggingConfiguration) "
                   "in each of those regions — off by default, see README.")
@click.option("--retention", type=int, default=None, metavar="DAYS",
              help="Set log retention in days when running --setup (0 = never expire). Omit to leave existing policy unchanged.")
def main(
    web: bool,
    port: int,
    host: str,
    region: str | None,
    profile: str | None,
    token: str | None,
    no_db: bool,
    no_content: bool,
    debug: bool,
    setup: bool,
    auto_setup_flag: bool,
    retention: int | None,
) -> None:
    """Monitor AWS Bedrock token usage and costs via a live web dashboard.

    \b
    The dashboard is the single place to view and configure everything:
    time window, region view, spend threshold, Slack/webhook alerts, refresh
    interval, and JSON/CSV export are all controlled from the web UI.

    \b
    Launch options (set when starting the server) cover only what the UI
    cannot change at runtime: the bind address, which regions to monitor, and
    the AWS profile.

    \b
    Examples
    --------
      bedrock-insights                          # launch the dashboard (major regions)
      bedrock-insights --region us-east-1,us-west-2   # monitor specific regions
      bedrock-insights --host 0.0.0.0 --port 9000     # change the bind address
      bedrock-insights --setup                  # one-time setup wizard
      bedrock-insights --setup --retention 90   # setup + 90-day log retention
      bedrock-insights --auto-setup              # enable logging in every monitored region, then launch
    """
    if debug:
        set_debug(True)

    if setup:
        run_setup(region, profile, retention)
        return

    clients        = make_clients(region, profile)
    # Use the first region for Bedrock model/pricing discovery.
    bedrock_client = make_bedrock_client(clients[0][0], profile)

    if region is None and len(clients) > 1:
        click.echo(
            "No --region given; monitoring major Bedrock regions: "
            + ", ".join(r for r, _ in clients)
            + ".  Pass --region to narrow.",
            err=True,
        )

    if auto_setup_flag:
        auto_setup([r for r, _ in clients], profile)

    run_web(clients, bedrock_client, host, port, token,
            persist=not no_db, show_content=not no_content)
