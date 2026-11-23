"""Build the zip for the reconstruction Lambda.

    python tools/build_lambda.py            # arm64, Python 3.12 -> dist/reconstruction-lambda.zip

Run it from reconstruction/. Downloads Linux wheels so it works from Windows too.
Adds trace_reconstruction/ and the trace_schema files the validator needs.
Handler: trace_reconstruction.lambda_handler.handler. boto3 is not bundled.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(os.path.dirname(HERE), "trace_schema")
PLATFORMS = {"arm64": "manylinux2014_aarch64", "x86_64": "manylinux2014_x86_64"}
SCHEMA_FILES = ("validate.py", "canonical-span.schema.json")


def runtime_requirements(path: str) -> list:
    reqs = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                reqs.append(line)
    return reqs


def build(arch: str, python: str, out: str) -> str:
    reqs = runtime_requirements(os.path.join(HERE, "requirements.txt"))
    tmp = tempfile.mkdtemp(prefix="reconstruction-lambda-")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--quiet", "--target", tmp,
            "--platform", PLATFORMS[arch], "--implementation", "cp",
            "--python-version", python, "--only-binary=:all:", "--upgrade", *reqs,
        ])
        shutil.copytree(
            os.path.join(HERE, "trace_reconstruction"),
            os.path.join(tmp, "trace_reconstruction"),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        schema_out = os.path.join(tmp, "trace_schema")
        os.makedirs(schema_out)
        for name in SCHEMA_FILES:
            shutil.copy2(os.path.join(SCHEMA, name), os.path.join(schema_out, name))
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(tmp):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(files):
                    if name.endswith(".pyc"):
                        continue
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    p.add_argument("--python", default="3.12", help="Lambda runtime version, e.g. 3.12")
    p.add_argument("--out", default=os.path.join(HERE, "dist", "reconstruction-lambda.zip"))
    a = p.parse_args(argv)
    path = build(a.arch, a.python, a.out)
    print(f"{path}  {os.path.getsize(path) / 1e6:.1f} MB  "
          f"handler=trace_reconstruction.lambda_handler.handler  arch={a.arch}  python={a.python}")


if __name__ == "__main__":
    main()
