# Copyright 2026 Agencia Maya.
#
# Write tools extension for the official Google Ads MCP server.
# Follows the same conventions as ads_mcp/tools/search.py.
#
# SAFETY MODEL (inspired by FGRibreau/mcp-google-ads):
#   - Master switch: GOOGLE_ADS_MCP_ENABLE_WRITES must be "true"
#   - Campaigns are ALWAYS created PAUSED
#   - Daily budget capped by GOOGLE_ADS_MCP_MAX_DAILY_BUDGET (default 100.0)
#   - Enabling entities requires confirm=True
#   - Removing entities requires confirm="EXCLUIR"
#   - Character limits validated BEFORE calling the API

"""Write (mutate) tools for the Google Ads MCP server."""

import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

import ads_mcp.utils as utils

mutate_mcp = FastMCP("mutate")

# ---------------------------------------------------------------- guardrails

_WRITES_ENABLED = (
    os.environ.get("GOOGLE_ADS_MCP_ENABLE_WRITES", "false").lower() == "true"
)
_MAX_DAILY_BUDGET = float(
    os.environ.get("GOOGLE_ADS_MCP_MAX_DAILY_BUDGET", "100.0")
)

_H_LIMIT, _D_LIMIT = 30, 90
_SL_TEXT, _SL_DESC, _CALLOUT = 25, 35, 25


def _guard_writes() -> None:
    if not _WRITES_ENABLED:
        raise ToolError(
            "Escrita desabilitada. Defina GOOGLE_ADS_MCP_ENABLE_WRITES=true "
            "no servidor para habilitar operações de mutate."
        )


def _guard_budget(amount: float) -> None:
    if amount > _MAX_DAILY_BUDGET:
        raise ToolError(
            f"Orçamento diário {amount:.2f} excede o teto configurado de "
            f"{_MAX_DAILY_BUDGET:.2f} (GOOGLE_ADS_MCP_MAX_DAILY_BUDGET). "
            "Ajuste o teto no servidor ou reduza o orçamento."
        )


def _guard_chars(texts: List[str], limit: int, label: str) -> None:
    for t in texts:
        if len(t) > limit:
            raise ToolError(
                f"{label} excede {limit} caracteres ({len(t)}): '{t}'"
            )


def _cid(customer_id: str) -> str:
    return customer_id.replace("-", "").strip()


def _micros(amount: float) -> int:
    return int(round(amount * 1_000_000))


def _handle(ex: GoogleAdsException) -> ToolError:
    msgs = [e.message for e in ex.failure.errors]
    return ToolError(f"Google Ads API error: {'; '.join(msgs)}")


# ------------------------------------------------------------------- budgets


@mutate_mcp.tool
def create_campaign_budget(
    customer_id: str, name: str, daily_amount: float
) -> Dict[str, Any]:
    """Creates a daily campaign budget (not shared).

    Args:
        customer_id: The customer account id.
        name: Budget name (must be unique in the account).
        daily_amount: Daily amount in the ACCOUNT CURRENCY (e.g. 40.0 = R$40/day).
    """
    _guard_writes()
    _guard_budget(daily_amount)
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("CampaignBudgetService")

    op = client.get_type("CampaignBudgetOperation")
    budget = op.create
    budget.name = name
    budget.amount_micros = _micros(daily_amount)
    budget.delivery_method = (
        client.enums.BudgetDeliveryMethodEnum.STANDARD
    )
    budget.explicitly_shared = False
    try:
        resp = svc.mutate_campaign_budgets(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"budget_resource_name": resp.results[0].resource_name}


