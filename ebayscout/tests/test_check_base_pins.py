"""check_base_pins: ebayscout's build fails when the shared base image does not
have a package at the version requirements.txt pins (ebayscout/Dockerfile)."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from ebayscout import check_base_pins as cbp  # noqa: E402

CLIP = "d05afc436d78f1c48dc0dbf8e5980a9d471f35f6"
REQS = f"""
--index-url https://download.pytorch.org/whl/cpu
torch==2.2.2+cpu   # the CPU wheel
--extra-index-url https://pypi.org/simple
# a comment
clip @ git+https://github.com/openai/CLIP.git@{CLIP}
google-api-core[grpc]==2.30.3
"""
BASE = {"torch": "2.2.2+cpu", "clip": "1.0", "google-api-core": "2.30.3"}
CLIP_URL = {"url": "https://github.com/openai/CLIP.git",
            "vcs_info": {"vcs": "git", "commit_id": CLIP}}


def _check(text, base=BASE, clip_url=CLIP_URL):
    return cbp.problems(cbp.parse(text), base.get,
                        lambda name: clip_url if name == "clip" else None)


def test_parse_skips_options_and_comments_and_drops_extras():
    assert cbp.parse(REQS) == [
        ("torch", "==", "2.2.2+cpu"),
        ("clip", "@", f"git+https://github.com/openai/CLIP.git@{CLIP}"),
        ("google-api-core", "==", "2.30.3"),
    ]


def test_a_base_with_every_pin_passes():
    assert _check(REQS) == []


def test_a_changed_pin_fails():
    assert _check(REQS.replace("2.30.3", "2.31.0")) == [
        "google-api-core: requirements.txt pins 2.31.0, the base has 2.30.3"]


def test_a_package_the_base_lacks_fails():
    assert _check(REQS + "anthropic==0.40.0\n") == [
        "anthropic: not installed in the base"]


def test_a_different_clip_commit_fails():
    other = dict(CLIP_URL, vcs_info={"vcs": "git", "commit_id": "ded190a"})
    assert len(_check(REQS, clip_url=other)) == 1


def test_an_inexact_requirement_fails():
    assert len(_check("flask>=3.0\n")) == 1
    assert len(_check("clip @ git+https://github.com/openai/CLIP.git\n")) == 1


def test_the_real_requirements_are_all_exact():
    # Every line has to be checkable, CLIP's included: without a commit the base
    # and this file could name different encoders and the check would not see it.
    with open(os.path.join(os.path.dirname(HERE), "requirements.txt")) as f:
        reqs = cbp.parse(f.read())
    assert reqs and all(op in ("==", "@") for _, op, _ in reqs)
    clip = dict((n, v) for n, _, v in reqs)["clip"]
    assert re.fullmatch(r"[0-9a-f]{40}", cbp._split_vcs(clip)[1])
