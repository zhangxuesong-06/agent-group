from __future__ import annotations

from typing import Any

from .store import Store


def cluster_public(store: Store, cluster: dict[str, Any]) -> dict[str, Any]:
    """Attach live model information for the cluster settings UI."""
    by_id = {provider["id"]: provider for provider in store.list_providers()}
    models = []
    missing = []
    for provider_id in cluster.get("provider_ids") or []:
        provider = by_id.get(provider_id)
        if provider is None:
            missing.append(provider_id)
            continue
        enabled = bool(provider.get("is_active", provider.get("enabled", True)))
        models.append(
            {
                "id": provider["id"],
                "name": provider["name"],
                "model": provider["model"],
                "enabled": enabled,
            }
        )
    enabled = [model for model in models if model["enabled"]]
    return {
        **cluster,
        "models": models,
        "enabled_count": len(enabled),
        "missing_count": len(missing),
        "single_model": len(enabled) <= 1,
    }


def build_cluster_snapshot(
    store: Store,
    cluster_id: str | None = None,
    cluster_mode: bool = True,
    mock: bool = False,
) -> dict[str, Any]:
    """Resolve a saved cluster (or all models) into an immutable run snapshot."""
    cluster = store.get_cluster(cluster_id) if cluster_id else None
    if cluster_id and cluster is None:
        raise ValueError("集群不存在")
    if cluster:
        name = cluster["name"]
        provider_ids = list(cluster.get("provider_ids") or [])
        planner_id = cluster.get("planner_id") or ""
        snapshot_id = cluster["id"]
    else:
        name = "全部模型"
        provider_ids = [provider["id"] for provider in store.get_enabled_providers()]
        active = store.get_active_provider()
        planner_id = active["id"] if active else ""
        snapshot_id = ""

    enabled = {
        provider["id"]: provider for provider in store.get_enabled_providers()
    }
    selected = [enabled[pid] for pid in provider_ids if pid in enabled]
    if not selected:
        if mock:
            return {
                "id": snapshot_id,
                "name": name,
                "provider_ids": [],
                "planner_id": "",
                "single_model": True,
                "models": [],
            }
        raise ValueError("集群内没有可用模型（可能已被删除或停用）")

    planner = next(
        (provider for provider in selected if provider["id"] == planner_id),
        selected[0],
    )
    single_model = (not cluster_mode) or len(selected) <= 1
    if single_model:
        selected = [planner]
    return {
        "id": snapshot_id,
        "name": name,
        "provider_ids": [provider["id"] for provider in selected],
        "planner_id": planner["id"],
        "single_model": single_model,
        "models": [
            {
                "id": provider["id"],
                "name": provider["name"],
                "model": provider["model"],
            }
            for provider in selected
        ],
    }
