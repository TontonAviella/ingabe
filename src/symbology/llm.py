import random

from src.postgis_tiles import MVT_LAYER_NAME


def generate_maplibre_layers_for_layer_id(layer_id: str, geometry_type: str) -> list:
    colors = [
        "#FF6B6B",
        "#4ECDC4",
        "#45B7D1",
        "#96CEB4",
        "#FFEAA7",
        "#DDA0DD",
        "#98D8C8",
        "#F7DC6F",
        "#BB8FCE",
        "#85C1E9",
        "#F8C471",
        "#82E0AA",
        "#F1948A",
        "#85C1E9",
        "#D7BDE2",
        "#A9DFBF",
        "#F9E79F",
        "#AED6F1",
        "#F5B7B1",
        "#A3E4D7",
    ]

    selected_color = random.choice(colors)

    geometry_type_lower = geometry_type.lower() if geometry_type else "unknown"

    layers = []

    if geometry_type_lower in ["point", "multipoint"]:
        layers.append(
            {
                "id": f"{layer_id}",
                "type": "circle",
                "source": layer_id,
                "source-layer": MVT_LAYER_NAME,
                "paint": {
                    "circle-radius": 6,
                    "circle-color": selected_color,
                    "circle-stroke-width": 1,
                    "circle-stroke-color": [
                        "case",
                        ["boolean", ["feature-state", "selected"], False],
                        "#FF8C42",
                        "#000",
                    ],
                },
                "metadata": {"layer_name": layer_id},
            }
        )
    elif geometry_type_lower in ["linestring", "multilinestring"]:
        layers.append(
            {
                "id": f"{layer_id}",
                "type": "line",
                "source": layer_id,
                "source-layer": MVT_LAYER_NAME,
                "paint": {
                    "line-color": [
                        "case",
                        ["boolean", ["feature-state", "selected"], False],
                        "#FF8C42",
                        selected_color,
                    ],
                    "line-width": 2,
                },
                "metadata": {"layer_name": layer_id},
            }
        )
    else:
        layers.append(
            {
                "id": f"{layer_id}",
                "type": "fill",
                "source": layer_id,
                "source-layer": MVT_LAYER_NAME,
                "paint": {
                    "fill-color": selected_color,
                    "fill-opacity": [
                        "case",
                        ["boolean", ["feature-state", "selected"], False],
                        0.9,
                        0.6,
                    ],
                    "fill-outline-color": "#000",
                },
                "metadata": {"layer_name": layer_id},
            }
        )

        layers.append(
            {
                "id": f"{layer_id}-line",
                "type": "line",
                "source": layer_id,
                "source-layer": MVT_LAYER_NAME,
                "paint": {
                    "line-color": [
                        "case",
                        ["boolean", ["feature-state", "selected"], False],
                        "#FF8C42",
                        "#000",
                    ],
                    "line-width": 1,
                },
                "metadata": {"layer_name": layer_id},
            }
        )

    return layers
