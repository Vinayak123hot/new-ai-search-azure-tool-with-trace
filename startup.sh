#!/bin/bash
/home/site/wwwroot/pythonenv3.11/bin/uvicorn app.main_traced:app --host 0.0.0.0 --port 8000 --workers 1 --log-level info
