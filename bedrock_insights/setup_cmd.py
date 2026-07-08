from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import ClientError, NoRegionError
from rich.console import Console
from rich.panel import Panel

console = Console()

LOG_GROUP = "/aws/bedrock/model-invocations"
ROLE_NAME = "AmazonBedrockModelInvocationLoggingRole"

_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

_PERMISSION_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams",
            ],
            "Resource": f"arn:aws:logs:*:*:log-group:{LOG_GROUP}:*",
        }
    ],
}


def run_setup(region: str | None, profile: str | None, retention: int | None = None) -> None:
    """Run setup for one or more (comma-separated) regions.

    With no --region, defaults to the same major Bedrock regions the
    dashboard monitors by default (rather than just the current session's
    region), so a plain `--setup` covers what a plain launch will look at.
    """
    from .client import MAJOR_REGIONS, parse_regions
    regions = parse_regions(region) or list(MAJOR_REGIONS)
    for i, r in enumerate(regions):
        if len(regions) > 1:
            console.print(f"\n[bold]══ Region {i + 1}/{len(regions)}: {r} ══[/bold]")
        _run_setup_region(r, profile, retention)


def _auto_setup_one(region: str, profile: str | None) -> str:
    """Run setup for one region into a private buffer; return the rendered output.

    Runs off the main thread (see auto_setup), so it must not touch the shared
    module-level `console` — each call gets its own Console writing to an
    in-memory buffer, which the caller prints once the region is done. This
    keeps concurrent regions' output from interleaving on the terminal.
    """
    buf = io.StringIO()
    region_console = Console(file=buf, force_terminal=True, width=100)
    try:
        _run_setup_region(region, profile, retention=None, out=region_console)
    except Exception as exc:  # noqa: BLE001 — never let one region's crash break the batch
        region_console.print(f"[yellow]--auto-setup: skipped {region} after an unexpected error:[/yellow] {exc!r}")
    return buf.getvalue()


def auto_setup(regions: list[str], profile: str | None) -> None:
    """Best-effort, quiet setup for each region the dashboard is about to monitor.

    Used by `--auto-setup`: enables Bedrock invocation logging in any region
    that doesn't already have it configured. Idempotent (skips regions that
    are already set up) and non-fatal — a failure in one region (e.g. missing
    IAM permissions) is reported and skipped rather than aborting startup.

    Regions run concurrently (new-role creation has a ~7s IAM-propagation wait,
    so running N regions sequentially could otherwise add tens of seconds to
    every launch); each region's output is buffered and flushed in order so
    concurrent regions don't interleave on the terminal.
    """
    if not regions:
        return
    console.print(
        f"[dim]--auto-setup: checking Bedrock invocation logging in "
        f"{len(regions)} region(s) (in parallel)…[/dim]"
    )
    with ThreadPoolExecutor(max_workers=min(8, len(regions))) as pool:
        # Submit in order but read results in submission order (not completion
        # order) so output stays deterministic run to run.
        futures = [pool.submit(_auto_setup_one, r, profile) for r in regions]
        for r, fut in zip(regions, futures):
            console.print(f"\n[bold]── {r} ──[/bold]")
            console.file.write(fut.result())
            console.file.flush()


