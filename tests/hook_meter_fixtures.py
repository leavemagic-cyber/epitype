"""Process barriers for synthetic meter races; never installed as a hook."""
from pathlib import Path
import textwrap


def synchronized_meter_hook(script, directory, participants):
    """Hold each real notice before its claim so every contender reaches the race."""
    script = Path(script).resolve()
    directory = Path(directory).resolve()
    ready = directory / "meter-race-ready"
    ready.mkdir()
    wrapper = directory / "meter-race-hook.py"
    wrapper.write_text(textwrap.dedent(f'''\
        import os
        from pathlib import Path
        import runpy
        import sys
        import time
        sys.path[:0] = {[str(script.parents[2]), str(script.parent)]!r}
        from epitype import context_meter, memspec
        original_notice = context_meter.notice
        ready = Path({str(ready)!r})

        def synchronized_notice(*args, **kwargs):
            value = original_notice(*args, **kwargs)
            if value is not None:
                (ready / str(os.getpid())).touch(exist_ok=False)
                deadline = time.monotonic() + memspec.HOOK_TIMEOUT_SECONDS
                while len(list(ready.iterdir())) < {participants}:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("meter race participants did not reach the barrier")
                    time.sleep(0.005)
            return value

        context_meter.notice = synchronized_notice
        runpy.run_path({str(script)!r}, run_name="__main__")
        '''), encoding="utf-8")
    return wrapper
