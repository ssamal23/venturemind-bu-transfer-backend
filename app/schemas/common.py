from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """JSON uses camelCase to match the React types."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
