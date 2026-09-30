"""Start the web app:  python audit/serve.py   (Render and Docker run exactly this).

PORT is set by Render (default 10000); HOST defaults to 0.0.0.0 so the platform's proxy can reach it."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    import uvicorn
    uvicorn.run("webapp:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "10000")),
                log_level=os.environ.get("LOG_LEVEL", "info"), access_log=False)


if __name__ == "__main__":
    main()
