# Azure NSG Tag-Based Automation

An event-driven automation solution that dynamically assigns Azure Network Security Group (NSG) rules based on Virtual Machine tags. The solution uses Azure Event Grid to capture VM create/update events and an Azure Function to evaluate tags and apply corresponding NSG rules.

## What This Solution Does

This solution automates the management of NSG rules based on VM tagging:
1. When a VM is created or its tags are updated, Event Grid captures the event
2. An Azure Function is triggered automatically
3. The function reads the VM's tags and looks up matching NSG rules from a centralized configuration
4. It applies the appropriate NSG rules to the VM's network interface or subnet NSG
5. All operations are idempotent, ensuring safety even with repeated events

## Architecture Overview

```
┌─────────────────┐       ┌──────────────────────┐       ┌─────────────────────┐
│  Azure VM       │       │  Event Grid          │       │  Azure Function     │
│  (Created /     │──────▶│  System Topic        │──────▶│  (Evaluate Tags &   │
│   Tag Changed)  │ Event │  (Subscription)      │       │   Update NSG Rules) │
└─────────────────┘       └──────────────────────┘       └────────┬────────────┘
                                                                  │
                                                                  ▼
                                                         ┌─────────────────────┐
                                                         │  NSG Rules Updated  │
                                                         │  via Azure SDK      │
                                                         └─────────────────────┘
```

## Components

### Event Grid System Topic
- Source: Azure Subscription
- Topic Type: `Microsoft.Resources.Subscriptions`
- Captures resource write events across the subscription

### Event Grid Subscription
- Filters for `Microsoft.Resources.ResourceWriteSuccess` events
- Advanced filter: Only processes `Microsoft.Compute/virtualMachines` subjects
- Destination: Azure Function endpoint

### Azure Function
- Runtime: Python 3.11
- Trigger: Event Grid
- Authentication: System-Assigned Managed Identity
- Processes VM events and applies NSG rules based on tags

### Network Security Groups (NSGs)
- Can be attached at NIC level or subnet level
- Rules are created/updated idempotently
- Priority ranges and naming conventions defined in configuration

### Managed Identity
- System-assigned identity for the Function App
- RBAC roles: Reader (subscription), Network Contributor (NSG modifications)
- No credential management required

### Log Analytics / Application Insights
- All function executions are logged
- Enables auditing and troubleshooting
- Monitors rule application success/failure

### Configuration (tag-nsg-mapping.json)
- Centralized mapping of tags to NSG rules
- Can be updated without code changes
- Supports multiple tag-based rules

## Prerequisites

1. Azure subscription with appropriate permissions
2. Azure CLI installed and authenticated
3. Bicep CLI installed (comes with Azure CLI)
4. Bash shell for running scripts
5. Existing NSGs attached to VMs or subnets (or create them as needed)

## Deployment Steps

### 1. Deploy Infrastructure with Bicep

```bash
# Set variables
RESOURCE_GROUP="rg-nsg-automation"
LOCATION="eastus"
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

# Create resource group
az group create --name $RESOURCE_GROUP --location $LOCATION

# Deploy Bicep template
az deployment group create \
  --resource-group $RESOURCE_GROUP \
  --template-file infra/main.bicep \
  --parameters subscriptionId=$SUBSCRIPTION_ID \
  --parameters functionAppName=fn-nsg-tag-automation

# Get the Function App's Managed Identity Principal ID
PRINCIPAL_ID=$(az deployment group show \
  --resource-group $RESOURCE_GROUP \
  --name main \
  --query properties.outputs.functionAppPrincipalId.value -o tsv)

echo "Function App Managed Identity Principal ID: $PRINCIPAL_ID"
```

### 2. Assign RBAC Roles

```bash
# Run the RBAC assignment script
chmod +x scripts/assign-rbac.sh
./scripts/assign-rbac.sh $RESOURCE_GROUP $SUBSCRIPTION_ID
```

This assigns:
- **Reader** role on the subscription (to read VM details)
- **Network Contributor** role on the subscription (to modify NSG rules)

### 3. Deploy Function Code

```bash
# Navigate to the function app directory
cd function_app

# Deploy the function code
func azure functionapp publish fn-nsg-tag-automation
```

Alternatively, set up CI/CD with GitHub Actions or Azure DevOps.

### 4. Verify Deployment

