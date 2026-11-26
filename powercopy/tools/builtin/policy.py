from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from powercopy.tools.base import Tool


# 1. 定义工具的输入参数
#
# strip_whitespace=True 会先去掉首尾空白，再检查 min_length，
# 因此纯空格输入（如 "   "）会被压成空字符串并校验失败。
RegionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1)
]
KeywordText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1)
]


class PolicySearchInput(BaseModel):

    model_config = ConfigDict(
        strict=True,
        extra="forbid"
    )

    region: RegionText = Field(
        description="需要检索的省份或地区"
    )

    keyword: KeywordText = Field(
        description="政策检索关键词"
    )


# 2. 编写实际业务函数
def search_policy(region: str, keyword: str):

    # 当前使用模拟结果
    return {
        "region": region,
        "keyword": keyword,
        "mock": True,
        "documents": [
            {
                "id": "mock-001",
                "title": "峰谷分时电价政策（模拟材料）"
            }
        ]
    }


# 3. 将函数封装成标准工具
policy_search_tool = Tool(
    name="search_policy",
    description=(
        "根据地区和关键词检索售电政策。"
        "当前为模拟工具，不返回真实政策依据。"
    ),
    args_schema=PolicySearchInput,
    handler=search_policy
)


class PolicyDetailInput(BaseModel):

    model_config = ConfigDict(
        strict=True,
        extra="forbid"
    )

    policy_id: str = Field(
        min_length=1,
        description="从政策检索工具获得的政策 ID"
    )


def get_policy_detail(policy_id: str):

    # 模拟政策详情数据库
    documents = {
        "mock-001": {
            "id": "mock-001",
            "title": "峰谷分时电价政策（模拟材料）",
            "content": (
                "这是用于测试 Agent 工具链路的模拟资料，"
                "不包含真实电价比例或正式政策条款。"
            ),
            "mock": True
        }
    }

    document = documents.get(policy_id)

    return {
        "found": document is not None,
        "policy_id": policy_id,
        "document": document,
        "mock": True
    }


policy_detail_tool = Tool(
    name="get_policy_detail",
    description=(
        "根据政策 ID 获取政策详细资料。"
        "应使用 search_policy 返回的政策 ID，"
        "不要自行编造 ID。当前仅支持模拟资料。"
    ),
    args_schema=PolicyDetailInput,
    handler=get_policy_detail
)
