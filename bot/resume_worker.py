"""
resume_worker.py
~~~~~~~~~~~~~~~~
The separate process a resume is read in: a short-lived child, and the
bot-side function that runs one.

Parsing a PDF means running a large parser over a file a stranger chose, so
the bot never does it in its own process. `run_worker` pipes the bytes to a
fresh interpreter started with `-I -B` (no PYTHON* variables, no user site,
no script directory on the path, no bytecode written), an environment holding
only PATH and LANG — so no bot token or API key is there to leak — stderr
discarded, and a 20-second kill. The child lowers its own resource limits so
that not one byte can reach a file and a crash cannot leave a core dump, then
answers on stdout with closed-vocabulary JSON, which the parent validates again
rather than trusting.

It is not confined beyond that. The child runs as the bot's own user, in the
bot's working directory, and can read any file that user can (`.env` and the
bot's databases among them) and open a socket. The claim is the narrow one above:
no key in its environment, no byte in a file, a kill on time. README.md's
privacy section says exactly that much and no more.

The same file is both halves. As a script it is the child (`main`); imported,
it is the parent (`run_worker`). That is why its module scope imports only the
standard library: when it runs as the child, module scope executes before
`main` has put this directory on `sys.path` (`-I` removes it), so a top-level
`import resume_parse` would fail before the fix could run. Both halves import
it inside the function instead.

Nothing from the resume is written to disk or logged, on either side.
"""

import asyncio
import json
import os
import signal
import sys
from datetime import date
from pathlib import Path

WORKER_PATH: Path = Path(__file__).resolve()
TIMEOUT_S = 20.0
MAX_CONCURRENT = 2
BUSY_WAIT_S = 10.0
MAX_STDOUT = 65_536

#: The child's ceilings. CPU seconds sit under TIMEOUT_S so the kernel stops a
#: runaway parse first; 1 GiB of address space is refused on macOS, and ignored.
_CPU_SECONDS = 15
_ADDRESS_SPACE = 1024 * 1024 * 1024

#: (loop, limit, semaphore), remade when either changes; see `_semaphore`.
_held: "tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None" = None


def _semaphore() -> asyncio.Semaphore:
    """
    The semaphore capping concurrent workers, for the running loop.

    Remade whenever the running loop is not the one it was made in or
    MAX_CONCURRENT has changed. The bot has one loop, so this is one semaphore;
    tests run each case in its own loop, and a semaphore that has had waiters in
    one loop raises "bound to a different event loop" in the next.
    """
    global _held
    loop = asyncio.get_running_loop()
    if _held is None or _held[0] is not loop or _held[1] != MAX_CONCURRENT:
        _held = (loop, MAX_CONCURRENT, asyncio.Semaphore(MAX_CONCURRENT))
    return _held[2]


def worker_env() -> dict[str, str]:
    """Everything the child may see of the environment: a PATH and a locale."""
    return {"PATH": os.defpath, "LANG": "C.UTF-8"}


# ------------------------------------------------------------------ the child

def _limit_resources() -> None:
    """
    Each limit in its own `try`, so one the platform refuses (RLIMIT_AS on
    macOS raises ValueError) cannot stop the others from being set.
    """
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        # RLIMIT_FSIZE does not cover core files, and a core file of this
        # process would hold the resume.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_ADDRESS_SPACE, _ADDRESS_SPACE))
    except (ValueError, OSError, AttributeError):
        pass


def _arguments(parser, argv: list[str]) -> tuple[str, date]:
    """(kind, today) from the command line. Wrong ones are the parent's bug, not the file's."""
    try:
        return argv[1], date.fromisoformat(argv[2])
    except (IndexError, TypeError, ValueError):
        raise parser.ResumeRefusal("worker_failed") from None


def _reply(parser, argv: list[str]) -> dict:
    """The child's answer. Never an exception's text: a refusal code, or `corrupt`."""
    try:
        kind, today = _arguments(parser, argv)
        data = sys.stdin.buffer.read(parser.MAX_BYTES + 1)
        if len(data) > parser.MAX_BYTES:
            raise parser.ResumeRefusal("too_big")
        return {"ok": True, "draft": parser.parse_bytes(data, kind, today)}
    except parser.ResumeRefusal as refusal:
        return {"ok": False, "reason": refusal.reason}
    except Exception:  # a stranger's file broke the parser; say so, and nothing more
        return {"ok": False, "reason": "corrupt"}


