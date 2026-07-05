#!/usr/bin/env python
"""Genera el bundle estático de la HU ladder: /data/ladder/build/harness/{strategy.py,
decide_system7.py, treys/} + /data/ladder/bundle.zip. Correr DENTRO del contenedor:
    docker compose exec -T dashboard uv run python /data/ladder-src/build_bundle.py
"""
import json
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))                  # /data/ladder-src
APP = os.environ.get("S7_APP_DIR", "/app")
OUT = os.environ.get("S7_LADDER_DIR", "/data/ladder")
CFG = os.environ.get("S7_LADDER_STRAT", "/data/strategies/system7-hu.json")


def main():
    cfg = json.load(open(CFG))
    assert cfg.get("hu") is True, "la config de la ladder debe ser HU (hu:true)"
    build = os.path.join(OUT, "build")
    har = os.path.join(build, "harness")
    shutil.rmtree(build, ignore_errors=True)
    os.makedirs(har, exist_ok=True)

    tpl = open(os.path.join(HERE, "strategy_template.py")).read()
    assert "%%CONFIG%%" in tpl
    open(os.path.join(har, "strategy.py"), "w").write(
        tpl.replace("%%CONFIG%%", repr(cfg)))     # repr = literal Python válido (json crudo mete true/false/null)

    shutil.copy2(os.path.join(APP, "decide_system7.py"), os.path.join(har, "decide_system7.py"))

    import treys
    shutil.copytree(os.path.dirname(treys.__file__), os.path.join(har, "treys"),
                    ignore=shutil.ignore_patterns("__pycache__"))

    zp = os.path.join(OUT, "bundle.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(build):
            for f in files:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, build))
        z.writestr("assets/README.txt",
                   "System 7 HU static bot (QuantArmy-7). Engine decide_system7 v%s + treys vendored; "
                   "config system7-hu embedded in strategy.py.\n" % _engine_version(har))
    print("bundle:", zp, os.path.getsize(zp), "bytes")
    print("harness:", sorted(os.listdir(har)))


def _engine_version(har):
    try:
        for line in open(os.path.join(har, "decide_system7.py")):
            if line.startswith("VERSION"):
                return line.split('"')[1]
    except Exception:
        pass
    return "?"


if __name__ == "__main__":
    main()
