from pydantic import BaseModel, ConfigDict


class PolicyAnalysis(BaseModel):

    model_config = ConfigDict(
        strict=True,
        extra="forbid"
    )

    policy_title: str
    summary: str
    key_points: list[str]
    relevant: bool
