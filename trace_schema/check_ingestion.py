"""Validate canonical records produced by the repository's ingestion adapters."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingest"))

from traceingest import formats  # noqa: E402
from traceingest.canonical import NormalizationContext, to_record  # noqa: E402
from validate import validate_record  # noqa: E402


def main() -> int:
    fixtures = ROOT / "ingest" / "tests" / "fixtures"
    inputs = [fixtures / "alibaba_small.csv", fixtures / "rcaeval_small.csv",
              fixtures / "otlp_small.json"]
    context = NormalizationContext.for_source(
        "verification", run_id="00000000-0000-4000-8000-000000000001")
    checked = 0
    failures = []
    for path in inputs:
        for index, span in enumerate(formats.load_path(str(path)).spans):
            for error in validate_record(to_record(span, context)):
                failures.append(f"{path.name}[{index}]: {error}")
            checked += 1
    if failures:
        print("\n".join(failures))
        return 1
    print(f"PASS: {checked} Alibaba, RCAEval, and OTLP ingestion records match trace schema v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
