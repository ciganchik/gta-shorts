web: uvicorn app.web:app --host 0.0.0.0 --port $PORT
worker: python -m app.worker
capture: python -m app.capture_worker
render: python -m app.shorts_engine
