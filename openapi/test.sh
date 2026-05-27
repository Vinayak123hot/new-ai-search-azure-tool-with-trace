#!/bin/bash
set -euo pipefail

BASE="https://vinnyclasifiervm.eastus.cloudapp.azure.com"
KEY="befb2fb3yfg37f3gf8777gf3gfy3fb3hfb3fhb3hf3fb3hf3fj3"

echo ""
echo "=== 1. Health check ==="
curl -sf "$BASE/healthz"

echo ""
echo ""
echo "=== 2. Valid request — expect spread + candidates ==="
curl -sf -X POST "$BASE/get_kb_candidates" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d '{"description":"Outlook crashes when I click send with a PDF attached"}' \
  | python3 -m json.tool

echo ""
echo "=== 3. Wrong API key — expect 401 ==="
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/get_kb_candidates" \
  -H "Content-Type: application/json" \
  -H "x-api-key: wrong-key" \
  -d '{"description":"test"}')
echo "HTTP $STATUS (expected 401)"

echo ""
echo "=== 4. Empty description — expect 400 ==="
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/get_kb_candidates" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $KEY" \
  -d '{"description":""}')
echo "HTTP $STATUS (expected 400)"

echo ""
echo "=== All tests done ==="