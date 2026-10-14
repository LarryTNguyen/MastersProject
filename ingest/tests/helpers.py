import os

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "fixtures")


def fx(name):
    return os.path.join(FX, name)


def load(name, **kw):
    """Read a fixture with the matching converter (kept independent of formats.py)."""
    path = fx(name)
    if name.startswith("alibaba"):
        from traceingest import alibaba
        with open(path, newline="") as f:
            return alibaba.read_alibaba(f, **kw)
    if name.startswith("rcaeval"):
        from traceingest import rcaeval
        with open(path, newline="") as f:
            return rcaeval.read_rcaeval_csv(f)
    import json
    from traceingest import otlp
    from traceingest.spans import Span
    with open(path) as f:
        return sorted(otlp.from_otlp(json.load(f)), key=Span.sort_key)


def by_rpcid(spans):
    return {s.attributes["alibaba.rpcid"]: s for s in spans}


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"
