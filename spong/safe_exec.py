"""Safe command execution with timeout."""

import subprocess
import shlex
import logging

log = logging.getLogger(__name__)


def safe_exec(cmd: str, timeout: int = 30) -> list[str]:
    """Run cmd, return output lines. Returns [] on error."""
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += result.stderr
        return output.splitlines(keepends=True)
    except subprocess.TimeoutExpired:
        log.warning("safe_exec timeout: %s", cmd)
        return [f"[timeout after {timeout}s]\n"]
    except FileNotFoundError:
        log.warning("safe_exec command not found: %s", cmd.split()[0])
        return [f"[command not found: {cmd.split()[0]}]\n"]
    except Exception as e:
        log.error("safe_exec error running '%s': %s", cmd, e)
        return [f"[error: {e}]\n"]


def safe_exec_str(cmd: str, timeout: int = 30) -> str:
    """Run cmd, return output as a single string."""
    return "".join(safe_exec(cmd, timeout))
