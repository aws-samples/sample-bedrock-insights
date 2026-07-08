from __future__ import annotations

import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError, ProfileNotFound
from rich.console import Console

from .cloudwatch import LOG_GROUP

console = Console()

# Curated set of the major commercial Bedrock regions, used as the default
# monitoring scope when no --region is given (so `bedrock-insights` covers the
# regions where Bedrock usage typically lives instead of just one).
MAJOR_REGIONS: tuple[str, ...] = (
    "us-east-1", "us-west-2", "eu-central-1", "eu-west-1",
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
)


def _make_session(region: str | None, profile: str | None) -> boto3.Session:
    """Create a boto3 Session, exiting with a readable error on credential/region failures."""
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as exc:
        console.print(f"[red]AWS profile not found:[/red] {exc}")
        sys.exit(1)
    except NoCredentialsError:
        console.print(
            "[red]No AWS credentials found.[/red] "
            "Configure them with [bold]aws configure[/bold] or set "
            "[bold]AWS_ACCESS_KEY_ID[/bold] / [bold]AWS_SECRET_ACCESS_KEY[/bold]."
        )
        sys.exit(1)


def make_client(region: str | None, profile: str | None):
    """Create a CloudWatch Logs boto3 client, exiting with a readable error on failure."""
    session = _make_session(region, profile)
    try:
        return session.client("logs")
    except NoRegionError:
        resolved = session.region_name
        if resolved:
            console.print(
                f"[red]CloudWatch Logs is not available in region: {resolved}[/red]\n"
                "Pass [bold]--region[/bold] with a supported region."
            )
        else:
            console.print(
                "[red]No AWS region configured.[/red] "
                "Pass [bold]--region[/bold] or set [bold]AWS_DEFAULT_REGION[/bold]."
            )
        sys.exit(1)


def parse_regions(region: str | None) -> list[str]:
    """Split a comma-separated --region value into a clean list (order-preserving)."""
    if not region:
        return []
    seen, out = set(), []
    for part in region.split(","):
        r = part.strip()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _default_session_region(profile: str | None) -> str | None:
    """Best-effort read of the session's configured default region (no error)."""
    try:
        return boto3.Session(profile_name=profile).region_name
    except Exception:
        return None


def default_regions(profile: str | None) -> list[str]:
    """Regions to monitor when none are given: the major Bedrock regions, with
    the session's own configured region included first if it isn't already one."""
    regions = list(MAJOR_REGIONS)
    own = _default_session_region(profile)
    if own and own not in regions:
        regions.insert(0, own)
    return regions


def make_clients(region: str | None, profile: str | None) -> list[tuple[str, object]]:
    """Create a CloudWatch Logs client per region. Returns [(region, client), ...].

    Accepts a single region or a comma-separated list. When none is given,
    defaults to the major Bedrock regions (see default_regions()).
    """
    regions = parse_regions(region) or default_regions(profile)
    return [(r, make_client(r, profile)) for r in regions]


def make_bedrock_client(region: str | None, profile: str | None):
    """Create an Amazon Bedrock boto3 client for model/profile discovery."""
    session = _make_session(region, profile)
    try:
        return session.client("bedrock")
    except NoRegionError:
        resolved = session.region_name
        if resolved:
            console.print(
                f"[red]Amazon Bedrock is not available in region: {resolved}[/red]\n"
                "Pass [bold]--region[/bold] with a supported region."
            )
        else:
            console.print(
                "[red]No AWS region configured.[/red] "
                "Pass [bold]--region[/bold] or set [bold]AWS_DEFAULT_REGION[/bold]."
            )
        sys.exit(1)


def handle_client_error(exc: ClientError) -> None:
    """Print a human-readable message for a CloudWatch ClientError."""
    code = exc.response["Error"]["Code"]
    msg  = exc.response["Error"]["Message"]
    if code == "ResourceNotFoundException":
        console.print(f"[yellow]Log group not found:[/yellow] {LOG_GROUP}")
        console.print(
            "[dim]Run [bold]bedrock-insights --setup[/bold] to enable "
            "Bedrock model invocation logging.[/dim]"
        )
    elif code in ("AccessDeniedException", "UnauthorizedException"):
        console.print(f"[red]Access denied:[/red] {msg}")
        console.print(
            "[dim]Your credentials need [bold]logs:FilterLogEvents[/bold] "
            f"on [bold]{LOG_GROUP}[/bold].[/dim]"
        )
    else:
        console.print(f"[red]AWS error ({code}):[/red] {msg}")
