"""Typed contracts and generated JSON Schemas shared with the Gemini pipeline."""
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, TypeAdapter
from typing_extensions import NotRequired, TypedDict

Id = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:.*\S)?$")]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Weight = Annotated[float, Field(ge=0)]
Kind = Literal["object", "container", "door", "surface", "room"]
ArgKind = Literal["object", "container", "door", "surface", "room", "carrier", "robot", "any"]


def unique(values):
    if len(values) != len(set(values)):
        raise ValueError("entries must be unique")
    return values


Ids = Annotated[list[Id], AfterValidator(unique), Field(json_schema_extra={"uniqueItems": True})]
NonemptyIds = Annotated[Ids, Field(min_length=1)]
RoomPair = Annotated[Ids, Field(min_length=2, max_length=2)]


class Constraints(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    requirements: NotRequired[list[Text]]
    allowed_robots: NotRequired[NonemptyIds]
    required_actions: NotRequired[NonemptyIds]
    payload_kg: NotRequired[Weight]


class Task(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    task_id: Id
    description: Text
    depends_on: NotRequired[Ids]
    constraints: NotRequired[Constraints]


class Entity(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow", strict=True)
    kind: Kind
    in_: NotRequired[Annotated[Id, Field(alias="in")]]
    weight_kg: NotRequired[Weight]
    connects: NotRequired[RoomPair]


class RobotState(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow", strict=True)
    at: Id
    room: NotRequired[Id]
    holding: NotRequired[Id | None]
    airborne: NotRequired[bool]


class World(TypedDict):
    __pydantic_config__ = ConfigDict(extra="allow", strict=True)
    entities: Annotated[dict[Id, Entity | Kind], Field(min_length=1)]
    robots: Annotated[dict[Id, RobotState], Field(min_length=1)]
    open: NotRequired[Ids]
    passages: NotRequired[list[RoomPair]]


class Mission(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    mission_id: Id
    natural_language: NotRequired[Text]
    tasks: Annotated[list[Task], Field(min_length=1)]
    world: World


class Action(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    syntax: Text
    args: dict[Id, Annotated[list[ArgKind], Field(min_length=1), AfterValidator(unique)]]


class Robot(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    id: Id
    role: NotRequired[str]
    max_payload_kg: Weight
    cargo: bool
    actions: Annotated[dict[Id, Action], Field(min_length=1)]


class Fleet(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    _comment: NotRequired[str]
    robots: Annotated[list[Robot], Field(min_length=1)]


MISSION_ADAPTER = TypeAdapter(Mission)
FLEET_ADAPTER = TypeAdapter(Fleet)
MISSION_SCHEMA = MISSION_ADAPTER.json_schema(by_alias=True)
FLEET_SCHEMA = FLEET_ADAPTER.json_schema(by_alias=True)
