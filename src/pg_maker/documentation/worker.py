"""Container entrypoint; all invocations are recorded by the common build runner."""

import sys
from pathlib import Path

from pg_maker.errors import MakerError
from pg_maker.storage import read_json, write_json
from pg_maker.yamlio import load_yaml

from . import site, translation, writer
from .validation import html_tree, man_tree


def main():
    request = read_json(Path(sys.argv[1]))
    action = request.pop("action")
    result_path = Path(request.pop("result"))
    if action == "writer":
        request["assets"] = {key: Path(value) for key, value in request["assets"].items()}
        request["source"], request["destination"] = (
            Path(request["source"]),
            Path(request["destination"]),
        )
        result = writer.apply(**request)
    elif action == "extract":
        request["root"] = Path(request["root"])
        result = translation.extract(**request)
    elif action == "translate":
        request["root"], request["destination"] = (
            Path(request["root"]),
            Path(request["destination"]),
        )
        request["bundle"] = load_yaml(Path(request["bundle"]), "translations")
        result = translation.apply(**request)
    elif action == "site":
        site.prepare(
            Path(request["source"]),
            Path(request["destination"]),
            request["context"],
            request["language"],
            branding=request["branding"],
            links=request["links"],
        )
        result = {"prepared": True}
    elif action == "html":
        result = html_tree(Path(request.pop("root")), **request)
    elif action == "man":
        result = man_tree(Path(request["root"]))
    else:
        raise ValueError("unknown documentation operation")
    write_json(result_path, result)


if __name__ == "__main__":
    try:
        main()
    except MakerError as exc:
        print(str(exc), file=sys.stderr)
        if len(sys.argv) > 1:
            request = read_json(Path(sys.argv[1]))
            write_json(Path(request["result"]).with_suffix(".error.json"), exc.as_dict())
        raise SystemExit(exc.exit_code) from exc
