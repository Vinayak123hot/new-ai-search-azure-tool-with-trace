#!/bin/bash
/home/site/wwwroot/pythonenv3.11/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --log-level info
