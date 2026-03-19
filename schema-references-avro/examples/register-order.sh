#!/usr/bin/env bash
#
# register-order.sh
#
# Demonstrates correct bottom-up registration order for Avro schemas
# with references in Apicurio Registry (v3 API).
#
# The dependency graph:
#
#   OrderEvent
#     -> Address (shared)
#     -> Money (shared)
#     -> PaymentInfo (depends on Money)
#
# Registration order (topological sort, leaves first):
#   1. Address       (leaf - no dependencies)
#   2. Money         (leaf - no dependencies)
#   3. PaymentInfo   (depends on Money)
#   4. OrderEvent    (depends on Address, PaymentInfo)
#
# Usage:
#   ./register-order.sh [REGISTRY_URL]
#
# Example:
#   ./register-order.sh http://localhost:8080/apis/registry/v3
#

set -euo pipefail

REGISTRY_URL="${1:-http://localhost:8080/apis/registry/v3}"
GROUP="com.example.commerce"
CONTENT_TYPE="application/json"

echo "============================================="
echo "Schema Registration (Bottom-Up Order)"
echo "Registry: ${REGISTRY_URL}"
echo "Group:    ${GROUP}"
echo "============================================="
echo ""

# --- Helper function ---

register_artifact() {
  local artifact_id="$1"
  local version="$2"
  local schema_content="$3"
  local references="${4:-[]}"

  echo "Registering: ${GROUP}/${artifact_id} v${version}"

  # Build the request body. If references are provided, include them.
  local body
  body=$(cat <<ENDJSON
{
  "artifactId": "${artifact_id}",
  "artifactType": "AVRO",
  "firstVersion": {
    "version": "${version}",
    "content": {
      "content": $(echo "${schema_content}" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))'),
      "contentType": "application/json",
      "references": ${references}
    }
  }
}
ENDJSON
)

  local http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${REGISTRY_URL}/groups/${GROUP}/artifacts" \
    -H "Content-Type: ${CONTENT_TYPE}" \
    -d "${body}")

  if [[ "${http_code}" -ge 200 && "${http_code}" -lt 300 ]]; then
    echo "  -> Success (HTTP ${http_code})"
  else
    echo "  -> FAILED (HTTP ${http_code})"
    echo "  -> Response:"
    curl -s -X POST "${REGISTRY_URL}/groups/${GROUP}/artifacts" \
      -H "Content-Type: ${CONTENT_TYPE}" \
      -d "${body}" | python3 -m json.tool 2>/dev/null || true
    exit 1
  fi
  echo ""
}

# --- Step 0: Ensure the group exists ---

echo "Step 0: Creating group '${GROUP}' (if it does not exist)..."
curl -s -o /dev/null -w "" \
  -X POST "${REGISTRY_URL}/groups" \
  -H "Content-Type: ${CONTENT_TYPE}" \
  -d "{\"groupId\": \"${GROUP}\"}" || true
echo "  -> Done"
echo ""

# --- Step 1: Register leaf schemas (no dependencies) ---

echo "===== Step 1: Leaf schemas (no dependencies) ====="
echo ""

ADDRESS_SCHEMA='{
  "type": "record",
  "name": "Address",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "street", "type": "string" },
    { "name": "city", "type": "string" },
    { "name": "state", "type": ["null", "string"], "default": null },
    { "name": "postalCode", "type": ["null", "string"], "default": null },
    { "name": "country", "type": "string" }
  ]
}'

register_artifact "Address" "1.0.0" "${ADDRESS_SCHEMA}"

MONEY_SCHEMA='{
  "type": "record",
  "name": "Money",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "amount", "type": "double" },
    { "name": "currency", "type": "string" }
  ]
}'

register_artifact "Money" "1.0.0" "${MONEY_SCHEMA}"

# --- Step 2: Register schemas that depend only on leaf schemas ---

echo "===== Step 2: Schemas depending on leaves ====="
echo ""

PAYMENT_INFO_SCHEMA='{
  "type": "record",
  "name": "PaymentInfo",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "paymentId", "type": "string" },
    { "name": "method", "type": "string" },
    { "name": "amount", "type": "com.example.commerce.Money" },
    { "name": "paidAt", "type": { "type": "long", "logicalType": "timestamp-millis" } }
  ]
}'

PAYMENT_INFO_REFS='[
  {
    "name": "com.example.commerce.Money",
    "groupId": "com.example.commerce",
    "artifactId": "Money",
    "version": "1.0.0"
  }
]'

register_artifact "PaymentInfo" "1.0.0" "${PAYMENT_INFO_SCHEMA}" "${PAYMENT_INFO_REFS}"

# --- Step 3: Register root schemas ---

echo "===== Step 3: Root schemas ====="
echo ""

ORDER_EVENT_SCHEMA='{
  "type": "record",
  "name": "OrderEvent",
  "namespace": "com.example.commerce",
  "fields": [
    { "name": "orderId", "type": "string" },
    { "name": "timestamp", "type": { "type": "long", "logicalType": "timestamp-millis" } },
    { "name": "eventType", "type": "string" },
    { "name": "shippingAddress", "type": "com.example.commerce.Address" },
    { "name": "payment", "type": "com.example.commerce.PaymentInfo" }
  ]
}'

ORDER_EVENT_REFS='[
  {
    "name": "com.example.commerce.Address",
    "groupId": "com.example.commerce",
    "artifactId": "Address",
    "version": "1.0.0"
  },
  {
    "name": "com.example.commerce.PaymentInfo",
    "groupId": "com.example.commerce",
    "artifactId": "PaymentInfo",
    "version": "1.0.0"
  }
]'

register_artifact "OrderEvent" "1.0.0" "${ORDER_EVENT_SCHEMA}" "${ORDER_EVENT_REFS}"

# --- Summary ---

echo "============================================="
echo "Registration complete."
echo ""
echo "Registered artifacts in order:"
echo "  1. Address       v1.0.0  (leaf)"
echo "  2. Money         v1.0.0  (leaf)"
echo "  3. PaymentInfo   v1.0.0  (depends on Money)"
echo "  4. OrderEvent    v1.0.0  (depends on Address, PaymentInfo)"
echo ""
echo "Key points:"
echo "  - Leaf schemas (no dependencies) are registered first."
echo "  - Each schema's references point to already-registered artifacts."
echo "  - Version pins are explicit (1.0.0), not 'latest'."
echo "  - In a real pipeline, derive this order via topological sort"
echo "    of the dependency graph, not hardcoded order."
echo "============================================="