def main(argv: "list[str] | None" = None) -> int:
    """
    The child's entry point (2.2): `resume_worker.py KIND YYYY-MM-DD`, the
    file's bytes on stdin, one JSON object on stdout. Always returns 0.

    The order matters. Limits go on before any resume byte is read, and
    `sys.path` is fixed before `resume_parse` is imported, because `-I` leaves
    this file's directory off it.
    """
    argv = sys.argv if argv is None else argv
    if hasattr(signal, "SIGXFSZ"):
        # Over RLIMIT_FSIZE a write fails with an error instead of killing us.
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _limit_resources()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import resume_parse

    sys.stdout.write(json.dumps(_reply(resume_parse, argv)))
    sys.stdout.flush()
    return 0


# ------------------------------------------------------------------ the parent

def _refusal(reason: str) -> Exception:
    from resume_parse import ResumeRefusal

    return ResumeRefusal(reason)


async def _spawn(kind: str, today: date, argv: "list[str] | None") -> asyncio.subprocess.Process:
    """The child, isolated (`-I -B`), with a scrubbed environment and stderr discarded."""
    try:
        return await asyncio.create_subprocess_exec(
            *(argv if argv is not None
              else (sys.executable, "-I", "-B", str(WORKER_PATH), kind, today.isoformat())),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=worker_env())
    except OSError:
        raise _refusal("worker_failed") from None


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _exchange(process: asyncio.subprocess.Process, data: bytes, timeout: float) -> bytes:
    """Pipe `data` in and read stdout, killing the child if it outlives `timeout`."""
    try:
        out, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        # Both, and named here rather than bound once at import: on 3.10, which
        # the bot still starts on, `wait_for` raises asyncio's own class, and
        # only 3.11 made it the builtin. Missing it would skip the kill.
        _kill(process)
        await process.wait()
        raise _refusal("timeout") from None
    except asyncio.CancelledError:
        _kill(process)
        raise
    return out


def _draft_from(returncode: "int | None", out: bytes) -> dict:
    """What the child said, as a validated draft, or the refusal it amounts to."""
    import resume_parse

    if returncode != 0 or len(out) > MAX_STDOUT:
        raise resume_parse.ResumeRefusal("worker_failed")
    try:
        reply = json.loads(out)
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the decoder allows
        raise resume_parse.ResumeRefusal("worker_failed") from None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise resume_parse.ResumeRefusal("worker_failed")
    if not reply["ok"]:
        reason = reply.get("reason")
        known = isinstance(reason, str) and reason in resume_parse.REASONS
        raise resume_parse.ResumeRefusal(reason if known else "worker_failed")
    return resume_parse.validate_draft(reply.get("draft"))


async def run_worker(data: bytes, kind: str, today: date, *, timeout: float = TIMEOUT_S,
                     argv: "list[str] | None" = None) -> dict:
    """
    2.2. Returns validate_draft(...) or raises resume_parse.ResumeRefusal.
    `argv` (tests only) replaces [sys.executable, "-I", "-B", str(WORKER_PATH),
    kind, today.isoformat()] as the command.

    At most MAX_CONCURRENT children run at once; a caller that waits more than
    BUSY_WAIT_S for a slot is told `busy` rather than queued behind a stuck
    upload. `data` is let go as soon as it has been piped.
    """
    import resume_parse

    semaphore = _semaphore()
    try:
        await asyncio.wait_for(semaphore.acquire(), BUSY_WAIT_S)
    except (TimeoutError, asyncio.TimeoutError):  # both, for 3.10; see `_exchange`
        raise resume_parse.ResumeRefusal("busy") from None
    try:
        process = await _spawn(kind, today, argv)
        out = await _exchange(process, data, timeout)
    finally:
        semaphore.release()
        del data
    return _draft_from(process.returncode, out)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
