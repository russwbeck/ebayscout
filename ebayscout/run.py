"""
ebayscout/run.py

Container entry point.  The Dockerfile sets CMD ["python", "run.py"].
Running from the /app working directory, this file imports the package
and calls main().
"""

import sys
from ebayscout.job import main

if __name__ == "__main__":
    sys.exit(main())
