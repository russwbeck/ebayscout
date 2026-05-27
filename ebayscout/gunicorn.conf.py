# Gunicorn config for ebayscout Flask service
# Mirrors buybot's gunicorn.conf.py

bind        = "0.0.0.0:8080"
workers     = 1       # single worker — CLIP model lives in one process
threads     = 8       # handle concurrent Slack events
timeout     = 0       # unlimited — CLIP hydration can take 30-60s at cold start
preload_app = True    # import app in master process so startup errors are visible in logs


def post_fork(server, worker):
    """Start background CLIP + Sheets hydration after the worker is forked."""
    import ebayscout.main as m
    m.startup()
