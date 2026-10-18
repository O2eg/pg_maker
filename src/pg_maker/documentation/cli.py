"""Standalone tools use the same locked, containerized operations as pg-maker."""

import sys


def writer_main():
    from pg_maker.cli import main

    return main(["docs", "write", *sys.argv[1:]])


def translation_main():
    from pg_maker.cli import main

    arguments = sys.argv[1:]
    if arguments and arguments[0] in {"extract", "apply", "validate"}:
        command = {
            "extract": "prepare-translations",
            "apply": "apply-translations",
            "validate": "apply-translations",
        }[arguments.pop(0)]
    else:
        command = "prepare-translations"
    return main(["docs", command, *arguments])
