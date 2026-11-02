"""Build the zip for the Dataset Adapter Lambda.

    python tools/build_lambda.py            # -> dist/adapter-lambda.zip

Run it from ingest/. The adapter only uses the standard library (boto3 comes
with the Lambda runtime), so the zip is just the traceingest package and works
on arm64 and x86_64. Handler: traceingest.adapter_lambda.handler.
"""
from __future__ import annotations

import argparse
import os
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(out: str) -> str:
    pkg = os.path.join(HERE, "traceingest")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(pkg):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, HERE))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=os.path.join(HERE, "dist", "adapter-lambda.zip"))
    a = p.parse_args(argv)
    path = build(a.out)
    print(f"{path}  {os.path.getsize(path) / 1e3:.0f} KB  handler=traceingest.adapter_lambda.handler")


if __name__ == "__main__":
    main()
