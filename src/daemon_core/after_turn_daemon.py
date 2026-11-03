"""Executable composition root for the neutral after-turn LaunchAgent."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
configured_env = os.getenv("DAEMON_CORE_ENV_FILE", "").strip()
load_dotenv(
    Path(configured_env).expanduser()
    if configured_env
    else PROJECT_ROOT / ".env"
)


def _build_service():
    from integrations.after_turn_service.hermes_observer import (
        HermesAfterTurnObserver,
    )
    from integrations.after_turn_service.openclaw_observer import (
        OpenClawAfterTurnObserver,
    )
    from daemon_core.after_turn_service import (
        AfterTurnService,
        configured_after_turn_user_ids,
    )

    user_ids = configured_after_turn_user_ids()
    observers = {
        "hermes": HermesAfterTurnObserver(),
        "openclaw": OpenClawAfterTurnObserver(),
    }
    return AfterTurnService(
        user_ids=user_ids,
        batch_observers=observers,
    )


def run_daemon() -> None:
    """Run the neutral worker after its environment has been configured."""

    logging.basicConfig(
        level=os.getenv("DAEMON_CORE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _build_service().run(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daemon Harness neutral after-turn service",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        run_daemon()
        return

    from daemon_core.after_turn_service import (
        read_after_turn_service_status,
    )

    report = read_after_turn_service_status(
        expected_revision=os.getenv(
            "DAEMON_CORE_RUNTIME_REVISION",
            "",
        ),
    )
    print("ready" if report["ok"] else report["code"])
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
