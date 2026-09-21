#!/usr/bin/env python3
"""Persistent python kernel for stage hooks - the CHILD process.

Started by :class:`yaha.hooks.kernel.PythonKernel` as
``python -u -m yaha.hooks.kernel_main`` with the *package parent* on
``PYTHONPATH``, so ``yaha.hooks.dom`` is importable.

Protocol
--------
Requests arrive as one JSON object per line on stdin::

    {"id": 3, "code": "x = 1", "snapshot": {...}}

Responses go out on stdout as one record per line, prefixed with the ASCII
record separator ``\\x1e`` so that stray bytes written by hook code (for example
``os.write(1, ...)``) can never be mistaken for - or corrupt - a response::

    \\x1e{"id": 3, "ok": true, "stdout": "...", "stderr": "", "emitted": [],
         "exc_type": null, "exc_value": null, "traceback": null}

State
-----
Every request is executed in ONE persistent namespace dict, which is the whole
point: a later block sees the variables an earlier block assigned. The
namespace is deliberately NOT rolled back when a block raises - a failed
Jupyter-like cell keeps whatever it already mutated.

``sys.stdout`` / ``sys.stderr`` are redirected around each request, so ``print``
output is captured per block and never pollutes the protocol. ``emit(value)`` is
pre-bound (unconditionally, not only for ``capture`` hooks) as the explicit way
to hand structured output back to the parent.
"""

import io
import json
import sys
import traceback

RS = "\x1e"

try:  # the parent puts the package parent on PYTHONPATH for us
    from yaha.hooks.dom import YahaView
except Exception:  # pragma: no cover - defensive: never die at import time
    YahaView = None


_REAL_STDOUT = sys.stdout
_REAL_STDIN = sys.stdin


def _send(payload):
    _REAL_STDOUT.write(RS + json.dumps(payload, default=str) + "\n")
    _REAL_STDOUT.flush()


def _execute(code, namespace, snapshot, emitted):
    """Run one block in the persistent namespace and capture its effects."""
    del emitted[:]
    if YahaView is not None:
        namespace["yaha"] = YahaView.from_snapshot(snapshot)
    else:  # pragma: no cover
        namespace["yaha"] = snapshot or {}

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    ok = True
    exc_info = {"exc_type": None, "exc_value": None, "traceback": None}
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        exec(compile(code, "<yaha-hook>", "exec"), namespace)
    except BaseException as exc:  # SystemExit included: a hook must not kill us
        ok = False
        exc_info = {
            "exc_type": type(exc).__name__,
            "exc_value": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    return {
        "ok": ok,
        "stdout": buf_out.getvalue(),
        "stderr": buf_err.getvalue(),
        "emitted": list(emitted),
        **exc_info,
    }


def main():
    namespace = {"__name__": "__yaha_hook__"}
    emitted = []

    def emit(value):
        emitted.append(str(value))

    emit.__doc__ = (
        "Hand structured output back to the parent. Under a (capture) hook the "
        "emitted text joins stdout in the injected observation."
    )
    namespace["emit"] = emit

    for line in _REAL_STDIN:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            continue
        result = _execute(
            request.get("code") or "",
            namespace,
            request.get("snapshot") or {},
            emitted,
        )
        result["id"] = request.get("id")
        try:
            _send(result)
        except Exception:
            break


if __name__ == "__main__":
    main()
