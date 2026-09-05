"""Unified command-line entry point for the Epitype toolkit."""

from importlib import import_module
import sys

from . import __version__


_COMMANDS = {
    "search": ("epitype.memsearch", "main", ()),
    "decisions": ("epitype.decision_lint", "main", ()),
    "ledger": ("epitype.ledger_gate", "main", ()),
    "compact-map": ("epitype.compact_map", "main", ()),
    "scar-census": ("epitype.scar_census", "main", ()),
    "pending": ("epitype.pending_lint", "main", ()),
    "narration": ("epitype.narration_meter", "main", ()),
    "token-meter": ("epitype.token_meter", "main", ()),
    "exam": ("exam.exam_runner", "main", ()),
    "trust": ("adapters.codex.hook_trust", "main", ("check",)),
    "install": ("install.graft", "main", ("install",)),
    "uninstall": ("install.graft", "main", ("uninstall",)),
    "doctor": ("install.graft", "main", ("doctor",)),
    "vaults": ("install.graft", "main", ("vaults",)),
    "relocate": ("install.graft", "main", ("relocate",)),
}


def _help():
    commands = "\n".join(f"  {name}" for name in _COMMANDS)
    return (
        "usage: epitype <command> [options]\n\n"
        "commands:\n"
        f"{commands}\n\n"
        "Run 'epitype <command> --help' for command-specific options."
    )


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--version"], ["-V"]):
        print(f"epitype {__version__}")
        return 0
    if not arguments or arguments in (["--help"], ["-h"], ["help"]):
        print(_help())
        return 0

    command = arguments.pop(0)
    target = _COMMANDS.get(command)
    if target is None:
        print(f"epitype: unknown command: {command}", file=sys.stderr)
        print(_help(), file=sys.stderr)
        return 2
    module_name, function_name, prefix = target
    function = getattr(import_module(module_name), function_name)
    result = function([*prefix, *arguments])
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
