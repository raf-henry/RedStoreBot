RUNTIME=python
START=uvicorn main:app --host 0.0.0.0 --port $PORT
MEMORY=512
VERSION=recommended
DISPLAY_NAME=redstore-discord-bridge
DESCRIPTION=Bridge FastAPI e bot Discord do RedStore
AUTORESTART=true
