#!/usr/bin/env bash
# =============================================================================
# validate-compatibility.sh
#
# Validates that a new schema version is compatible with the existing schema
# registered in Apicurio Registry. Uses the Apicurio Registry REST API to
# check compatibility before registering a new version.
#
# Usage:
#   ./validate-compatibility.sh <registry-url> <group-id> <artifact-id> <schema-file>
#
# Examples:
#   # Check compatibility against a local Apicurio Registry instance
#   ./validate-compatibility.sh http://localhost:8080 reddit-pipeline reddit-stream-value ./reddit-stream-v2-valid.json
#
#   # Check compatibility against a remote instance
#   ./validate-compatibility.sh https://registry.example.com reddit-pipeline kafka-predictions-value ./kafka-predictions-v2.json
#
# Prerequisites:
#   - curl
#   - jq (optional, for pretty-printing responses)
#
# Exit codes:
#   0 - Schema is compatible
#   1 - Schema is NOT compatible or an error occurred
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [[ $# -lt 4 ]]; then
    echo "Usage: $0 <registry-url> <group-id> <artifact-id> <schema-file>"
    echo ""
    echo "Arguments:"
    echo "  registry-url   Base URL of the Apicurio Registry (e.g., http://localhost:8080)"
    echo "  group-id       Group ID for the artifact (e.g., reddit-pipeline)"
    echo "  artifact-id    Artifact ID to check against (e.g., reddit-stream-value)"
    echo "  schema-file    Path to the JSON Schema file to validate"
    exit 1
fi

REGISTRY_URL="${1%/}"  # Strip trailing slash
GROUP_ID="$2"
ARTIFACT_ID="$3"
SCHEMA_FILE="$4"

# Apicurio Registry v3 API base path
API_BASE="${REGISTRY_URL}/apis/registry/v3"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: Schema file not found: $SCHEMA_FILE"
    exit 1
fi

if ! command -v curl &>/dev/null; then
    echo "ERROR: curl is required but not installed."
    exit 1
fi

HAS_JQ=false
if command -v jq &>/dev/null; then
    HAS_JQ=true
fi

# ---------------------------------------------------------------------------
# Step 1: Verify the artifact exists
# ---------------------------------------------------------------------------
echo "==> Checking if artifact '${GROUP_ID}/${ARTIFACT_ID}' exists..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    "${API_BASE}/groups/${GROUP_ID}/artifacts/${ARTIFACT_ID}")

if [[ "$HTTP_CODE" == "404" ]]; then
    echo "WARNING: Artifact '${GROUP_ID}/${ARTIFACT_ID}' does not exist."
    echo "This is the first version -- no compatibility check needed."
    echo "Proceeding to register the initial schema..."
    echo ""

    # Register the artifact for the first time
    REGISTER_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-Registry-ArtifactId: ${ARTIFACT_ID}" \
        -H "X-Registry-ArtifactType: JSON" \
        -d "{
            \"artifactId\": \"${ARTIFACT_ID}\",
            \"artifactType\": \"JSON\",
            \"firstVersion\": {
                \"version\": \"1\",
                \"content\": {
                    \"content\": $(cat "$SCHEMA_FILE" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'),
                    \"contentType\": \"application/json\"
                }
            }
        }" \
        "${API_BASE}/groups/${GROUP_ID}/artifacts")

    REG_HTTP_CODE=$(echo "$REGISTER_RESPONSE" | tail -1)
    REG_BODY=$(echo "$REGISTER_RESPONSE" | sed '$d')

    if [[ "$REG_HTTP_CODE" =~ ^2 ]]; then
        echo "SUCCESS: Initial schema registered."
        if $HAS_JQ; then
            echo "$REG_BODY" | jq .
        else
            echo "$REG_BODY"
        fi
        exit 0
    else
        echo "ERROR: Failed to register initial schema (HTTP $REG_HTTP_CODE)."
        echo "$REG_BODY"
        exit 1
    fi
elif [[ ! "$HTTP_CODE" =~ ^2 ]]; then
    echo "ERROR: Unexpected response from registry (HTTP $HTTP_CODE)."
    echo "Verify the registry URL and artifact coordinates."
    exit 1
fi

echo "    Artifact exists."

# ---------------------------------------------------------------------------
# Step 2: Get the current artifact metadata (including compatibility rule)
# ---------------------------------------------------------------------------
echo "==> Retrieving artifact rules..."

RULES_RESPONSE=$(curl -s -w "\n%{http_code}" \
    "${API_BASE}/groups/${GROUP_ID}/artifacts/${ARTIFACT_ID}/rules")

RULES_HTTP_CODE=$(echo "$RULES_RESPONSE" | tail -1)
RULES_BODY=$(echo "$RULES_RESPONSE" | sed '$d')

if [[ "$RULES_HTTP_CODE" =~ ^2 ]]; then
    echo "    Current rules:"
    if $HAS_JQ; then
        echo "$RULES_BODY" | jq .
    else
        echo "    $RULES_BODY"
    fi
else
    echo "    No artifact-level rules configured (using global defaults)."
fi

# ---------------------------------------------------------------------------
# Step 3: Test compatibility by creating a dry-run version
# ---------------------------------------------------------------------------
echo ""
echo "==> Testing compatibility of '${SCHEMA_FILE}' against '${GROUP_ID}/${ARTIFACT_ID}'..."
echo ""

SCHEMA_CONTENT=$(cat "$SCHEMA_FILE" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')

COMPAT_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -H "X-Registry-DryRun: true" \
    -d "{
        \"content\": {
            \"content\": ${SCHEMA_CONTENT},
            \"contentType\": \"application/json\"
        }
    }" \
    "${API_BASE}/groups/${GROUP_ID}/artifacts/${ARTIFACT_ID}/versions")

COMPAT_HTTP_CODE=$(echo "$COMPAT_RESPONSE" | tail -1)
COMPAT_BODY=$(echo "$COMPAT_RESPONSE" | sed '$d')

if [[ "$COMPAT_HTTP_CODE" =~ ^2 ]]; then
    echo "COMPATIBLE: The new schema is compatible with the existing version."
    echo ""
    if $HAS_JQ; then
        echo "$COMPAT_BODY" | jq .
    else
        echo "$COMPAT_BODY"
    fi
    echo ""
    echo "You can safely register this schema as a new version."
    exit 0
else
    echo "INCOMPATIBLE: The new schema is NOT compatible."
    echo ""
    echo "HTTP Status: ${COMPAT_HTTP_CODE}"
    if $HAS_JQ; then
        echo "$COMPAT_BODY" | jq .
    else
        echo "$COMPAT_BODY"
    fi
    echo ""
    echo "Review the error details above and adjust your schema evolution."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 4 (optional): Register the new version if --register flag is passed
# ---------------------------------------------------------------------------
# To actually register after validation, you would run:
#
#   curl -X POST \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"content\": {
#             \"content\": $(cat schema.json | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))'),
#             \"contentType\": \"application/json\"
#         }
#     }" \
#     "${API_BASE}/groups/${GROUP_ID}/artifacts/${ARTIFACT_ID}/versions"
#
# The registry will enforce compatibility rules before accepting the version.
