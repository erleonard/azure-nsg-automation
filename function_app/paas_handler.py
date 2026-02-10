import json
import logging
import os
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import SecurityRule
from azure.mgmt.resource import ResourceManagementClient

app = func.FunctionApp()

RULE_MAPPING_PATH = os.path.join(os.path.dirname(__file__), "tag-nsg-mapping.json")
with open(RULE_MAPPING_PATH, "r") as f:
    RULE_MAPPING = json.load(f)


def parse_resource_id(resource_id: str) -> dict:
    parts = resource_id.strip("/").split("/")
    parsed = {}
    for i in range(0, len(parts) - 1, 2):
        parsed[parts[i]] = parts[i + 1]
    return parsed


def get_matching_rules(tags: dict) -> list:
    matched = []
    for rule_def in RULE_MAPPING.get("rules", []):
        tag_key = rule_def["tag_key"]
        tag_value = rule_def["tag_value"]
        if tags.get(tag_key) == tag_value:
            matched.extend(rule_def["nsg_rules"])
    return matched


@app.function_name(name="PaasNsgTagHandler")
@app.event_grid_trigger(arg_name="event")
def paas_nsg_tag_handler(event: func.EventGridEvent):
    """
    Triggered when a Private Endpoint is created or updated.
    Reads tags from the parent PaaS resource and applies
    matching NSG rules to the Private Endpoint's subnet NSG.
    """
    logging.info(f"Event received: {event.id}, type: {event.event_type}")

    data = event.get_json()
    resource_id = data.get("resourceUri", "")

    # Only process Private Endpoint events
    if "Microsoft.Network/privateEndpoints" not in resource_id:
        logging.info(f"Skipping non-PE resource: {resource_id}")
        return

    parsed = parse_resource_id(resource_id)
    subscription_id = parsed.get("subscriptions")
    resource_group = parsed.get("resourceGroups")
    pe_name = parsed.get("privateEndpoints")

    if not all([subscription_id, resource_group, pe_name]):
        logging.error(f"Could not parse resource ID: {resource_id}")
        return

    credential = DefaultAzureCredential()
    network_client = NetworkManagementClient(credential, subscription_id)
    resource_client = ResourceManagementClient(credential, subscription_id)

    # 1. Get the Private Endpoint
    try:
        pe = network_client.private_endpoints.get(resource_group, pe_name)
    except Exception as e:
        logging.error(f"Failed to get Private Endpoint '{pe_name}': {e}")
        return

    # 2. Get the parent PaaS resource and its tags
    #    (PE -> privateLinkServiceConnections -> linked resource)
    tags = {}
    if pe.private_link_service_connections:
        linked_resource_id = (
            pe.private_link_service_connections[0]
            .private_link_service_id
        )
        # Try multiple API versions for broader compatibility
        api_versions = ["2023-01-01", "2022-09-01", "2021-04-01"]
        for api_version in api_versions:
            try:
                parent_resource = resource_client.resources.get_by_id(
                    linked_resource_id, api_version=api_version
                )
                tags = parent_resource.tags or {}
                logging.info(
                    f"Parent PaaS resource tags: {tags} "
                    f"(from {linked_resource_id}, API version: {api_version})"
                )
                break  # Success, exit the loop
            except Exception as e:
                if api_version == api_versions[-1]:
                    # Last attempt failed
                    logging.warning(
                        f"Could not read parent resource tags with any API version: {e}. "
                        f"Falling back to PE tags."
                    )
                    tags = pe.tags or {}
                # Otherwise, try next API version
    else:
        tags = pe.tags or {}

    # 3. Determine matching NSG rules
    rules_to_apply = get_matching_rules(tags)
    if not rules_to_apply:
        logging.info(f"No matching NSG rules for PE '{pe_name}' tags.")
        return

    # 4. Find the PE's subnet and its NSG
    for nic_ref in pe.network_interfaces:
        nic_parsed = parse_resource_id(nic_ref.id)
        nic_name = nic_parsed.get("networkInterfaces")
        nic_rg = nic_parsed.get("resourceGroups")

        try:
            nic = network_client.network_interfaces.get(nic_rg, nic_name)
        except Exception as e:
            logging.error(
                f"Failed to get NIC '{nic_name}' in resource group "
                f"'{nic_rg}': {e}"
            )
            continue

        for ip_config in nic.ip_configurations:
            subnet_id = ip_config.subnet.id
            subnet_parsed = parse_resource_id(subnet_id)
            vnet_name = subnet_parsed.get("virtualNetworks")
            subnet_name = subnet_parsed.get("subnets")
            subnet_rg = subnet_parsed.get("resourceGroups")

            try:
                subnet = network_client.subnets.get(
                    subnet_rg, vnet_name, subnet_name
                )
            except Exception as e:
                logging.error(
                    f"Failed to get subnet '{subnet_name}' in VNet "
                    f"'{vnet_name}': {e}"
                )
                continue

            if not subnet.network_security_group:
                logging.warning(
                    f"No NSG on subnet '{subnet_name}' for PE "
                    f"'{pe_name}'. Skipping."
                )
                continue

            nsg_parsed = parse_resource_id(
                subnet.network_security_group.id
            )
            nsg_name = nsg_parsed.get("networkSecurityGroups")
            nsg_rg = nsg_parsed.get("resourceGroups")

            # 5. Apply matching rules to the subnet NSG
            for rule in rules_to_apply:
                security_rule = SecurityRule(
                    protocol=rule["protocol"],
                    source_address_prefix=rule["source_address_prefix"],
                    destination_address_prefix=rule[
                        "destination_address_prefix"
                    ],
                    source_port_range=rule["source_port_range"],
                    destination_port_range=rule["destination_port_range"],
                    access=rule["access"],
                    priority=rule["priority"],
                    direction=rule["direction"],
                )
                try:
                    network_client.security_rules.begin_create_or_update(
                        nsg_rg,
                        nsg_name,
                        rule["name"],
                        security_rule,
                    ).result()
                    logging.info(
                        f"Applied rule '{rule['name']}' to subnet NSG "
                        f"'{nsg_name}' for PE '{pe_name}'."
                    )
                except Exception as e:
                    logging.error(
                        f"Failed to apply rule '{rule['name']}' to "
                        f"NSG '{nsg_name}': {e}"
                    )
