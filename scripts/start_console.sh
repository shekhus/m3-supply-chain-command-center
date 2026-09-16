#!/bin/sh
# Console entrypoint: a thin Streamlit client of the API at API_BASE_URL.
exec streamlit run console/app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false
