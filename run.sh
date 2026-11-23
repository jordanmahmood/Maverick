#!/bin/sh
# Start the Maverick hub and open the display window. Keys are read from maverick/.env.
cd "$(dirname "$0")"
echo "Display: http://localhost:8000/"
echo "Glasses or phone: ./device.sh (USB cable) or ./device.sh wifi"
(sleep 2; open http://localhost:8000/) &
exec uv run --project backend uvicorn maverick.main:app --app-dir backend --host 0.0.0.0 --port 8000
