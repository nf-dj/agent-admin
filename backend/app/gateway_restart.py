"""Gateway restart helper.

`openclaw config patch` writes the file atomically but the running gateway
doesn't pick up channel changes (new Matrix/Telegram accounts, etc.) until
it restarts. So every time we mutate channel config we need to bounce
`openclaw-gateway.service` in the bots user's systemd session.

We debounce restarts: if a second sync request comes in within DEBOUNCE_S of
the previous one, we just queue a single restart for after the window closes.
This keeps batched edits / multi-step creates from causing N restarts.
"""
from __future__ import annotations
import logging
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# Coalesce restart requests inside this window (seconds)
DEBOUNCE_S = 4.0

_lock = threading.Lock()
_pending_timer: threading.Timer | None = None
_last_restart_at: float = 0.0


def _do_restart() -> None:
    """Actually invoke `systemctl --user restart openclaw-gateway`."""
    global _pending_timer, _last_restart_at
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "openclaw-gateway"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            _last_restart_at = time.time()
            log.info("openclaw-gateway restarted successfully")
        else:
            log.error("openclaw-gateway restart failed (rc=%s): stderr=%s",
                      result.returncode, result.stderr.strip())
    except FileNotFoundError:
        log.error("systemctl not found \u2014 cannot restart openclaw-gateway")
    except subprocess.TimeoutExpired:
        log.error("openclaw-gateway restart timed out")
    except Exception:
        log.exception("Unexpected error restarting openclaw-gateway")
    finally:
        with _lock:
            _pending_timer = None


def schedule_gateway_restart() -> None:
    """Queue a debounced gateway restart.

    Safe to call multiple times in quick succession \u2014 only one restart will
    actually happen per DEBOUNCE_S window.
    """
    global _pending_timer
    with _lock:
        if _pending_timer is not None:
            # Restart already scheduled; just let it cover this request too.
            log.debug("gateway restart already pending; coalescing")
            return
        timer = threading.Timer(DEBOUNCE_S, _do_restart)
        timer.daemon = True
        _pending_timer = timer
        timer.start()
        log.info("scheduled gateway restart in %.1fs", DEBOUNCE_S)
