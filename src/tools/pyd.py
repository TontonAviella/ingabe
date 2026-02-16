from typing import Any, Dict, Type
from pydantic import BaseModel, ConfigDict
from pydantic import fields as pyd_fields


def _strip_titles(obj: Any) -> Any:
    if isinstance(obj, dict):
        obj.pop("title", None)
        for k in list(obj.keys()):
            obj[k] = _strip_titles(obj[k])
        return obj
    if isinstance(obj, list):
        return [_strip_titles(x) for x in obj]
    return obj


def _assert_all_properties_required(model: Type[BaseModel]) -> None:
    """Fail fast if any model field is optional/defaulted.

    Our LLM tool schema is used with strict function-calling which requires
    'required' to include every key in 'properties'. This guard ensures arg
    models don't declare optional fields (defaults or default_factory).
    """
    missing: list[str] = []
    for name, f in model.model_fields.items():
        is_required = False
        # Pydantic v2 FieldInfo typically has is_required()
        if hasattr(f, "is_required"):
            try:
                is_required = bool(f.is_required())  # type: ignore[attr-defined]
            except Exception:
                is_required = False
        else:
            # Fallback via undefined sentinel
            undefined = getattr(pyd_fields, "PydanticUndefined", object())
            is_required = (
                getattr(f, "default", undefined) is undefined
                and getattr(f, "default_factory", undefined) is undefined
            )
        if not is_required:
            missing.append(name)
    if missing:
        # Print a clear, actionable message to stdout to aid debugging during app startup
        print(
            "[Ingabe tools] Invalid tool arg model detected:",
            model.__name__,
            "— optional/default fields found:",
            sorted(missing),
            "| Fix by using Field(...) to mark them required.",
        )
        raise ValueError(
            f"Tool arg model {model.__name__} must require all fields, optional/default fields found: {sorted(missing)}. "
            "Use Field(...) to mark them required."
        )


def tool_from(fn, model: Type[BaseModel]) -> Dict[str, Any]:
    # Enforce strictness at build-time: all properties must be required
    try:
        _assert_all_properties_required(model)
    except ValueError as e:
        # Echo a helpful stdout hint including function name
        print(
            f"[Ingabe tools] Tool schema error in '{fn.__name__}' for model '{model.__name__}':",
            str(e),
        )
        raise
    schema = model.model_json_schema()

    if isinstance(schema, dict):
        schema.setdefault("type", "object")
        schema["additionalProperties"] = False
        schema = _strip_titles(schema)

    # Ensure required contains every property for strict function-calling
    props = {}
    if isinstance(schema, dict):
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            schema["required"] = sorted(list(props.keys()))

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip(),
            "strict": True,
            "parameters": schema,
        },
    }


class IngabeToolCallMetaArgs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    user_uuid: str
    conversation_id: int
    map_id: str
    project_id: str
    session: Any
