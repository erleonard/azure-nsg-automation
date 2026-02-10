@description('Event Grid System Topic to attach the subscription to')
param systemTopic object

@description('Function App resource for the destination')
param functionApp object

resource peEventSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2023-12-15-preview' = {
  parent: systemTopic
  name: 'pe-tag-nsg-sub'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: '${functionApp.id}/functions/PaasNsgTagHandler'
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Resources.ResourceWriteSuccess'
      ]
      advancedFilters: [
        {
          operatorType: 'StringContains'
          key: 'subject'
          values: [ 'Microsoft.Network/privateEndpoints' ]
        }
      ]
    }
  }
}

output eventSubscriptionName string = peEventSubscription.name