@mutate_mcp.tool
def update_campaign_budget(
    customer_id: str, budget_id: str, daily_amount: float
) -> Dict[str, Any]:
    """Updates the daily amount of an existing campaign budget.

    Args:
        customer_id: The customer account id.
        budget_id: The numeric id of the campaign budget.
        daily_amount: New daily amount in the account currency.
    """
    _guard_writes()
    _guard_budget(daily_amount)
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("CampaignBudgetService")

    op = client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = (
        f"customers/{customer_id}/campaignBudgets/{budget_id}"
    )
    budget.amount_micros = _micros(daily_amount)
    client.copy_from(
        op.update_mask,
        protobuf_helpers.field_mask(None, budget._pb),
    )
    try:
        resp = svc.mutate_campaign_budgets(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"updated": resp.results[0].resource_name}


# ----------------------------------------------------------------- campaigns


@mutate_mcp.tool
def create_search_campaign(
    customer_id: str,
    name: str,
    budget_resource_name: str,
    max_cpc: Optional[float] = None,
    geo_ids: List[int] = [2076],
    language_ids: List[int] = [1014],
) -> Dict[str, Any]:
    """Creates a Search campaign. ALWAYS created PAUSED (safety guardrail).

    Uses Maximize Clicks (TARGET_SPEND) bidding, optionally with a max CPC
    ceiling — the right strategy for accounts without conversion history.
    Search partners and Display network are disabled by default.

    Args:
        customer_id: The customer account id.
        name: Campaign name.
        budget_resource_name: Resource name returned by create_campaign_budget.
        max_cpc: Optional CPC bid ceiling in account currency.
        geo_ids: Geo target constant ids. Default [2076] = Brazil.
        language_ids: Language constant ids. Default [1014] = Portuguese.
    """
    _guard_writes()
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("CampaignService")

    op = client.get_type("CampaignOperation")
    c = op.create
    c.name = name
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    c.campaign_budget = budget_resource_name
    # Activate TARGET_SPEND (Maximize Clicks). Only set a bid ceiling when
    # provided — a ceiling of 0 is rejected by the API ("Too low").
    if max_cpc:
        c.target_spend.cpc_bid_ceiling_micros = _micros(max_cpc)
    else:
        client.copy_from(
            c.target_spend, client.get_type("TargetSpend")
        )
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = False
    c.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    try:
        resp = svc.mutate_campaigns(
            customer_id=customer_id, operations=[op]
        )
        campaign_rn = resp.results[0].resource_name
    except GoogleAdsException as ex:
        raise _handle(ex)

    # Geo + language criteria
    crit_svc = utils.get_googleads_service("CampaignCriterionService")
    ops = []
    for gid in geo_ids:
        o = client.get_type("CampaignCriterionOperation")
        o.create.campaign = campaign_rn
        o.create.location.geo_target_constant = (
            f"geoTargetConstants/{gid}"
        )
        ops.append(o)
    for lid in language_ids:
        o = client.get_type("CampaignCriterionOperation")
        o.create.campaign = campaign_rn
        o.create.language.language_constant = f"languageConstants/{lid}"
        ops.append(o)
    try:
        crit_svc.mutate_campaign_criteria(
            customer_id=customer_id, operations=ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)

    return {
        "campaign_resource_name": campaign_rn,
        "status": "PAUSED",
        "note": "Campanha criada PAUSADA. Use update_entity_status para ativar após revisão.",
    }


# ----------------------------------------------------------------- ad groups


@mutate_mcp.tool
def create_ad_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    max_cpc: Optional[float] = None,
) -> Dict[str, Any]:
    """Creates an ad group (ENABLED) inside a campaign.

    The paused campaign is the single safety gate — the ad group itself is
    enabled so activating the campaign later requires one call, not many.

    Args:
        customer_id: The customer account id.
        campaign_id: Numeric id of the parent campaign.
        name: Ad group name.
        max_cpc: Optional default CPC bid in account currency.
    """
    _guard_writes()
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("AdGroupService")

    op = client.get_type("AdGroupOperation")
    ag = op.create
    ag.name = name
    ag.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    if max_cpc:
        ag.cpc_bid_micros = _micros(max_cpc)
    try:
        resp = svc.mutate_ad_groups(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"ad_group_resource_name": resp.results[0].resource_name}


