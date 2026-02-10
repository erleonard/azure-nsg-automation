#!/bin/bash

# Script to assign RBAC roles to the Function App's Managed Identity
# Usage: ./assign-rbac.sh <resource-group-name> <subscription-id>

set -e

# Check if required arguments are provided
if [ $# -lt 2 ]; then
    echo "Usage: $0 <resource-group-name> <subscription-id>"
    echo "Example: $0 rg-nsg-automation xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    exit 1
fi

RESOURCE_GROUP=$1
SUBSCRIPTION_ID=$2
FUNCTION_APP_NAME="fn-nsg-tag-automation"

echo "================================================"
echo "Assigning RBAC Roles to Function App Managed Identity"
echo "================================================"
echo "Resource Group: $RESOURCE_GROUP"
echo "Subscription ID: $SUBSCRIPTION_ID"
echo "Function App: $FUNCTION_APP_NAME"
echo ""

# Get the Function App's Managed Identity Principal ID
echo "Retrieving Function App Managed Identity Principal ID..."
PRINCIPAL_ID=$(az functionapp identity show \
    --name $FUNCTION_APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --query principalId \
    --output tsv)

if [ -z "$PRINCIPAL_ID" ]; then
    echo "Error: Could not retrieve Managed Identity Principal ID"
    echo "Ensure the Function App exists and has a System-Assigned Managed Identity enabled"
    exit 1
fi

echo "Managed Identity Principal ID: $PRINCIPAL_ID"
echo ""

# Assign Reader role at subscription scope
echo "Assigning 'Reader' role at subscription scope..."
az role assignment create \
    --assignee $PRINCIPAL_ID \
    --role "Reader" \
    --scope "/subscriptions/$SUBSCRIPTION_ID" \
    --output none

echo "✓ Reader role assigned successfully"
echo ""

# Assign Network Contributor role at subscription scope
echo "Assigning 'Network Contributor' role at subscription scope..."
az role assignment create \
    --assignee $PRINCIPAL_ID \
    --role "Network Contributor" \
    --scope "/subscriptions/$SUBSCRIPTION_ID" \
    --output none

echo "✓ Network Contributor role assigned successfully"
echo ""

# Verify role assignments
echo "Verifying role assignments..."
echo ""
az role assignment list \
    --assignee $PRINCIPAL_ID \
    --scope "/subscriptions/$SUBSCRIPTION_ID" \
    --query "[].{Role:roleDefinitionName, Scope:scope}" \
    --output table

echo ""
echo "================================================"
echo "RBAC Role Assignment Complete!"
echo "================================================"
echo ""
echo "The Function App can now:"
echo "  - Read VM details and tags (Reader role)"
echo "  - Modify NSG rules (Network Contributor role)"
echo ""
