#!/usr/bin/env python3
"""Watchdog / auto-refire for a Gemini-pipeline watcher worker.

Keeps ONE worker (`watcher.py --config <cfg>`) processing. Rule:

  Refire the worker when BOTH are true:
    1) photos still exist to process in THIS worker's shard of the GCS input
       prefix, AND
    2) no progress has been made for STALL_SECONDS (default 300 = 5 min).

"Progress" = at least one previously-pending input was consumed (deleted) since
the last check — so it does NOT false-trip just because new photos arrived while
the worker is actively draining. When the shard is empty it does nothing (no idle
churn), and it never refires more often than once per STALL_SECONDS.

HARD LIMIT: at most MAX_REFIRES (2) automatic refires WITHOUT human interaction.
After two refires with no progress it stops refiring and posts a "needs a human"
Slack alert (out of tokens, a dead Google session needing `watcher.py --login`,
or a sleeping machine). The budget re-arms automatically once the worker is seen
draining again (real progress).

Every Slack ping reports how many images remain (total in the input + this
worker's shard). Reuses the watcher's own GCS access (raw REST + service-account
token) so it needs no extra dependencies. Run one per worker, ideally in tmux:
    python3 supervise.py --config worker0.json
    python3 supervise.py --config worker1.json
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher   # reuse get_gcs_token + gcs_list_objects (no google-cloud dep needed)

CHECK_EVERY   = 60     # seconds between GCS checks
STALL_SECONDS = 300    # 5 min of no progress with work pending → refire
MAX_REFIRES   = 2      # most automatic refires WITHOUT human interaction


def _load(path):
    with open(path) as f:
        return json.load(f)


def _owns(name, count, index):
    """Mirror watcher._owns: this worker's deterministic crc32 shard."""
    if count <= 1:
        return True
    base = name.rsplit("/", 1)[-1]
    return (zlib.crc32(base.encode("utf-8")) % count) == index


def _pending(cfg, prefix, count, index):
    """(shard_names, total) — input objects in THIS worker's shard, and the total
    across all shards (folder markers skipped). Uses the watcher's REST helpers."""
    token = watcher.get_gcs_token(cfg)                 # fresh token each cycle (no expiry bugs)
    items = watcher.gcs_list_objects(prefix.rstrip("/") + "/", cfg, token)
    out = set()
    total = 0
    for it in items:
        name = it.get("name")
        if not name or name.endswith("/"):
            continue
        total += 1
        if _owns(name, count, index):
            out.add(name)
    return out, total


def _remaining(shard_n, total):
    return (f"{total} image{'s' if total != 1 else ''} remaining "
            f"({shard_n} in this worker's shard)")


def _slack(cfg, text):
    url = cfg.get("slack_webhook_url")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f">>> SUPERVISE: slack post failed (ignored): {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="workerN.json (same file the watcher uses)")
    ap.add_argument("--stall", type=int, default=STALL_SECONDS,
                    help="seconds of no-progress-with-work before refiring (default 300)")
    args = ap.parse_args()

    cfg    = _load(args.config)
    count  = int(cfg.get("worker_count", 1) or 1)
    index  = int(cfg.get("worker_index", 0) or 0)
    prefix = cfg.get("gcs_input_prefix", "pipeline/input")
    label  = f"worker {index}"
    here   = os.path.dirname(os.path.abspath(__file__))

    proc = None
    last_launch = 0.0
    last_progress = time.time()
    refires = 0            # consecutive auto-refires since last progress (startup excluded)
    gave_up = False        # hit MAX_REFIRES with no progress → idle until a human helps

    def launch(reason):
        nonlocal proc, last_launch, last_progress
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except Exception:
                proc.kill()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(here, "watcher.py"), "--config", args.config],
            cwd=here)
        last_launch = last_progress = time.time()
        print(f">>> SUPERVISE: launched {label} — {reason}", flush=True)
        _slack(cfg, f"🔁 *{label} refired* — {reason}")

    # initial read so the startup ping carries a count and progress tracking starts warm
    try:
        prev_names, total = _pending(cfg, prefix, count, index)
    except Exception as e:
        print(f">>> SUPERVISE: initial GCS read failed: {e}", flush=True)
        prev_names, total = set(), 0
    launch(f"startup — {_remaining(len(prev_names), total)}")

    while True:
        time.sleep(CHECK_EVERY)
        try:
            names, total = _pending(cfg, prefix, count, index)
        except Exception as e:
            print(f">>> SUPERVISE: GCS check failed (ignored): {e}", flush=True)
            continue

        now = time.time()
        if prev_names - names:                         # ≥1 previously-pending input consumed
            if gave_up or refires:
                _slack(cfg, f"✅ *{label}* draining again — {_remaining(len(names), total)}. "
                            f"Auto-refire re-armed.")
            last_progress = now
            refires = 0
            gave_up = False
        prev_names = names

        pending = len(names)
        alive   = bool(proc and proc.poll() is None)
        stalled = (now - last_progress) >= args.stall
        cooled  = (now - last_launch) >= args.stall

        if not (pending > 0 and stalled and cooled):
            continue                                   # empty queue, or still within a window
        if gave_up:
            continue                                   # already escalated; wait for progress

        if refires < MAX_REFIRES:
            refires += 1
            mins = int((now - last_progress) // 60)
            launch(f"refire {refires}/{MAX_REFIRES} — {_remaining(pending, total)}; "
                   f"no progress for {mins}m"
                   + ("" if alive else " (worker had exited)"))
        else:
            gave_up = True
            _slack(cfg, f"⛔ *{label}* stopped after {MAX_REFIRES} auto-refires — "
                        f"{_remaining(pending, total)}. Needs a human (out of tokens, a "
                        f"dead Google session needing `python3 watcher.py --login "
                        f"--config {os.path.basename(args.config)}`, or a sleeping "
                        f"machine). Won't auto-refire again until it's draining.")


if __name__ == "__main__":
    main()