```bash
# Check that Event Grid subscription is active
az eventgrid event-subscription list \
  --source-resource-id /subscriptions/$SUBSCRIPTION_ID \
  --query "[].{name:name, provisioningState:provisioningState}"

# Check function app status
az functionapp show \
  --resource-group $RESOURCE_GROUP \
  --name fn-nsg-tag-automation \
  --query "{state:state, identity:identity.principalId}"
```

## Configuration

### Tag-to-NSG Rule Mapping

The `function_app/tag-nsg-mapping.json` file defines how VM tags map to NSG rules. Example:

```json
{
  "rules": [
    {
      "tag_key": "Dept",
      "tag_value": "Finance",
      "nsg_rule": {
        "name": "AllowFinanceHTTPS",
        "priority": 200,
        "direction": "Inbound",
        "access": "Allow",
        "protocol": "Tcp",
        "source_address_prefix": "10.10.20.0/24",
        "destination_address_prefix": "*",
        "source_port_range": "*",
        "destination_port_range": "443"
      }
    }
  ]
}
```

To add or modify rules:
1. Edit `function_app/tag-nsg-mapping.json`
2. Redeploy the function app or update the file in the deployed environment

## Best Practices

### 1. Idempotent Operations
- The function uses `begin_create_or_update` for NSG rules
- Repeated events for the same VM are safe and won't create duplicate rules
- Updates existing rules if they already exist

### 2. Managed Identity
- System-assigned identity eliminates credential management
- Least-privilege RBAC: only Reader and Network Contributor roles
- Credentials automatically rotated by Azure

### 3. Advanced Event Filtering
- Event Grid subscription filters to only VM events
- Reduces unnecessary function invocations
- Lower costs and improved performance

### 4. Centralized Rule Mapping
- Configuration file (`tag-nsg-mapping.json`) separates rules from code
- Easy to update without redeploying code
- Version controlled alongside infrastructure

### 5. Logging & Monitoring
- Application Insights captures all function executions
- Structured logging for easy querying
- Alerts can be configured on failures

### 6. Error Handling
- Graceful failure handling with comprehensive logging
- Invalid events are logged and skipped
- Network errors are caught and reported

## End-to-End Flow

1. **VM Creation/Update**: A VM is created or its tags are modified
2. **Event Emission**: Azure Resource Manager emits a `Microsoft.Resources.ResourceWriteSuccess` event
3. **Event Grid Filtering**: Event Grid checks if the event matches the subscription filter (VM resources only)
4. **Function Trigger**: If matched, Event Grid triggers the Azure Function with the event payload
5. **Parse Event**: Function parses the resource ID to extract subscription, resource group, and VM name
6. **Resource Type Check**: Verifies the event is for a Virtual Machine resource type
7. **Read VM Tags**: Uses Azure Compute SDK with Managed Identity to read the VM's current tags
8. **Rule Lookup**: Matches tags against the `tag-nsg-mapping.json` configuration
9. **Find NSG**: Locates the VM's network interface and associated NSG (NIC-level first, then subnet-level)
10. **Apply Rules**: Creates or updates NSG rules using the Network SDK
11. **Logging**: Logs success or failure to Application Insights for auditing

## Troubleshooting

### Function Not Triggering
- Verify Event Grid subscription is active and endpoint is healthy
- Check Event Grid metrics for delivery failures
- Ensure the function app is running

### Permission Errors
- Verify Managed Identity has Reader and Network Contributor roles
- Check role assignment scope (should be subscription or resource group level)

### NSG Rules Not Applied
- Verify NSG exists and is attached to the NIC or subnet
- Check function logs in Application Insights for error details
- Ensure tag-to-rule mapping is correct

### View Logs
```bash
# Stream function logs
func azure functionapp logstream fn-nsg-tag-automation

# Or query Application Insights
az monitor app-insights query \
  --app <app-insights-name> \
  --analytics-query "traces | where message contains 'nsg_tag_handler' | order by timestamp desc | take 50"
```

## Security Considerations

- Use Managed Identity instead of service principals
- Apply least-privilege RBAC roles
- Store sensitive configuration in Azure Key Vault if needed
- Enable diagnostic logging for audit trail
- Regularly review NSG rules and tag mappings

## Cost Optimization

- Event Grid charges per million operations (first 100K free per month)
- Azure Functions Consumption plan charges per execution and GB-s
- Minimize function invocations with advanced filtering
- Monitor costs in Azure Cost Management

## Contributing

Contributions are welcome! Please submit pull requests with:
- Clear description of changes
- Updated documentation if applicable
- Tested code changes

## License

See [LICENSE](LICENSE) file for details.