# ------------------------------------------------------------------ keywords

_MATCH = {"EXACT": "EXACT", "PHRASE": "PHRASE", "BROAD": "BROAD"}


@mutate_mcp.tool
def add_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Adds keywords to an ad group.

    Args:
        customer_id: The customer account id.
        ad_group_id: Numeric id of the ad group.
        keywords: List of {"text": "...", "match_type": "EXACT|PHRASE|BROAD"}.
    """
    _guard_writes()
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("AdGroupCriterionService")

    ops = []
    for kw in keywords:
        mt = kw.get("match_type", "PHRASE").upper()
        if mt not in _MATCH:
            raise ToolError(f"match_type inválido: {mt}")
        o = client.get_type("AdGroupCriterionOperation")
        crit = o.create
        crit.ad_group = (
            f"customers/{customer_id}/adGroups/{ad_group_id}"
        )
        crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, mt
        )
        ops.append(o)
    try:
        resp = svc.mutate_ad_group_criteria(
            customer_id=customer_id, operations=ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"added": len(resp.results)}


@mutate_mcp.tool
def add_negative_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: List[str],
    match_type: str = "BROAD",
) -> Dict[str, Any]:
    """Adds negative keywords at the CAMPAIGN level.

    Args:
        customer_id: The customer account id.
        campaign_id: Numeric id of the campaign.
        keywords: List of keyword texts to block.
        match_type: EXACT, PHRASE or BROAD (default BROAD).
    """
    _guard_writes()
    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("CampaignCriterionService")

    mt = match_type.upper()
    if mt not in _MATCH:
        raise ToolError(f"match_type inválido: {mt}")

    ops = []
    for text in keywords:
        o = client.get_type("CampaignCriterionOperation")
        crit = o.create
        crit.campaign = (
            f"customers/{customer_id}/campaigns/{campaign_id}"
        )
        crit.negative = True
        crit.keyword.text = text
        crit.keyword.match_type = getattr(
            client.enums.KeywordMatchTypeEnum, mt
        )
        ops.append(o)
    try:
        resp = svc.mutate_campaign_criteria(
            customer_id=customer_id, operations=ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"added_negatives": len(resp.results)}


# ---------------------------------------------------------------------- RSAs


@mutate_mcp.tool
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    headlines: List[str],
    descriptions: List[str],
    final_url: str,
    path1: str = "",
    path2: str = "",
    pin_first_headline: bool = True,
) -> Dict[str, Any]:
    """Creates a Responsive Search Ad in an ad group.

    Validates character limits BEFORE calling the API:
    headlines <= 30 chars (3-15 items), descriptions <= 90 chars (2-4 items).

    Args:
        customer_id: The customer account id.
        ad_group_id: Numeric id of the ad group.
        headlines: 3 to 15 headlines, max 30 chars each.
        descriptions: 2 to 4 descriptions, max 90 chars each.
        final_url: Landing page URL.
        path1: Optional display path 1 (max 15 chars).
        path2: Optional display path 2 (max 15 chars).
        pin_first_headline: Pin headlines[0] to position 1 (keyword-match).
    """
    _guard_writes()
    if not 3 <= len(headlines) <= 15:
        raise ToolError("RSA exige de 3 a 15 headlines.")
    if not 2 <= len(descriptions) <= 4:
        raise ToolError("RSA exige de 2 a 4 descriptions.")
    _guard_chars(headlines, _H_LIMIT, "Headline")
    _guard_chars(descriptions, _D_LIMIT, "Description")
    if path1:
        _guard_chars([path1], 15, "Path 1")
    if path2:
        _guard_chars([path2], 15, "Path 2")

    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc = utils.get_googleads_service("AdGroupAdService")

    op = client.get_type("AdGroupAdOperation")
    aga = op.create
    aga.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    aga.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = aga.ad
    ad.final_urls.append(final_url)
    rsa = ad.responsive_search_ad
    for i, h in enumerate(headlines):
        asset = client.get_type("AdTextAsset")
        asset.text = h
        if i == 0 and pin_first_headline:
            asset.pinned_field = (
                client.enums.ServedAssetFieldTypeEnum.HEADLINE_1
            )
        rsa.headlines.append(asset)
    for d in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d
        rsa.descriptions.append(asset)
    if path1:
        rsa.path1 = path1
    if path2:
        rsa.path2 = path2
    try:
        resp = svc.mutate_ad_group_ads(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"ad_resource_name": resp.results[0].resource_name}


# ------------------------------------------------------------------- assets


@mutate_mcp.tool
def create_sitelinks(
    customer_id: str,
    campaign_id: str,
    sitelinks: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Creates sitelink assets and links them to a campaign.

    Args:
        customer_id: The customer account id.
        campaign_id: Numeric id of the campaign.
        sitelinks: List of {"text","description1","description2","url"}.
            text <= 25 chars, descriptions <= 35 chars each.
    """
    _guard_writes()
    for sl in sitelinks:
        _guard_chars([sl["text"]], _SL_TEXT, "Sitelink text")
        _guard_chars(
            [sl.get("description1", ""), sl.get("description2", "")],
            _SL_DESC,
            "Sitelink description",
        )

    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    asset_svc = utils.get_googleads_service("AssetService")

    asset_ops = []
    for sl in sitelinks:
        o = client.get_type("AssetOperation")
        a = o.create
        a.final_urls.append(sl["url"])
        a.sitelink_asset.link_text = sl["text"]
        if sl.get("description1"):
            a.sitelink_asset.description1 = sl["description1"]
        if sl.get("description2"):
            a.sitelink_asset.description2 = sl["description2"]
        asset_ops.append(o)
    try:
        asset_resp = asset_svc.mutate_assets(
            customer_id=customer_id, operations=asset_ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)

    ca_svc = utils.get_googleads_service("CampaignAssetService")
    link_ops = []
    for result in asset_resp.results:
        o = client.get_type("CampaignAssetOperation")
        o.create.campaign = (
            f"customers/{customer_id}/campaigns/{campaign_id}"
        )
        o.create.asset = result.resource_name
        o.create.field_type = client.enums.AssetFieldTypeEnum.SITELINK
        link_ops.append(o)
    try:
        ca_svc.mutate_campaign_assets(
            customer_id=customer_id, operations=link_ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"sitelinks_created": len(asset_resp.results)}


