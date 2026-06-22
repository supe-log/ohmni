"""Smart Ohmni blueprint: basic + SLAM + navigation + autonomous exploration.

Auto-wires (via name/type matching):
- OhmniConnection.pointcloud (Out[PointCloud2]) -> voxel_mapper inputs
- OhmniConnection.odom (Out[PoseStamped])       -> localization
- voxel_mapper.global_map / occupancy           -> cost_mapper / planner / explorer
- replanning_a_star_planner -> Out[Twist]       -> OhmniConnection.cmd_vel
- wavefront_frontier_explorer.goal              -> planner goal
"""

from dimos.core.blueprints import autoconnect

from .basic import ohmni_basic

try:
    from dimos.mapping.voxels import voxel_mapper
    from dimos.mapping.costmapper import cost_mapper
    from dimos.navigation.frontier_exploration import wavefront_frontier_explorer
    from dimos.navigation.replanning_a_star.module import replanning_a_star_planner

    ohmni_smart = (
        autoconnect(
            ohmni_basic,
            voxel_mapper(voxel_size=0.05),   # 5cm voxels for indoor
            cost_mapper(),
            replanning_a_star_planner(),
            wavefront_frontier_explorer(),   # autonomous exploration of unknown space
        )
        .global_config(n_workers=7, robot_model="ohmni_52")
    )
except ImportError as e:
    # Mapping/navigation modules may require extra dependencies
    import logging
    logging.getLogger(__name__).warning(
        "Smart blueprint unavailable — missing dependencies: %s. "
        "Install dimos with mapping extras.", e
    )
    ohmni_smart = ohmni_basic
