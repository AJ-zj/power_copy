"""Framework-neutral binding. Register the bound handlers using the existing Tool API.
No network, no writes, and no direct dependency on unknown local Tool constructors.
"""
from dataclasses import dataclass
from typing import Callable
from pydantic import BaseModel
from powercopy.data.query import PolicyQueryService, SearchPolicyArgs, PolicyDetailArgs

@dataclass(frozen=True)
class PolicyToolBinding:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable


def build_policy_bindings(service: PolicyQueryService) -> tuple[PolicyToolBinding, ...]:
    return (
        PolicyToolBinding('search_policy', '在本地政策库按字面关键词查询最新观测版本。地区精确匹配或未标注，未标注不代表全国适用。空地区查询全部。无结果不代表政策不存在。', SearchPolicyArgs, service.search_policy),
        PolicyToolBinding('get_policy_detail', '按 search_policy 返回的 policy_id 查询指定版本正文和来源。version 省略时为最新观测版本，不代表当前法律效力。正文分段时用返回的 version 和 next_offset 继续读取。', PolicyDetailArgs, service.get_policy_detail),
    )