@mutate_mcp.tool
def create_callouts(
    customer_id: str, campaign_id: str, texts: List[str]
) -> Dict[str, Any]:
    """Creates callout assets (<= 25 chars each) and links them to a campaign.

    Args:
        customer_id: The customer account id.
        campaign_id: Numeric id of the campaign.
        texts: Callout texts, max 25 chars each. Recommended 6+.
    """
    _guard_writes()
    _guard_chars(texts, _CALLOUT, "Callout")

    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    asset_svc = utils.get_googleads_service("AssetService")

    asset_ops = []
    for t in texts:
        o = client.get_type("AssetOperation")
        o.create.callout_asset.callout_text = t
        asset_ops.append(o)
    try:
        asset_resp = asset_svc.mutate_assets(
            customer_id=customer_id, operations=asset_ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)

    ca_svc = utils.get_googleads_service("CampaignAssetService")
    link_ops = []
    for result in asset_resp.results:
        o = client.get_type("CampaignAssetOperation")
        o.create.campaign = (
            f"customers/{customer_id}/campaigns/{campaign_id}"
        )
        o.create.asset = result.resource_name
        o.create.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
        link_ops.append(o)
    try:
        ca_svc.mutate_campaign_assets(
            customer_id=customer_id, operations=link_ops
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"callouts_created": len(asset_resp.results)}


# ------------------------------------------------------- status & removal

