import azure.functions as func
import json
import logging
import os

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import ManagedIdentityCredential
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.network.models import (
    FirewallPolicyApplicationRule,
    FirewallPolicyFilterRuleCollection,
    FirewallPolicyFilterRuleCollectionAction,
    FirewallPolicyRuleApplicationProtocol,
)

app = func.FunctionApp()

WEB_CATEGORIES = ["ComputerAndTechnology", "Business"]
RC_ACTION = "Allow"
WEB_CONTENT_RULE_NAME = "web content"
RC_PRIORITY_START = 4000
RC_PRIORITY_MAX = 4900
RC_PRIORITY_STEP = 10


def get_next_rc_priority(rule_collections: list) -> int:
    used = {
        rc.priority
        for rc in rule_collections
        if hasattr(rc, "priority")
        and rc.priority is not None
        and RC_PRIORITY_START <= rc.priority <= RC_PRIORITY_MAX
        and rc.priority % RC_PRIORITY_STEP == 0
    }
    for priority in range(RC_PRIORITY_START, RC_PRIORITY_MAX + RC_PRIORITY_STEP, RC_PRIORITY_STEP):
        if priority not in used:
            return priority
    raise ValueError(
        f"No available RC priority slots in range {RC_PRIORITY_START}–{RC_PRIORITY_MAX}. "
        f"All {(RC_PRIORITY_MAX - RC_PRIORITY_START) // RC_PRIORITY_STEP + 1} slots are occupied."
    )


def build_rule_collection(name: str, priority: int, source_cidrs: list) -> FirewallPolicyFilterRuleCollection:
    return FirewallPolicyFilterRuleCollection(
        name=name,
        priority=priority,
        action=FirewallPolicyFilterRuleCollectionAction(action_type=RC_ACTION),
        rule_collection_type="FirewallPolicyFilterRuleCollection",
        rules=[
            FirewallPolicyApplicationRule(
                name=WEB_CONTENT_RULE_NAME,
                rule_type="ApplicationRule",
                source_addresses=source_cidrs,
                protocols=[
                    FirewallPolicyRuleApplicationProtocol(protocol_type="Https", port=443)
                ],
                web_categories=WEB_CATEGORIES,
            )
        ],
    )


@app.function_name(name="CreateFirewallRule")
@app.route(route="create-fw-rule", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def create_fw_rule(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("CreateFirewallRule triggered")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    required = ["vnet_name", "resource_cidr", "vnet_resource_group", "fwp_name", "rcg_name"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return func.HttpResponse(
            json.dumps({"error": f"Missing required fields: {missing}"}),
            status_code=400,
            mimetype="application/json",
        )

    vnet_name: str = body["vnet_name"]
    resource_cidr: str = body["resource_cidr"]
    vnet_rg: str = body["vnet_resource_group"]
    fwp_name: str = body["fwp_name"]
    rcg_name: str = body["rcg_name"]

    subscription_id = os.environ["SUBSCRIPTION_ID"]
    fwp_rg = os.environ["FIREWALL_POLICY_RESOURCE_GROUP"]
    rc_name = f"{vnet_name}-application"

    try:
        credential = ManagedIdentityCredential()
        client = NetworkManagementClient(credential, subscription_id)

        # Query VNet for its current full address space — used as the authoritative source
        try:
            vnet = client.virtual_networks.get(vnet_rg, vnet_name)
        except ResourceNotFoundError:
            return func.HttpResponse(
                json.dumps({"error": f"VNet '{vnet_name}' not found in resource group '{vnet_rg}'"}),
                status_code=404,
                mimetype="application/json",
            )

        vnet_prefixes = sorted(vnet.address_space.address_prefixes or [])
        if not vnet_prefixes:
            return func.HttpResponse(
                json.dumps({"error": f"VNet '{vnet_name}' has no address prefixes"}),
                status_code=422,
                mimetype="application/json",
            )

        try:
            rcg = client.firewall_policy_rule_collection_groups.get(fwp_rg, fwp_name, rcg_name)
        except ResourceNotFoundError:
            return func.HttpResponse(
                json.dumps({"error": f"RCG '{rcg_name}' not found in policy '{fwp_name}'"}),
                status_code=404,
                mimetype="application/json",
            )

        rule_collections = list(rcg.rule_collections or [])
        existing_rc = next(
            (rc for rc in rule_collections if hasattr(rc, "name") and rc.name == rc_name),
            None,
        )

        if existing_rc is None:
            priority = get_next_rc_priority(rule_collections)
            rule_collections.append(build_rule_collection(rc_name, priority, vnet_prefixes))
            rcg.rule_collections = rule_collections
            client.firewall_policy_rule_collection_groups.begin_create_or_update(
                fwp_rg, fwp_name, rcg_name, rcg
            ).result()
            logging.info(f"Created RC '{rc_name}' (priority {priority}) with CIDRs {vnet_prefixes}")
            return func.HttpResponse(
                json.dumps({
                    "status": "created",
                    "firewall_policy": fwp_name,
                    "rcg_name": rcg_name,
                    "rc_name": rc_name,
                    "priority": priority,
                    "source_cidrs": vnet_prefixes,
                }),
                status_code=200,
                mimetype="application/json",
            )

        # RC already exists — sync source_addresses with VNet's current address space
        web_rule = next(
            (r for r in (existing_rc.rules or []) if r.name == WEB_CONTENT_RULE_NAME),
            None,
        )
        current_sources = sorted(web_rule.source_addresses or []) if web_rule else []

        if web_rule and current_sources == vnet_prefixes:
            logging.info(f"RC '{rc_name}' already up to date — no changes needed")
            return func.HttpResponse(
                json.dumps({
                    "status": "no_change",
                    "firewall_policy": fwp_name,
                    "rcg_name": rcg_name,
                    "rc_name": rc_name,
                    "source_cidrs": vnet_prefixes,
                }),
                status_code=200,
                mimetype="application/json",
            )

        if web_rule:
            web_rule.source_addresses = vnet_prefixes
        else:
            existing_rc.rules = list(existing_rc.rules or [])
            existing_rc.rules.append(
                FirewallPolicyApplicationRule(
                    name=WEB_CONTENT_RULE_NAME,
                    rule_type="ApplicationRule",
                    source_addresses=vnet_prefixes,
                    protocols=[
                        FirewallPolicyRuleApplicationProtocol(protocol_type="Https", port=443)
                    ],
                    web_categories=WEB_CATEGORIES,
                )
            )
        rcg.rule_collections = rule_collections
        client.firewall_policy_rule_collection_groups.begin_create_or_update(
            fwp_rg, fwp_name, rcg_name, rcg
        ).result()
        logging.info(f"Updated RC '{rc_name}': {current_sources} → {vnet_prefixes}")
        return func.HttpResponse(
            json.dumps({
                "status": "updated",
                "firewall_policy": fwp_name,
                "rcg_name": rcg_name,
                "rc_name": rc_name,
                "source_cidrs": vnet_prefixes,
                "previous_cidrs": current_sources,
            }),
            status_code=200,
            mimetype="application/json",
        )

    except ValueError as exc:
        logging.error(str(exc))
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=422,
            mimetype="application/json",
        )
    except Exception as exc:
        logging.error(f"Unexpected error: {exc}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=500,
            mimetype="application/json",
        )
