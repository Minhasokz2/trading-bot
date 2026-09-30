#!/bin/bash
# macOS launcher — double-click in Finder (Terminal opens) or run from Terminal.
cd "$(dirname "$0")/.." || exit 1
done_msg(){ echo; read -n 1 -s -r -p "Press any key to close this window..."; echo; }
echo "== Coin Audit Bot — Mac setup =="
PY=""
for c in python3.12 python3.13 python3.11 python3.14 \
         /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,15) else 1)" 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11-3.14 not found (the Python that ships with macOS is 3.9 — too old)."
  echo "Install ONE of these, then double-click this file again:"
  echo "  a) python.org -> Downloads -> macOS -> Python 3.12 installer"
  echo "  b) Homebrew:  brew install python@3.12"
  done_msg; exit 1
fi
echo "Using $($PY --version) at $(command -v $PY)"
"$PY" -m venv .venv || { echo "Could not create .venv"; done_msg; exit 1; }
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt || { echo "pip install failed — see the error above."; done_msg; exit 1; }
if ! .venv/bin/python -c "import lightgbm" 2>/dev/null; then
  echo
  echo "LightGBM needs the OpenMP library on macOS."
  if command -v brew >/dev/null 2>&1; then
    echo "Installing it with Homebrew: brew install libomp"; brew install libomp
  else
    echo "Install Homebrew (https://brew.sh), then run:  brew install libomp"
  fi
fi
.venv/bin/python -c "import binance_sdk_spot, pandas_ta_classic, lightgbm, vectorbt, quantstats, statsmodels" \
  && echo "All libraries import correctly." || echo "Some libraries failed to import — see above."
chmod +x mac/*.command linux/*.sh 2>/dev/null
echo; echo "Done. Next: double-click \"10 Demo (offline).command\" to check the install, then \"2 Audit a coin.command\"."
done_msg
