"""Kindred CLI facade and compatibility surface."""

from __future__ import annotations

import click

from kindred import __version__
from kindred.cli_card import card, card_activate  # noqa: F401
from kindred.cli_common import (  # noqa: F401
    SCENARIO_CHOICES,
    _configure_observability,
    _echo_cold_start,
    _load_cli_config,
    _now_iso,
    _require_db,
    _require_mouth_host_binding,
    _require_mouth_host_runtime,
    _require_resident_commit,
)
from kindred.cli_daemon import (  # noqa: F401
    _doctor_preflight,
    _platform_service,
    _require_relationship_runtime,
    doctor,
    logs,
    run,
    start,
    status,
    stop,
)
from kindred.cli_db import db, db_bootstrap, db_migrate, db_status  # noqa: F401
from kindred.cli_dream import (  # noqa: F401
    _validate_dream_date,
    _yesterday_date,
    dream,
    dream_run,
)
from kindred.cli_inventory import inventory, inventory_add, inventory_import  # noqa: F401
from kindred.cli_serve import serve
from kindred.cli_soul import (  # noqa: F401
    _soul_layout,
    soul,
    soul_list_snapshots,
    soul_rollback,
)
from kindred.cli_tick import (  # noqa: F401
    _build_client_graph,
    _echo_tick_result,
    _run_mock_tick,
    _run_real_llm_tick,
    _run_scenario_tick,
    tick,
)
from kindred.mouth_host.install import install, uninstall


@click.group()
@click.version_option(__version__, prog_name="kindred")
def cli() -> None:
    """Kindred — 一个能呼吸的 AI 伴侣框架。

    呼吸 = SQLite 里真的有一行 tick 数据。
    """


for command in (
    tick,
    run,
    doctor,
    start,
    stop,
    status,
    logs,
    db,
    inventory,
    card,
    soul,
    dream,
    serve,
    install,
    uninstall,
):
    cli.add_command(command)


def main() -> None:
    """Entry point for `python -m kindred` and `kindred` console script."""
    cli()


if __name__ == "__main__":
    main()
