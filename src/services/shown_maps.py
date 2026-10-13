"""The maps a project shows now: the one rule for "is this layer in that project" (tower work, earlier inspections).

A project's maps form a history: removing a layer forks a new map and keeps the old one, and two uploads made at the
same time fork two sibling maps from one parent. The maps a project shows are its live maps that no live map was
forked from (the latest of each line), in a live project. An older version still listing a removed layer does not
count; both siblings do.
"""

SHOWN_MAPS_SQL = """
SELECT m.id, m.project_id, m.layers
  FROM user_mundiai_maps m
  JOIN user_mundiai_projects p ON p.id = m.project_id AND p.soft_deleted_at IS NULL
 WHERE m.soft_deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM user_mundiai_maps c WHERE c.parent_map_id = m.id AND c.soft_deleted_at IS NULL)
"""
