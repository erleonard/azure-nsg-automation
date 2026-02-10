import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import SecurityRule

app = func.FunctionApp()

# Load tag-to-NSG rule mapping configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'tag-nsg-mapping.json')
with open(CONFIG_PATH, 'r') as f:
    TAG_NSG_MAPPING = json.load(f)


def parse_resource_id(resource_id: str) -> Optional[Dict[str, str]]:
    """
    Parse Azure resource ID into components.
    
    Args:
        resource_id: Azure resource ID string
        
    Returns:
        Dictionary with subscription_id, resource_group, provider, resource_type, resource_name
        Returns None if parsing fails
    """
    pattern = r'/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<resource_group>[^/]+)/providers/(?P<provider>[^/]+)/(?P<resource_type>[^/]+)/(?P<resource_name>[^/]+)'
    match = re.match(pattern, resource_id, re.IGNORECASE)
    
    if not match:
        logging.error(f"Failed to parse resource ID: {resource_id}")
        return None
    
    return {
        'subscription_id': match.group('subscription'),
        'resource_group': match.group('resource_group'),
        'provider': match.group('provider'),
        'resource_type': match.group('resource_type'),
        'resource_name': match.group('resource_name')
    }


def get_matching_rules(tags: Dict[str, str]) -> List[Dict]:
    """
    Get NSG rules that match the given VM tags.
    
    Args:
        tags: Dictionary of VM tags
        
    Returns:
        List of matching NSG rule definitions
    """
    matching_rules = []
    
    for rule_config in TAG_NSG_MAPPING.get('rules', []):
        tag_key = rule_config.get('tag_key')
        tag_value = rule_config.get('tag_value')
        
        if tag_key in tags and tags[tag_key] == tag_value:
            matching_rules.extend(rule_config.get('nsg_rules', []))
            logging.info(f"Matched {len(rule_config.get('nsg_rules', []))} rule(s) for tag {tag_key}={tag_value}")
    
    return matching_rules


def get_vm_nsg(network_client: NetworkManagementClient, compute_client: ComputeManagementClient,
               subscription_id: str, resource_group: str, vm_name: str) -> Optional[Tuple[str, str, str]]:
    """
    Find the NSG associated with a VM (checks NIC-level first, then subnet-level).
    
    Args:
        network_client: Azure Network Management client
        compute_client: Azure Compute Management client
        subscription_id: Azure subscription ID
        resource_group: Resource group name
        vm_name: Virtual machine name
        
    Returns:
        Tuple of (nsg_name, nsg_resource_group, attachment_level) or None if no NSG found
    """
    try:
        # Get the VM to find its network interfaces
        vm = compute_client.virtual_machines.get(resource_group, vm_name)
        
        if not vm.network_profile or not vm.network_profile.network_interfaces:
            logging.warning(f"VM {vm_name} has no network interfaces")
            return None
        
        # Get the primary NIC
        primary_nic = None
        for nic_ref in vm.network_profile.network_interfaces:
            nic_id = nic_ref.id
            nic_parts = parse_resource_id(nic_id)
            if not nic_parts:
                continue
            
            nic = network_client.network_interfaces.get(
                nic_parts['resource_group'],
                nic_parts['resource_name']
            )
            
            if nic_ref.primary or len(vm.network_profile.network_interfaces) == 1:
                primary_nic = nic
                break
        
        if not primary_nic:
            logging.warning(f"No primary NIC found for VM {vm_name}")
            return None
        
        # Check if NIC has an NSG attached
        if primary_nic.network_security_group:
            nsg_id = primary_nic.network_security_group.id
            nsg_parts = parse_resource_id(nsg_id)
            if nsg_parts:
                logging.info(f"Found NIC-level NSG: {nsg_parts['resource_name']}")
                return (nsg_parts['resource_name'], nsg_parts['resource_group'], 'nic')
        
        # Check if the subnet has an NSG attached
        if primary_nic.ip_configurations:
            ip_config = primary_nic.ip_configurations[0]
            if ip_config.subnet:
                subnet_id = ip_config.subnet.id
                subnet_parts = parse_resource_id(subnet_id)
                
                if subnet_parts:
                    # Get the virtual network name from the subnet ID
                    vnet_pattern = r'/virtualNetworks/(?P<vnet_name>[^/]+)/subnets/'
                    vnet_match = re.search(vnet_pattern, subnet_id)
                    
                    if vnet_match:
                        vnet_name = vnet_match.group('vnet_name')
                        subnet = network_client.subnets.get(
                            subnet_parts['resource_group'],
                            vnet_name,
                            subnet_parts['resource_name']
                        )
                        
                        if subnet.network_security_group:
                            nsg_id = subnet.network_security_group.id
                            nsg_parts = parse_resource_id(nsg_id)
                            if nsg_parts:
                                logging.info(f"Found subnet-level NSG: {nsg_parts['resource_name']}")
                                return (nsg_parts['resource_name'], nsg_parts['resource_group'], 'subnet')
        
        logging.warning(f"No NSG found for VM {vm_name} at NIC or subnet level")
        return None
        
    except Exception as e:
        logging.error(f"Error finding NSG for VM {vm_name}: {str(e)}")
        return None