def _run_setup_region(region: str | None, profile: str | None, retention: int | None = None,
                      out: Console | None = None) -> None:
    console = out or globals()["console"]
    session = boto3.Session(profile_name=profile, region_name=region)
    resolved_region = session.region_name
    try:
        bedrock = session.client("bedrock")
        logs    = session.client("logs")
        iam     = session.client("iam")
        sts     = session.client("sts")
    except NoRegionError:
        if resolved_region:
            console.print(
                f"[red]AWS Bedrock is not available in region: {resolved_region}[/red]\n"
                "Pass [bold]--region[/bold] with a supported region, e.g. "
                "[bold]us-east-1[/bold] or [bold]eu-west-1[/bold]."
            )
        else:
            console.print(
                "[red]No AWS region configured.[/red] "
                "Pass [bold]--region[/bold] or set [bold]AWS_DEFAULT_REGION[/bold]."
            )
        return

    try:
        identity       = sts.get_caller_identity()
        account_id     = identity["Account"]
        actual_region  = session.region_name or "unknown"
    except ClientError as exc:
        console.print(f"[red]Cannot determine AWS identity:[/red] {exc}")
        return

    console.print(Panel.fit(
        f"[bold]Bedrock Insights — One-time Setup[/bold]\n"
        f"Account: [cyan]{account_id}[/cyan]   Region: [cyan]{actual_region}[/cyan]",
        border_style="blue",
    ))

    # ── 1. Check whether logging is already configured ──────────────────────
    try:
        resp   = bedrock.get_model_invocation_logging_configuration()
        config = resp.get("loggingConfig", {})
        cw     = config.get("cloudWatchConfig", {})
        if cw.get("logGroupName"):
            console.print(
                "\n[green]✓[/green] Model invocation logging is already enabled.\n"
                f"  Log group : [cyan]{cw['logGroupName']}[/cyan]\n"
                f"  Role ARN  : [cyan]{cw.get('roleArn', 'n/a')}[/cyan]"
            )
            _apply_retention(logs, retention, console)
            return
    except ClientError as exc:
        console.print(
            f"[yellow]Could not read existing logging config[/yellow] "
            f"({exc.response['Error']['Code']}); continuing with setup."
        )

    # ── 2. Create CloudWatch log group ───────────────────────────────────────
    console.print(f"\n  [dim]1/3[/dim] Creating log group [cyan]{LOG_GROUP}[/cyan] ...", end=" ")
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        console.print("[green]✓[/green]")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceAlreadyExistsException":
            console.print("[dim]already exists[/dim]")
        else:
            console.print(f"[red]failed[/red] — {exc.response['Error']['Message']}")
            return

    _apply_retention(logs, retention, console)

    # ── 3. Create / verify IAM role ──────────────────────────────────────────
    role_arn: str | None = None
    console.print(f"  [dim]2/3[/dim] Creating IAM role [cyan]{ROLE_NAME}[/cyan] ...", end=" ")
    try:
        resp     = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(_TRUST_POLICY),
            Description="Allows Amazon Bedrock to write model invocation logs to CloudWatch Logs",
        )
        role_arn = resp["Role"]["Arn"]
        console.print("[green]✓[/green]")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
            console.print("[dim]already exists[/dim]")
        else:
            console.print(f"[red]failed[/red] — {exc.response['Error']['Message']}")
            _print_manual_steps(account_id, actual_region, console)
            return

    # Attach inline permission policy
    try:
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName="BedrockCloudWatchLogsPolicy",
            PolicyDocument=json.dumps(_PERMISSION_POLICY),
        )
    except ClientError as exc:
        console.print(
            f"  [yellow]Warning: could not attach policy to role:[/yellow] "
            f"{exc.response['Error']['Message']}"
        )

    # IAM changes need a moment to propagate before Bedrock can assume the role.
    # This one-off delay during interactive setup is intentional (eventual
    # consistency in IAM), not a leftover debugging sleep.
    time.sleep(7)  # nosemgrep: arbitrary-sleep

    # ── 4. Enable model invocation logging ───────────────────────────────────
    console.print("  [dim]3/3[/dim] Enabling model invocation logging ...", end=" ")
    try:
        bedrock.put_model_invocation_logging_configuration(
            loggingConfig={
                "cloudWatchConfig": {
                    "logGroupName": LOG_GROUP,
                    "roleArn": role_arn,
                },
                "textDataDeliveryEnabled":      True,
                "imageDataDeliveryEnabled":     False,
                "embeddingDataDeliveryEnabled": False,
            }
        )
        console.print("[green]✓[/green]")
    except ClientError as exc:
        console.print(f"[red]failed[/red] — {exc.response['Error']['Message']}")
        return

    console.print(
        "\n[bold green]✓ Setup complete![/bold green]\n"
        "Bedrock will now log every model invocation to CloudWatch.\n"
        "[dim]First entries appear within ~30 s of your next Bedrock call.[/dim]\n"
        "\nRun [bold]bedrock-insights[/bold] to see your usage."
    )


def _apply_retention(logs, retention: int | None, console: Console) -> None:
    if retention is None:
        console.print(
            "  [dim]Tip: re-run with [bold]--retention DAYS[/bold] to set a retention policy, "
            "or [bold]--retention 0[/bold] to remove one.[/dim]"
        )
    elif retention == 0:
        try:
            logs.delete_retention_policy(logGroupName=LOG_GROUP)
            console.print("  [dim]Retention policy removed — logs will never expire.[/dim]")
        except ClientError as exc:
            console.print(f"  [yellow]Warning: could not remove retention policy:[/yellow] {exc.response['Error']['Message']}")
    else:
        try:
            logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=retention)
            console.print(
                f"  [dim]Retention set to [bold]{retention}[/bold] days. "
                "To remove it, re-run with [bold]--retention 0[/bold].[/dim]"
            )
        except ClientError as exc:
            console.print(f"  [yellow]Warning: could not set retention policy:[/yellow] {exc.response['Error']['Message']}")


def _print_manual_steps(account_id: str, region: str, console: Console) -> None:
    console.print(
        "\n[yellow]Insufficient permissions to create the IAM role automatically.[/yellow]\n"
        "Ask your admin to create a role named [bold]AmazonBedrockModelInvocationLoggingRole[/bold] with:\n"
    )
    console.print("[bold]Trust policy:[/bold]")
    console.print_json(json.dumps(_TRUST_POLICY, indent=2))
    console.print("\n[bold]Permission policy:[/bold]")
    console.print_json(json.dumps(_PERMISSION_POLICY, indent=2))
    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    logging_cfg = json.dumps({
        "cloudWatchConfig": {"logGroupName": LOG_GROUP, "roleArn": role_arn},
        "textDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "embeddingDataDeliveryEnabled": False,
    })
    console.print(
        "\nThen run:\n"
        "  aws bedrock put-model-invocation-logging-configuration \\\n"
        "    --logging-config '" + logging_cfg + "'"
    )
