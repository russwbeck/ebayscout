"""Check that the shared base image already has every package ebayscout pins.

ebayscout's image starts FROM buttonmatcher's shared base (button-base:<tag>,
built from buttonmatcher's base/Dockerfile) and installs nothing itself, so
Artifact Registry stores the stack once for both services. requirements.txt
still lists what this service needs; ebayscout/Dockerfile runs this script so
a pin the base does not meet fails the build instead of shipping without it:

    python -I ebayscout/check_base_pins.py ebayscout/requirements.txt

Every requirement must be exact: name==version, or name @ git+URL@commit.
Standard library only: it runs before any app code, and in CI.
"""
import json
import re
import sys
from importlib import metadata

_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(==|@)?\s*(.*)$")


def parse(text):
    """(name, op, value) for each requirement line; op is '==', '@' or None."""
    reqs = []
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _REQ.match(line)
        if m is None:
            reqs.append((line, None, ""))
        else:
            reqs.append((m.group(1), m.group(2), m.group(3).split(";")[0].strip()))
    return reqs


def _split_vcs(value):
    """'git+URL@rev' -> (URL, rev); rev is '' when the line names none."""
    spec = value[len("git+"):] if value.startswith("git+") else value
    url, sep, rev = spec.rpartition("@")
    if not sep or "/" in rev:
        return spec, ""
    return url, rev


def problems(reqs, version_of, direct_url_of):
    """A message for each requirement the installed packages don't meet.

    version_of(name) gives the installed version or None; direct_url_of(name)
    gives the install's direct_url.json as a dict, or None.
    """
    out = []
    for name, op, value in reqs:
        if op is None:
            out.append(f"{name}{value}: pin it exactly (== or @commit) so it can be checked")
            continue
        have = version_of(name)
        if have is None:
            out.append(f"{name}: not installed in the base")
        elif op == "==" and have != value:
            out.append(f"{name}: requirements.txt pins {value}, the base has {have}")
        elif op == "@":
            url, rev = _split_vcs(value)
            got = direct_url_of(name) or {}
            got_url = got.get("url", "")
            got_rev = (got.get("vcs_info") or {}).get("commit_id", "")
            if not rev:
                out.append(f"{name}: pin a commit (@<sha>) so it can be checked")
            elif (got_url, got_rev) != (url, rev):
                out.append(f"{name}: requirements.txt asks for {url}@{rev}, "
                           f"the base has {got_url or '?'}@{got_rev or '?'}")
    return out


def _version_of(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _direct_url_of(name):
    try:
        text = metadata.distribution(name).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    return json.loads(text) if text else None


def main(argv):
    path = argv[1] if len(argv) > 1 else "ebayscout/requirements.txt"
    with open(path, encoding="utf-8") as f:
        reqs = parse(f.read())
    bad = problems(reqs, _version_of, _direct_url_of)
    if bad:
        print(f"{path} does not match the base image:")
        for line in bad:
            print(f"  {line}")
        print("Build a new base in buttonmatcher (base/Dockerfile; a package only "
              "ebayscout uses goes in base/requirements-ebayscout.txt), then put "
              "its tag in ebayscout/Dockerfile's FROM.")
        return 1
    print(f"{path}: the base has all {len(reqs)} pins.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