def apply_nsg_rules(network_client: NetworkManagementClient, nsg_name: str, 
                   nsg_resource_group: str, rules: List[Dict]) -> bool:
    """
    Apply NSG rules to the specified NSG.
    
    Args:
        network_client: Azure Network Management client
        nsg_name: NSG name
        nsg_resource_group: NSG resource group
        rules: List of rule definitions to apply
        
    Returns:
        True if all rules applied successfully, False otherwise
    """
    success = True
    
    for rule_def in rules:
        try:
            security_rule = SecurityRule(
                name=rule_def['name'],
                priority=rule_def['priority'],
                direction=rule_def['direction'],
                access=rule_def['access'],
                protocol=rule_def['protocol'],
                source_address_prefix=rule_def['source_address_prefix'],
                destination_address_prefix=rule_def['destination_address_prefix'],
                source_port_range=rule_def['source_port_range'],
                destination_port_range=rule_def['destination_port_range']
            )
            
            logging.info(f"Applying NSG rule {rule_def['name']} to NSG {nsg_name}")
            
            # Use begin_create_or_update for idempotent operation
            poller = network_client.security_rules.begin_create_or_update(
                nsg_resource_group,
                nsg_name,
                rule_def['name'],
                security_rule
            )
            
            # Wait for the operation to complete
            result = poller.result()
            logging.info(f"Successfully applied rule {rule_def['name']} to NSG {nsg_name}")
            
        except Exception as e:
            logging.error(f"Failed to apply rule {rule_def['name']} to NSG {nsg_name}: {str(e)}")
            success = False
    
    return success


@app.event_grid_trigger(arg_name="event")
def nsg_tag_handler(event: func.EventGridEvent):
    """
    Azure Function triggered by Event Grid events.
    Processes VM create/update events and applies NSG rules based on tags.
    
    Args:
        event: Event Grid event
    """
    try:
        logging.info(f"Processing Event Grid event: {event.id}")
        logging.info(f"Event type: {event.event_type}")
        logging.info(f"Subject: {event.subject}")
        
        # Parse the resource ID from the event subject
        resource_id = event.subject
        parsed_id = parse_resource_id(resource_id)
        
        if not parsed_id:
            logging.error(f"Could not parse resource ID from event subject: {resource_id}")
            return
        
        # Only process Virtual Machine events
        if parsed_id['resource_type'].lower() != 'virtualmachines':
            logging.info(f"Skipping non-VM resource: {parsed_id['resource_type']}")
            return
        
        subscription_id = parsed_id['subscription_id']
        resource_group = parsed_id['resource_group']
        vm_name = parsed_id['resource_name']
        
        logging.info(f"Processing VM: {vm_name} in resource group: {resource_group}")
        
        # Initialize Azure SDK clients with Managed Identity
        credential = DefaultAzureCredential()
        compute_client = ComputeManagementClient(credential, subscription_id)
        network_client = NetworkManagementClient(credential, subscription_id)
        
        # Get the VM to read its tags
        try:
            vm = compute_client.virtual_machines.get(resource_group, vm_name)
            tags = vm.tags or {}
            logging.info(f"VM tags: {tags}")
        except Exception as e:
            logging.error(f"Failed to get VM {vm_name}: {str(e)}")
            return
        
        # Get matching NSG rules based on tags
        matching_rules = get_matching_rules(tags)
        
        if not matching_rules:
            logging.info(f"No matching NSG rules found for VM {vm_name} tags")
            return
        
        logging.info(f"Found {len(matching_rules)} matching rules for VM {vm_name}")
        
        # Find the NSG associated with the VM
        nsg_info = get_vm_nsg(network_client, compute_client, subscription_id, resource_group, vm_name)
        
        if not nsg_info:
            logging.warning(f"No NSG found for VM {vm_name}. Cannot apply rules.")
            return
        
        nsg_name, nsg_resource_group, attachment_level = nsg_info
        logging.info(f"Applying rules to {attachment_level}-level NSG: {nsg_name}")
        
        # Apply the NSG rules
        success = apply_nsg_rules(network_client, nsg_name, nsg_resource_group, matching_rules)
        
        if success:
            logging.info(f"Successfully processed VM {vm_name} and applied all NSG rules")
        else:
            logging.warning(f"Completed processing VM {vm_name} but some rules failed to apply")
        
    except Exception as e:
        logging.error(f"Error processing event {event.id}: {str(e)}", exc_info=True)