_ENTITY_SERVICES = {
    "campaign": ("CampaignService", "CampaignOperation", "campaigns",
                 "mutate_campaigns", "CampaignStatusEnum"),
    "ad_group": ("AdGroupService", "AdGroupOperation", "adGroups",
                 "mutate_ad_groups", "AdGroupStatusEnum"),
    "ad": ("AdGroupAdService", "AdGroupAdOperation", "adGroupAds",
           "mutate_ad_group_ads", "AdGroupAdStatusEnum"),
}


@mutate_mcp.tool
def update_entity_status(
    customer_id: str,
    entity_type: str,
    entity_id: str,
    status: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Pauses or enables a campaign, ad group, or ad.

    ENABLING requires confirm=True — enabling means real money starts
    being spent. Ask the user for explicit confirmation first.

    Args:
        customer_id: The customer account id.
        entity_type: "campaign", "ad_group" or "ad".
        entity_id: Numeric id. For "ad" use the composite "adGroupId~adId".
        status: "ENABLED" or "PAUSED".
        confirm: Must be True when status is ENABLED.
    """
    _guard_writes()
    status = status.upper()
    if status not in ("ENABLED", "PAUSED"):
        raise ToolError("status deve ser ENABLED ou PAUSED.")
    if status == "ENABLED" and not confirm:
        raise ToolError(
            "Ativar significa começar a gastar dinheiro de verdade. "
            "Confirme com o usuário e chame novamente com confirm=True."
        )
    if entity_type not in _ENTITY_SERVICES:
        raise ToolError(
            f"entity_type deve ser um de: {list(_ENTITY_SERVICES)}"
        )

    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc_name, op_name, path, mutate_fn, enum_name = _ENTITY_SERVICES[
        entity_type
    ]
    svc = utils.get_googleads_service(svc_name)

    op = client.get_type(op_name)
    entity = op.update
    entity.resource_name = (
        f"customers/{customer_id}/{path}/{entity_id}"
    )
    entity.status = getattr(getattr(client.enums, enum_name), status)
    client.copy_from(
        op.update_mask,
        protobuf_helpers.field_mask(None, entity._pb),
    )
    try:
        resp = getattr(svc, mutate_fn)(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {
        "updated": resp.results[0].resource_name,
        "new_status": status,
    }


@mutate_mcp.tool
def remove_entity(
    customer_id: str,
    entity_type: str,
    entity_id: str,
    confirm: str = "",
) -> Dict[str, Any]:
    """PERMANENTLY removes a campaign, ad group, or ad. Irreversible.

    Requires confirm="EXCLUIR" (literal). Prefer pausing over removing —
    removal destroys performance history.

    Args:
        customer_id: The customer account id.
        entity_type: "campaign", "ad_group" or "ad".
        entity_id: Numeric id. For "ad" use the composite "adGroupId~adId".
        confirm: Must be the literal string "EXCLUIR".
    """
    _guard_writes()
    if confirm != "EXCLUIR":
        raise ToolError(
            "Remoção é permanente e destrói o histórico. Confirme com o "
            "usuário e chame novamente com confirm='EXCLUIR'. "
            "Considere pausar em vez de excluir."
        )
    if entity_type not in _ENTITY_SERVICES:
        raise ToolError(
            f"entity_type deve ser um de: {list(_ENTITY_SERVICES)}"
        )

    customer_id = _cid(customer_id)
    client = utils.get_googleads_client()
    svc_name, op_name, path, mutate_fn, _ = _ENTITY_SERVICES[entity_type]
    svc = utils.get_googleads_service(svc_name)

    op = client.get_type(op_name)
    op.remove = f"customers/{customer_id}/{path}/{entity_id}"
    try:
        resp = getattr(svc, mutate_fn)(
            customer_id=customer_id, operations=[op]
        )
    except GoogleAdsException as ex:
        raise _handle(ex)
    return {"removed": resp.results[0].resource_name}
