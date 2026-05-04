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


def build_rule_collection(name: str, priority: int, resource_cidr: str) -> FirewallPolicyFilterRuleCollection:
    return FirewallPolicyFilterRuleCollection(
        name=name,
        priority=priority,
        action=FirewallPolicyFilterRuleCollectionAction(action_type=RC_ACTION),
        rule_collection_type="FirewallPolicyFilterRuleCollection",
        rules=[
            FirewallPolicyApplicationRule(
                name=WEB_CONTENT_RULE_NAME,
                rule_type="ApplicationRule",
                source_addresses=[resource_cidr],
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

    required = ["vnet_name", "resource_cidr", "fwp_name", "rcg_name"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return func.HttpResponse(
            json.dumps({"error": f"Missing required fields: {missing}"}),
            status_code=400,
            mimetype="application/json",
        )

    vnet_name: str = body["vnet_name"]
    resource_cidr: str = body["resource_cidr"]
    fwp_name: str = body["fwp_name"]
    rcg_name: str = body["rcg_name"]

    subscription_id = os.environ["SUBSCRIPTION_ID"]
    fwp_rg = os.environ["FIREWALL_POLICY_RESOURCE_GROUP"]
    rc_name = f"{vnet_name}-application"

    try:
        credential = ManagedIdentityCredential()
        client = NetworkManagementClient(credential, subscription_id)

        try:
            rcg = client.firewall_policy_rule_collection_groups.get(fwp_rg, fwp_name, rcg_name)
        except ResourceNotFoundError:
            return func.HttpResponse(
                json.dumps({"error": f"RCG '{rcg_name}' not found in policy '{fwp_name}'"}),
                status_code=404,
                mimetype="application/json",
            )

        rule_collections = list(rcg.rule_collections or [])

        existing_rc_names = {rc.name for rc in rule_collections if hasattr(rc, "name")}
        if rc_name in existing_rc_names:
            return func.HttpResponse(
                json.dumps({"error": f"Rule collection '{rc_name}' already exists in RCG '{rcg_name}'"}),
                status_code=409,
                mimetype="application/json",
            )

        priority = get_next_rc_priority(rule_collections)
        rule_collections.append(build_rule_collection(rc_name, priority, resource_cidr))
        rcg.rule_collections = rule_collections

        client.firewall_policy_rule_collection_groups.begin_create_or_update(
            fwp_rg, fwp_name, rcg_name, rcg
        ).result()

        logging.info(f"Added RC '{rc_name}' (priority {priority}) to RCG '{rcg_name}' in '{fwp_name}'")

        return func.HttpResponse(
            json.dumps({
                "status": "success",
                "firewall_policy": fwp_name,
                "rcg_name": rcg_name,
                "rc_name": rc_name,
                "priority": priority,
                "source_cidr": resource_cidr,
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
