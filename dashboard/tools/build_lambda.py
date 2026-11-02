"""Build the zip for the dashboard API Lambda.

    python tools/build_lambda.py                 # arm64, Python 3.12 -> dist/dashboard-lambda.zip
    python tools/build_lambda.py --arch x86_64 --python 3.13 --out /tmp/dash.zip

Run it from dashboard/. It downloads Linux wheels for the target (so it works
from a Mac too), adds app/ and static/, and writes one zip. Handler:
app.lambda_handler.handler. boto3 is not bundled because the Lambda Python
runtime already has it.

The runtime packages are the lines of requirements.txt above the
"local development" comment, so the two can't drift apart.
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
PLATFORMS = {"arm64": "manylinux2014_aarch64", "x86_64": "manylinux2014_x86_64"}


def runtime_requirements(path: str) -> list:
    reqs = []
    with open(path) as f:
        for line in f:
            if "local development" in line:
                break
            line = line.split("#", 1)[0].strip()
            if line:
                reqs.append(line)
    return reqs


def build(arch: str, python: str, out: str) -> str:
    reqs = runtime_requirements(os.path.join(HERE, "requirements.txt"))
    tmp = tempfile.mkdtemp(prefix="dashboard-lambda-")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--target", tmp,
                               "--platform", PLATFORMS[arch], "--implementation", "cp",
                               "--python-version", python, "--only-binary=:all:", "--upgrade", *reqs])
        for d in ("app", "static"):
            shutil.copytree(os.path.join(HERE, d), os.path.join(tmp, d),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db"))
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
    p.add_argument("--out", default=os.path.join(HERE, "dist", "dashboard-lambda.zip"))
    a = p.parse_args(argv)
    path = build(a.arch, a.python, a.out)
    print(f"{path}  {os.path.getsize(path) / 1e6:.1f} MB  handler=app.lambda_handler.handler  arch={a.arch}  python={a.python}")


if __name__ == "__main__":
    main()
